#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HistoryZip - ZIP-based Git code history manager web app
=========================================================

A single-file Python application that lets users manage the version
history of a codebase using nothing but a web UI and ZIP files -
no direct Git commands required.

Concepts
--------
- Snapshot ZIP   : a plain ZIP of project files at a point in time (no .git).
                    Uploading one records the diff against the current state
                    as a new Git commit.
- History ZIP    : a ZIP that contains `project/.git`, i.e. a full Git
                    working tree. Uploading one restores/imports that history.
                    Downloading one exports the current history as a ZIP.

On upload, the ZIP kind is auto-detected (presence of a `.git` folder
anywhere inside the archive means "history ZIP", otherwise "snapshot ZIP").

Each commit can optionally be given a Git tag name and a commit message
from the UI. Every version always has a tag: if none is given, a
permanent, sequential one (v0, v1, ...) is auto-generated and persisted
as a real Git tag, so the Tag column is never blank and never shifts
just because some other version was deleted. Any version - not just the
most recent - can be edited (tag name / commit message) or permanently
deleted; other
versions' recorded content is preserved exactly when this happens.

Storage: the --data directory is temporary scratch space only. It is
wiped clean every time this server process starts, and can also be wiped
on demand from the UI via the "Clear" button. Use "Download history ZIP"
to save state permanently, and re-upload it later to resume work.

Dependencies: Python standard library only, plus a system `git` binary.

Usage:
    python3 historyzip_app.py [--port 8765] [--data ./data]

Then open http://localhost:8765/ in a browser. This app manages a single
project at a time; use "Clear" in the UI to wipe it and start over.
"""

import argparse
import glob
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, quote

# --------------------------------------------------------------------------
# Constants / global state
# --------------------------------------------------------------------------

APP_NAME = "HistoryZip"
GITHUB_URL = "https://github.com/covao/HistoryZip"

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DEFAULT_DATA_DIR = os.path.join(BASE_DIR, "data")

# In-memory task registry used to report progress of background jobs
# (uploads and ZIP generation) to the frontend via polling.
TASKS = {}
TASKS_LOCK = threading.Lock()

# Auxiliary files that must never be included in a generated history ZIP.
# Kept as a top-level filter so that legacy history ZIPs (from older
# versions of this app) that still contain these files are also cleaned
# up on export.
HISTORY_ZIP_EXCLUDE = {"manifest.json", "versions.json", "ZIP_HISTORY_README.md"}


def _clear_readonly_and_retry(func, path, exc):
    """Error handler for shutil.rmtree().

    Git writes loose objects under `.git/objects/**` (and some other
    internal files) as read-only. On POSIX this doesn't block deletion,
    because Unix decides deletability from the *containing directory's*
    write permission, not the file's own mode bits. Windows ties deletion
    to the file's own read-only attribute instead, so removing/replacing an
    existing `.git` folder fails there with PermissionError / WinError 5
    unless the read-only attribute is cleared first. Clear it and retry.
    """
    try:
        os.chmod(path, stat.S_IWRITE)
    except OSError:
        pass
    func(path)


def force_rmtree(path):
    """shutil.rmtree() that also clears Git's read-only file attribute, so
    that removing/replacing an existing repository works on Windows too."""
    if not os.path.exists(path):
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_clear_readonly_and_retry)
    else:
        shutil.rmtree(path, onerror=_clear_readonly_and_retry)


def force_remove(path):
    """os.remove() that also clears Git's read-only file attribute first,
    for the same reason as force_rmtree() above."""
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE)
        os.remove(path)


def clear_data_dir(data_dir):
    """Delete everything under the data directory and recreate it empty.
    Used at server startup, where the folder should exist and be ready
    immediately."""
    force_rmtree(data_dir)
    os.makedirs(data_dir, exist_ok=True)


def remove_data_dir(data_dir):
    """Delete the data directory itself (not just its contents), leaving
    no folder behind on disk. Used by the "Clear" button. The directory is
    recreated automatically (via os.makedirs) the next time it is needed,
    e.g. by the next upload."""
    force_rmtree(data_dir)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Task registry (used for progress reporting)
# --------------------------------------------------------------------------

def task_create():
    tid = uuid.uuid4().hex
    with TASKS_LOCK:
        TASKS[tid] = {
            "id": tid,
            "status": "running",   # running | done | error
            "progress": 0,
            "message": "",
            "result_path": None,
            "result_name": None,
            "error": None,
            "created": time.time(),
        }
    return tid


def task_update(tid, **kw):
    with TASKS_LOCK:
        if tid in TASKS:
            TASKS[tid].update(kw)


def task_get(tid):
    with TASKS_LOCK:
        t = TASKS.get(tid)
        return dict(t) if t else None


# --------------------------------------------------------------------------
# Git helpers
# --------------------------------------------------------------------------

class GitError(Exception):
    pass


def run_git(args, cwd):
    r = subprocess.run(
        ["git"] + args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if r.returncode != 0:
        raise GitError("git %s failed: %s" % (" ".join(args), r.stderr.strip()))
    return r.stdout


def ensure_git_identity(repo_dir):
    r = subprocess.run(["git", "config", "user.email"], cwd=repo_dir, capture_output=True, text=True)
    if not r.stdout.strip():
        subprocess.run(["git", "config", "user.email", "historyzip@local"], cwd=repo_dir)
        subprocess.run(["git", "config", "user.name", "HistoryZip"], cwd=repo_dir)
    # Work around "detected dubious ownership" issues in container environments
    subprocess.run(["git", "config", "--global", "--add", "safe.directory", "*"], capture_output=True)


def sanitize_tag_name(name):
    """Sanitize a user-supplied string into a valid, simple Git tag name."""
    name = (name or "").strip()
    if not name:
        return None
    bad_chars = set(' ~^:?*[\\\t\r\n')
    cleaned = "".join(("-" if c in bad_chars else c) for c in name)
    cleaned = cleaned.strip(".")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("/")
    if not cleaned or cleaned == "@":
        return None
    return cleaned[:100]


def get_tag_map(repo_dir):
    """Return {commit_hash: [tag_name, ...]} for all lightweight tags."""
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return {}
    try:
        r = subprocess.run(
            ["git", "for-each-ref", "refs/tags", "--format=%(objectname) %(refname:short)"],
            cwd=repo_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
    except OSError:
        return {}
    if r.returncode != 0:
        return {}
    tag_map = {}
    for line in r.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) != 2:
            continue
        obj_hash, tag_name = parts
        tag_map.setdefault(obj_hash, []).append(tag_name)
    return tag_map


def next_auto_tag(repo_dir):
    """Return the next stable, sequential auto-tag (e.g. "v7") for this
    repo, backed by a monotonically increasing counter persisted in the
    repo's own git config (.git/config - it travels with history ZIP
    exports/imports since .git is included). The counter is never reused,
    even if earlier versions are later deleted, so once a version gets an
    auto-tag it never changes just because something happens to other
    versions."""
    r = subprocess.run(
        ["git", "config", "--get", "historyzip.next-id"],
        cwd=repo_dir, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    try:
        n = int(r.stdout.strip()) if r.returncode == 0 and r.stdout.strip() else 0
    except ValueError:
        n = 0
    subprocess.run(["git", "config", "historyzip.next-id", str(n + 1)], cwd=repo_dir, capture_output=True)
    return "v%d" % n


def git_log_entries(repo_dir):
    """Return commits newest-first, each with an index (0 = oldest) and
    tags. Any commit that has no tag at all yet is backfilled with a
    permanent auto-generated one (see next_auto_tag), so every version
    always has a stable, non-blank identifier to display - this runs on
    every call, so it also self-heals data created before this existed."""
    if not os.path.isdir(os.path.join(repo_dir, ".git")):
        return []
    ensure_git_identity(repo_dir)
    out = run_git(
        ["log", "--pretty=format:%H%x1f%h%x1f%cI%x1f%s"],
        cwd=repo_dir,
    )
    lines = [l for l in out.split("\n") if l.strip()]
    parsed = []
    for line in lines:
        parts = line.split("\x1f")
        if len(parts) != 4:
            continue
        parsed.append(tuple(parts))  # (hash, short, date, subject)
    parsed.reverse()  # oldest first

    tag_map = get_tag_map(repo_dir)
    for h, short, date, subject in parsed:
        if not tag_map.get(h):
            auto_tag = next_auto_tag(repo_dir)
            run_git(["tag", "-f", auto_tag, h], cwd=repo_dir)
            tag_map.setdefault(h, []).append(auto_tag)

    entries = []
    for i, (h, short, date, subject) in enumerate(parsed):
        entries.append({
            "hash": h,
            "short": short,
            "date": date,
            "subject": subject,
            "tags": tag_map.get(h, []),
            "index": i,
            "fallback_label": "v%d" % i,
        })
    for e in entries:
        e["label"] = e["tags"][0] if e["tags"] else e["fallback_label"]
    entries.reverse()  # newest first for display
    return entries


# --------------------------------------------------------------------------
# ZIP helpers
# --------------------------------------------------------------------------

def worktree_dir(data_dir):
    return os.path.join(data_dir, "project")


def detect_zip_kind(zip_bytes):
    """Auto-detect whether an uploaded ZIP is a 'history' ZIP (contains a
    .git directory anywhere) or a plain 'snapshot' ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for n in zf.namelist():
                segments = n.replace("\\", "/").split("/")
                if ".git" in segments:
                    return "history"
    except zipfile.BadZipFile:
        raise GitError("The uploaded file is not a valid ZIP archive.")
    return "snapshot"


def extract_zip_to_dir(zip_bytes, target_dir, progress_cb=None, strip_single_root=True):
    """Extract a ZIP (given as bytes) into target_dir.

    If strip_single_root is True (the default, used for snapshot ZIPs) and
    the archive has a single top-level folder, its contents are lifted up
    by one level - this is a convenience for users who zip a project by
    right-clicking its containing folder. History ZIPs must NOT have their
    top-level `project/` folder stripped this way (it is always the sole
    top-level entry, but it is meaningful, not incidental wrapping), so
    callers importing a history ZIP pass strip_single_root=False.
    """
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        names = zf.namelist()
        strip_root = None
        if strip_single_root:
            roots = set()
            for n in names:
                if not n or n.startswith("__MACOSX"):
                    continue
                roots.add(n.split("/")[0])
            if len(roots) == 1:
                only = list(roots)[0]
                if all(n == only or n.startswith(only + "/") or n.startswith("__MACOSX") for n in names):
                    strip_root = only + "/"

        total = len(names)
        for i, n in enumerate(names):
            if n.startswith("__MACOSX") or n.endswith(".DS_Store"):
                continue
            rel = n
            if strip_root and rel.startswith(strip_root):
                rel = rel[len(strip_root):]
            if not rel:
                continue
            dest = os.path.join(target_dir, rel)
            if n.endswith("/"):
                os.makedirs(dest, exist_ok=True)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(n) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
            if progress_cb and total:
                progress_cb(int((i + 1) / total * 100))


def zip_dir_to_bytes(src_dir, arc_prefix="", progress_cb=None, exclude_top_level=None):
    """Recursively ZIP src_dir and return the archive as bytes.

    exclude_top_level: an optional set of file/directory names, relative to
    src_dir, to skip (only matched at the top level of src_dir).
    """
    exclude_top_level = exclude_top_level or set()
    buf = io.BytesIO()
    all_files = []
    for root, dirs, files in os.walk(src_dir):
        rel_root = os.path.relpath(root, src_dir)
        if rel_root == ".":
            dirs[:] = [d for d in dirs if d not in exclude_top_level]
        for f in files:
            if rel_root == "." and f in exclude_top_level:
                continue
            all_files.append(os.path.join(root, f))
    total = len(all_files) or 1
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, fp in enumerate(all_files):
            rel = os.path.relpath(fp, src_dir)
            arcname = os.path.join(arc_prefix, rel) if arc_prefix else rel
            zf.write(fp, arcname)
            if progress_cb:
                progress_cb(int((i + 1) / total * 100))
    buf.seek(0)
    return buf.read()


# --------------------------------------------------------------------------
# Core logic
# --------------------------------------------------------------------------

def do_upload_snapshot(tid, data_dir, filename, zip_bytes, tag, message):
    """Import a snapshot ZIP: diff against the current worktree and commit."""
    try:
        task_update(tid, message="Preparing...", progress=2)
        wdir = worktree_dir(data_dir)
        is_new = not os.path.isdir(os.path.join(wdir, ".git"))
        os.makedirs(wdir, exist_ok=True)

        if is_new:
            task_update(tid, message="Extracting (initial import)...", progress=5)

            def cb(p):
                task_update(tid, progress=5 + int(p * 0.5))

            extract_zip_to_dir(zip_bytes, wdir, progress_cb=cb)

            run_git(["init"], cwd=wdir)
            ensure_git_identity(wdir)
            task_update(tid, message="Creating commit...", progress=60)
            run_git(["add", "-A"], cwd=wdir)
            msg = message or ("Initial import: %s" % filename)
            run_git(["commit", "--allow-empty", "-m", msg], cwd=wdir)
        else:
            task_update(tid, message="Clearing previous files...", progress=8)
            for name in os.listdir(wdir):
                if name == ".git":
                    continue
                p = os.path.join(wdir, name)
                if os.path.isdir(p):
                    force_rmtree(p)
                else:
                    force_remove(p)

            task_update(tid, message="Extracting...", progress=15)

            def cb(p):
                task_update(tid, progress=15 + int(p * 0.45))

            extract_zip_to_dir(zip_bytes, wdir, progress_cb=cb)

            ensure_git_identity(wdir)
            task_update(tid, message="Creating commit...", progress=65)
            run_git(["add", "-A"], cwd=wdir)
            msg = message or ("Update: %s" % filename)
            run_git(["commit", "--allow-empty", "-m", msg], cwd=wdir)

        commit_hash = run_git(["rev-parse", "HEAD"], cwd=wdir).strip()

        tag_note = ""
        clean_tag = sanitize_tag_name(tag)
        if clean_tag:
            run_git(["tag", "-f", clean_tag, commit_hash], cwd=wdir)
            tag_note = " [%s]" % clean_tag

        task_update(tid, message="Finalizing...", progress=85)
        entries = git_log_entries(wdir)

        head_label = entries[0]["label"] if entries else ""
        task_update(
            tid, status="done", progress=100,
            message="Done (%s)%s" % (head_label, tag_note),
        )
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)


def do_upload_history(tid, data_dir, zip_bytes):
    """Import a history ZIP (project/.git ...), replacing all current data."""
    try:
        task_update(tid, message="Extracting...", progress=5)
        if os.path.isdir(data_dir):
            for name in os.listdir(data_dir):
                p = os.path.join(data_dir, name)
                if os.path.isdir(p):
                    force_rmtree(p)
                else:
                    force_remove(p)
        os.makedirs(data_dir, exist_ok=True)

        def cb(p):
            task_update(tid, progress=5 + int(p * 0.8))

        extract_zip_to_dir(zip_bytes, data_dir, progress_cb=cb, strip_single_root=False)

        wdir = worktree_dir(data_dir)
        if not os.path.isdir(os.path.join(wdir, ".git")):
            raise GitError(
                "No 'project/.git' found in the uploaded archive. "
                "Please upload a valid history ZIP."
            )
        ensure_git_identity(wdir)
        entries = git_log_entries(wdir)
        task_update(
            tid, status="done", progress=100,
            message="History ZIP imported (%d version(s))" % len(entries),
        )
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)


def do_upload_auto(tid, data_dir, filename, zip_bytes, tag, message):
    """Entry point for the unified upload endpoint: detect the ZIP kind and
    dispatch to the matching handler."""
    try:
        kind = detect_zip_kind(zip_bytes)
    except GitError as e:
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)
        return
    if kind == "history":
        do_upload_history(tid, data_dir, zip_bytes)
    else:
        do_upload_snapshot(tid, data_dir, filename, zip_bytes, tag, message)


def find_version(entries, version_ref):
    """Find a version entry by hash, short hash, numeric index, or tag
    name. Deliberately does NOT match on fallback_label: that label is
    purely positional (recomputed from current index on every call) and
    uses the same "vN" naming pattern as real auto-generated tags, so
    matching on it could silently resolve to the wrong commit."""
    for e in entries:
        candidates = {e["hash"], e["short"], str(e["index"])} | set(e["tags"])
        if version_ref in candidates:
            return e
    return None


def rebuild_commit(wdir, tree_hash, parent_hash, message, date_iso):
    """Create a new commit object from an existing tree, without touching
    the working directory. Used to rebuild history so that dropping or
    editing one version cannot corrupt or merge-conflict with any other
    version's recorded content: each surviving commit's tree is reused
    byte-for-byte, only its parent link (and, for the edited commit, its
    message) changes."""
    env = os.environ.copy()
    if date_iso:
        env["GIT_AUTHOR_DATE"] = date_iso
        env["GIT_COMMITTER_DATE"] = date_iso
    cmd = ["git", "commit-tree", tree_hash]
    if parent_hash:
        cmd += ["-p", parent_hash]
    cmd += ["-m", message]
    r = subprocess.run(
        cmd, cwd=wdir, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env
    )
    if r.returncode != 0:
        raise GitError("git commit-tree failed: %s" % r.stderr.strip())
    return r.stdout.strip()


def rebuild_history(wdir, entries_oldest_to_newest, skip_hashes=None, message_overrides=None):
    """Rebuild the linear commit history from a list of original commit
    entries (oldest first), optionally dropping some (skip_hashes) and/or
    overriding a commit's message (message_overrides: {old_hash: message}).

    Returns (new_head_hash, {old_hash: new_hash}) for the commits that were
    kept. This walks each kept commit's *tree* directly (via
    git commit-tree) rather than replaying diffs (as `git rebase` would),
    so there is no possibility of a merge conflict and every surviving
    version's file content is guaranteed to stay byte-for-byte identical.
    """
    skip_hashes = skip_hashes or set()
    message_overrides = message_overrides or {}
    new_parent = None
    old_to_new = {}
    for e in entries_oldest_to_newest:
        if e["hash"] in skip_hashes:
            continue
        tree_hash = run_git(["rev-parse", "%s^{tree}" % e["hash"]], cwd=wdir).strip()
        message = message_overrides.get(e["hash"])
        if message is None:
            message = run_git(["log", "-1", "--format=%B", e["hash"]], cwd=wdir).strip()
        new_hash = rebuild_commit(wdir, tree_hash, new_parent, message, e["date"])
        old_to_new[e["hash"]] = new_hash
        new_parent = new_hash
    return new_parent, old_to_new


def do_zip_history(tid, data_dir):
    try:
        if not os.path.isdir(os.path.join(worktree_dir(data_dir), ".git")):
            raise GitError("There is no history yet.")

        def cb(p):
            task_update(tid, progress=int(p))

        task_update(tid, message="Generating ZIP...", progress=1)
        data = zip_dir_to_bytes(
            data_dir, arc_prefix="", progress_cb=cb, exclude_top_level=HISTORY_ZIP_EXCLUDE
        )
        out_path = os.path.join(tempfile.gettempdir(), "historyzip_%s.zip" % tid)
        with open(out_path, "wb") as f:
            f.write(data)
        task_update(tid, status="done", progress=100, message="Done", result_path=out_path, result_name="history.zip")
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)


def do_delete_version(tid, data_dir, version_ref):
    """Permanently delete an arbitrary version (commit) from history. The
    remaining commits are rebuilt on top of each other so their recorded
    content is preserved exactly, regardless of which version is removed."""
    try:
        wdir = worktree_dir(data_dir)
        if not os.path.isdir(os.path.join(wdir, ".git")):
            raise GitError("There is no history yet.")

        entries = git_log_entries(wdir)
        if len(entries) < 2:
            raise GitError("Cannot delete the only remaining version.")

        target = find_version(entries, version_ref)
        if target is None:
            raise GitError("The requested version could not be found.")

        task_update(tid, message="Rebuilding history...", progress=20)
        ensure_git_identity(wdir)
        oldest_to_newest = list(reversed(entries))
        new_head, old_to_new = rebuild_history(wdir, oldest_to_newest, skip_hashes={target["hash"]})

        task_update(tid, message="Updating tags...", progress=80)
        for e in oldest_to_newest:
            if e["hash"] == target["hash"]:
                continue
            for t in e["tags"]:
                run_git(["tag", "-f", t, old_to_new[e["hash"]]], cwd=wdir)
        for t in target["tags"]:
            run_git(["tag", "-d", t], cwd=wdir)

        task_update(tid, message="Updating working tree...", progress=95)
        run_git(["reset", "--hard", new_head], cwd=wdir)

        task_update(tid, status="done", progress=100, message="Deleted version %s" % target["label"])
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)


def do_edit_version(tid, data_dir, version_ref, new_tag, new_message):
    """Edit an arbitrary version's tag name and/or commit message. The
    version's recorded file content (tree) is untouched; only its message
    and tag change. All other versions are rebuilt on top of each other
    with their content preserved exactly, same as do_delete_version()."""
    try:
        wdir = worktree_dir(data_dir)
        if not os.path.isdir(os.path.join(wdir, ".git")):
            raise GitError("There is no history yet.")

        entries = git_log_entries(wdir)
        target = find_version(entries, version_ref)
        if target is None:
            raise GitError("The requested version could not be found.")

        task_update(tid, message="Rebuilding history...", progress=20)
        ensure_git_identity(wdir)
        oldest_to_newest = list(reversed(entries))

        overrides = {}
        clean_message = (new_message or "").strip()
        if clean_message:
            overrides[target["hash"]] = clean_message

        new_head, old_to_new = rebuild_history(wdir, oldest_to_newest, message_overrides=overrides)

        task_update(tid, message="Updating tags...", progress=80)
        for e in oldest_to_newest:
            if e["hash"] == target["hash"]:
                continue
            for t in e["tags"]:
                run_git(["tag", "-f", t, old_to_new[e["hash"]]], cwd=wdir)
        clean_tag = sanitize_tag_name(new_tag)
        if clean_tag:
            # Explicit new tag: replace whatever this version had before.
            for t in target["tags"]:
                run_git(["tag", "-d", t], cwd=wdir)
            run_git(["tag", "-f", clean_tag, old_to_new[target["hash"]]], cwd=wdir)
        else:
            # No new tag given: keep this version's existing tag(s) - auto
            # or custom - unchanged, just repoint them to its (possibly
            # new, if the message changed) commit hash.
            for t in target["tags"]:
                run_git(["tag", "-f", t, old_to_new[target["hash"]]], cwd=wdir)

        task_update(tid, message="Updating working tree...", progress=95)
        run_git(["reset", "--hard", new_head], cwd=wdir)

        task_update(tid, status="done", progress=100, message="Updated version")
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)


def do_zip_snapshot(tid, data_dir, version_ref):
    tmp_checkout = None
    try:
        wdir = worktree_dir(data_dir)
        if not os.path.isdir(os.path.join(wdir, ".git")):
            raise GitError("There is no history yet.")

        entries = git_log_entries(wdir)
        target = find_version(entries, version_ref)
        if target is None and entries:
            target = entries[0]
        if target is None:
            raise GitError("The requested version could not be found.")

        task_update(tid, message="Reading version...", progress=10)
        tmp_checkout = tempfile.mkdtemp(prefix="hz_snap_")
        r = subprocess.run(
            ["git", "archive", "--format=tar", target["hash"]],
            cwd=wdir, capture_output=True,
        )
        if r.returncode != 0:
            raise GitError("git archive failed: %s" % r.stderr.decode("utf-8", "replace"))
        with tarfile.open(fileobj=io.BytesIO(r.stdout)) as tf:
            tf.extractall(tmp_checkout)

        task_update(tid, message="Generating ZIP...", progress=40)

        def cb(p):
            task_update(tid, progress=40 + int(p * 0.55))

        data = zip_dir_to_bytes(tmp_checkout, progress_cb=cb)
        out_path = os.path.join(tempfile.gettempdir(), "historyzip_%s.zip" % tid)
        with open(out_path, "wb") as f:
            f.write(data)
        name = "%s.zip" % target["label"]
        task_update(tid, status="done", progress=100, message="Done", result_path=out_path, result_name=name)
    except Exception as e:  # noqa
        task_update(tid, status="error", error=str(e), message="Error: %s" % e)
    finally:
        if tmp_checkout and os.path.isdir(tmp_checkout):
            try:
                force_rmtree(tmp_checkout)
            except OSError:
                pass


# --------------------------------------------------------------------------
# Minimal multipart/form-data parser (standard library only)
# --------------------------------------------------------------------------

def parse_multipart(body, content_type):
    if "boundary=" not in content_type:
        raise ValueError("boundary not found")
    boundary = content_type.split("boundary=", 1)[1].strip()
    if boundary.startswith('"') and boundary.endswith('"'):
        boundary = boundary[1:-1]
    boundary_bytes = ("--" + boundary).encode("utf-8")

    fields = {}
    files = {}

    parts = body.split(boundary_bytes)
    for part in parts:
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, content = part.split(b"\r\n\r\n", 1)
        content = content.rstrip(b"\r\n")
        headers = {}
        for line in header_blob.split(b"\r\n"):
            line = line.decode("utf-8", "replace")
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        disp = headers.get("content-disposition", "")
        name = None
        filename = None
        for seg in disp.split(";"):
            seg = seg.strip()
            if seg.startswith("name="):
                name = seg.split("=", 1)[1].strip('"')
            elif seg.startswith("filename="):
                filename = seg.split("=", 1)[1].strip('"')
        if name is None:
            continue
        if filename is not None:
            files[name] = {"filename": filename, "data": content}
        else:
            fields[name] = content.decode("utf-8", "replace")
    return fields, files


# --------------------------------------------------------------------------
# HTTP handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "HistoryZip/1.0"
    data_dir = DEFAULT_DATA_DIR

    def log_message(self, fmt, *args):
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    # ---------- response helpers ----------

    def _send_json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, html):
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error_json(self, msg, status=400):
        self._send_json({"error": msg}, status=status)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        return self.rfile.read(length) if length else b""

    # ---------- GET ----------

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        try:
            if path == "/" or path == "/index.html":
                self._send_html(render_index_html())
            elif path == "/api/log":
                wdir = worktree_dir(self.data_dir)
                entries = git_log_entries(wdir)
                self._send_json({"versions": entries})
            elif path == "/api/task":
                tid = qs.get("id", [""])[0]
                t = task_get(tid)
                if not t:
                    self._send_error_json("task not found", 404)
                    return
                pub = {k: v for k, v in t.items() if k != "result_path"}
                self._send_json(pub)
            elif path == "/api/download":
                tid = qs.get("id", [""])[0]
                t = task_get(tid)
                if not t or t.get("status") != "done" or not t.get("result_path"):
                    self._send_error_json("result not ready", 404)
                    return
                fp = t["result_path"]
                if not os.path.isfile(fp):
                    self._send_error_json("file missing", 404)
                    return
                with open(fp, "rb") as f:
                    data = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                fname = quote(t.get("result_name") or "download.zip")
                self.send_header("Content-Disposition", "attachment; filename*=UTF-8''%s" % fname)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            elif path == "/favicon.ico":
                self.send_response(204)
                self.end_headers()
            else:
                self._send_error_json("not found", 404)
        except Exception as e:  # noqa
            self._send_error_json(str(e), 500)

    # ---------- POST ----------

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)
        try:
            if path == "/api/upload":
                self._handle_upload(qs)
            elif path == "/api/zip_history":
                tid = task_create()
                th = threading.Thread(target=do_zip_history, args=(tid, self.data_dir), daemon=True)
                th.start()
                self._send_json({"task_id": tid})
            elif path == "/api/zip_snapshot":
                version = qs.get("version", [""])[0]
                tid = task_create()
                th = threading.Thread(
                    target=do_zip_snapshot, args=(tid, self.data_dir, version), daemon=True
                )
                th.start()
                self._send_json({"task_id": tid})
            elif path == "/api/delete_version":
                version = qs.get("version", [""])[0]
                tid = task_create()
                th = threading.Thread(
                    target=do_delete_version, args=(tid, self.data_dir, version), daemon=True
                )
                th.start()
                self._send_json({"task_id": tid})
            elif path == "/api/edit_version":
                version = qs.get("version", [""])[0]
                new_tag = qs.get("tag", [""])[0]
                new_message = qs.get("message", [""])[0]
                tid = task_create()
                th = threading.Thread(
                    target=do_edit_version, args=(tid, self.data_dir, version, new_tag, new_message), daemon=True
                )
                th.start()
                self._send_json({"task_id": tid})
            elif path == "/api/clear":
                remove_data_dir(self.data_dir)
                self._send_json({"status": "ok"})
            else:
                self._send_error_json("not found", 404)
        except Exception as e:  # noqa
            self._send_error_json(str(e), 500)

    def _handle_upload(self, qs):
        """Unified upload endpoint. Automatically detects whether the
        uploaded ZIP is a snapshot ZIP or a history ZIP."""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            self._send_error_json("multipart/form-data required", 400)
            return
        body = self._read_body()
        fields, files = parse_multipart(body, content_type)
        if "file" not in files:
            self._send_error_json("file field required", 400)
            return
        tag = fields.get("tag", "")
        message = fields.get("message", "")
        filename = files["file"]["filename"] or "upload.zip"
        data = files["file"]["data"]

        tid = task_create()
        th = threading.Thread(
            target=do_upload_auto,
            args=(tid, self.data_dir, filename, data, tag, message),
            daemon=True,
        )
        th.start()
        self._send_json({"task_id": tid})


# --------------------------------------------------------------------------
# Frontend (embedded HTML/CSS/JS)
# --------------------------------------------------------------------------

def render_index_html():
    return INDEX_HTML.replace("__APP_NAME__", APP_NAME).replace("__GITHUB_URL__", GITHUB_URL)


INDEX_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>__APP_NAME__</title>
<style>
  :root{
    --bar-h: 52px;
    --menu-w: 280px;
    --accent: #3366ff;
    --bg: #f4f5f7;
    --panel: #ffffff;
    --border: #dfe2e8;
    --text: #1f2430;
    --muted: #6b7280;
  }
  *{box-sizing:border-box;}
  html,body{
    margin:0; padding:0; height:100%;
    background:var(--bg); color:var(--text);
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
  }
  #app{ display:flex; flex-direction:column; height:100vh; }

  /* ---- Title bar ---- */
  #titlebar{
    height:var(--bar-h); flex:0 0 auto;
    display:flex; align-items:center;
    background:linear-gradient(90deg,#1f2937,#2d3748);
    color:#fff; overflow-x:auto; overflow-y:hidden;
    white-space:nowrap; -webkit-overflow-scrolling:touch;
    cursor:grab; user-select:none;
  }
  #titlebar.dragging{ cursor:grabbing; }
  #titlebar::-webkit-scrollbar{ height:4px; }
  #hamburger{
    flex:0 0 auto; width:var(--bar-h); height:var(--bar-h);
    display:flex; align-items:center; justify-content:center;
    font-size:22px; cursor:pointer; background:rgba(255,255,255,0.05);
  }
  #hamburger:hover{ background:rgba(255,255,255,0.15); }
  #titleicon{
    flex:0 0 auto; font-size:22px; padding:0 10px; cursor:pointer;
    text-decoration:none; display:flex; align-items:center;
  }
  #titletext{
    flex:0 0 auto; font-size:16px; font-weight:600; padding-right:16px;
  }
  #titlebar-spacer{ flex:1 1 auto; min-width:12px; }
  #fullscreenbtn{
    flex:0 0 auto; width:var(--bar-h); height:var(--bar-h);
    display:flex; align-items:center; justify-content:center;
    font-size:19px; cursor:pointer; background:rgba(255,255,255,0.05);
  }
  #fullscreenbtn:hover{ background:rgba(255,255,255,0.15); }

  /* ---- Body (menu + main) ---- */
  #body{ flex:1 1 auto; display:flex; min-height:0; position:relative; }
  #menu{
    width:var(--menu-w); flex:0 0 auto; background:var(--panel);
    border-right:1px solid var(--border); overflow-y:auto;
    transition:margin-left .2s ease, transform .2s ease;
    padding:14px;
  }
  #menu.collapsed{ margin-left:calc(-1 * var(--menu-w)); }
  #main{ flex:1 1 auto; overflow-y:auto; padding:18px; min-width:0; }

  h2{ font-size:15px; margin:0 0 8px; color:var(--text); }
  .section{ margin-bottom:22px; }
  label{ display:block; font-size:12px; color:var(--muted); margin:10px 0 4px; }
  input[type=text]{
    width:100%; padding:8px 10px; border:1px solid var(--border); border-radius:6px;
    font-size:13px; background:#fff; color:var(--text);
  }
  button{
    border:none; border-radius:6px; padding:9px 14px; font-size:13px;
    background:var(--accent); color:#fff; cursor:pointer; width:100%; margin-top:8px;
    font-weight:600;
  }
  button.secondary{ background:#4b5563; }
  button.danger{ background:#dc2626; }
  button:disabled{ opacity:.5; cursor:not-allowed; }
  button:hover:not(:disabled){ filter:brightness(1.08); }

  /* ---- Drop zone ---- */
  #dropzone{
    margin-top:8px; border:2px dashed var(--border); border-radius:10px;
    padding:22px 10px; text-align:center; font-size:12.5px; color:var(--muted);
    background:#fafbfc; cursor:pointer; transition:background .15s, border-color .15s;
  }
  #dropzone.dragover{ background:#eef2ff; border-color:var(--accent); color:var(--accent); }
  #dropzone .dz-icon{ font-size:26px; display:block; margin-bottom:6px; }
  #dropzone .dz-file{ margin-top:6px; font-weight:600; color:var(--text); word-break:break-all; }
  #fileInput{ display:none; }

  .progress-wrap{ margin-top:8px; display:none; }
  .progress-wrap.active{ display:block; }
  .progress-bar{ height:8px; background:#e5e7eb; border-radius:4px; overflow:hidden; }
  .progress-bar > div{ height:100%; width:0%; background:var(--accent); transition:width .15s; }
  .progress-label{ font-size:11px; color:var(--muted); margin-top:3px; }

  .card{
    background:var(--panel); border:1px solid var(--border); border-radius:10px;
    padding:16px; margin-bottom:16px;
  }
  table{ width:100%; border-collapse:collapse; font-size:13px; }
  th,td{ text-align:left; padding:8px 6px; border-bottom:1px solid var(--border); }
  th{ color:var(--muted); font-weight:600; font-size:12px; }
  tr:hover td{ background:#f8f9fb; }
  .tag-badge{
    display:inline-block; background:#eef2ff; color:var(--accent);
    border-radius:4px; padding:2px 7px; font-size:12px; font-weight:700;
  }
  .tag-badge.none{ background:#f1f2f4; color:var(--muted); font-weight:500; }
  .actions button{ width:auto; margin:0 0 0 6px; padding:5px 10px; font-size:12px; }
  .actions button:first-child{ margin-left:0; }
  td.col-tag input, td.col-message input{
    width:100%; padding:5px 7px; border:1px solid var(--border); border-radius:5px;
    font-size:12.5px; background:#fff; color:var(--text);
  }
  .muted{ color:var(--muted); font-size:12px; }
  .toast{
    position:fixed; bottom:18px; left:50%; transform:translateX(-50%);
    background:#1f2937; color:#fff; padding:10px 16px; border-radius:8px;
    font-size:13px; opacity:0; pointer-events:none; transition:opacity .25s;
    z-index:999; max-width:90vw;
  }
  .toast.show{ opacity:1; }

  @media (max-width: 720px){
    :root{ --menu-w: 84vw; }
    #menu{ position:absolute; top:0; bottom:0; left:0; z-index:50; box-shadow:2px 0 8px rgba(0,0,0,.15); }
    #menu.collapsed{ margin-left:calc(-1 * var(--menu-w)); box-shadow:none; }
    #titletext{ font-size:14px; }
  }
</style>
</head>
<body>
<div id="app">
  <div id="titlebar">
    <div id="hamburger" title="Toggle menu">&#9776;</div>
    <a id="titleicon" href="__GITHUB_URL__" target="_blank" rel="noopener" title="__GITHUB_URL__">&#128230;</a>
    <div id="titletext">__APP_NAME__</div>
    <div id="titlebar-spacer"></div>
    <div id="fullscreenbtn" title="Toggle fullscreen">&#9974;</div>
  </div>

  <div id="body">
    <div id="menu">
      <div class="section">
        <h2>Upload ZIP</h2>
        <div class="muted">Drop a snapshot ZIP or a history ZIP (project/.git). The kind is detected automatically.</div>

        <label>Tag name (optional, snapshot only)</label>
        <input type="text" id="tagName" placeholder="e.g. v1.0, release-1">
        <label>Commit message (optional, snapshot only)</label>
        <input type="text" id="commitMessage" placeholder="e.g. fix login bug">

        <div id="dropzone">
          <span class="dz-icon">&#11014;&#65039;</span>
          <div>Drag &amp; drop a .zip here, or click to browse</div>
          <div class="dz-file" id="dzFileName"></div>
          <input type="file" id="fileInput" accept=".zip">
        </div>
        <button class="secondary" id="btnClearUpload">Clear</button>

        <div class="progress-wrap" id="pwUpload">
          <div class="progress-bar"><div id="pbUpload"></div></div>
          <div class="progress-label" id="plUpload">Waiting</div>
        </div>
      </div>

      <div class="section">
        <h2>Download history ZIP</h2>
        <div class="muted">Exports the full current state, including .git.</div>
        <button id="btnDownloadHist">Generate &amp; download</button>
        <div class="progress-wrap" id="pwZipHist">
          <div class="progress-bar"><div id="pbZipHist"></div></div>
          <div class="progress-label" id="plZipHist">Waiting</div>
        </div>
      </div>
    </div>

    <div id="main">
      <div class="card">
        <h2>Version history</h2>
        <div id="verTableWrap">
          <table>
            <thead><tr><th>Tag</th><th>Date</th><th>Message</th><th>Hash</th><th>Action</th></tr></thead>
            <tbody id="verTbody">
              <tr><td colspan="5" class="muted">Loading history...</td></tr>
            </tbody>
          </table>
        </div>
        <div class="progress-wrap" id="pwZipSnap">
          <div class="progress-bar"><div id="pbZipSnap"></div></div>
          <div class="progress-label" id="plZipSnap">Waiting</div>
        </div>
      </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
(function(){
  "use strict";

  function $(id){ return document.getElementById(id); }

  function toast(msg){
    var t = $("toast");
    t.textContent = msg;
    t.classList.add("show");
    clearTimeout(toast._h);
    toast._h = setTimeout(function(){ t.classList.remove("show"); }, 3200);
  }

  // ---- Title bar: hamburger menu toggle ----
  var menu = $("menu");
  $("hamburger").addEventListener("click", function(){
    menu.classList.toggle("collapsed");
  });

  // ---- Title bar: drag-to-scroll horizontally ----
  (function(){
    var bar = $("titlebar");
    var isDown = false, startX = 0, startScroll = 0;
    bar.addEventListener("pointerdown", function(e){
      if (e.target.closest("#hamburger, #titleicon, #fullscreenbtn")) return;
      isDown = true; bar.classList.add("dragging");
      startX = e.clientX; startScroll = bar.scrollLeft;
      bar.setPointerCapture(e.pointerId);
    });
    bar.addEventListener("pointermove", function(e){
      if (!isDown) return;
      bar.scrollLeft = startScroll - (e.clientX - startX);
    });
    function up(){ isDown = false; bar.classList.remove("dragging"); }
    bar.addEventListener("pointerup", up);
    bar.addEventListener("pointercancel", up);
  })();

  // ---- Fullscreen toggle ----
  $("fullscreenbtn").addEventListener("click", function(){
    if (!document.fullscreenElement){
      document.documentElement.requestFullscreen && document.documentElement.requestFullscreen();
    } else {
      document.exitFullscreen && document.exitFullscreen();
    }
  });

  // ---- Generic: upload a file with progress reporting ----
  function uploadFile(file, extraFields, pw, pb, pl){
    return new Promise(function(resolve, reject){
      var fd = new FormData();
      fd.append("file", file, file.name);
      for (var k in extraFields){ fd.append(k, extraFields[k]); }
      var xhr = new XMLHttpRequest();
      xhr.open("POST", "/api/upload");
      pw.classList.add("active");
      pb.style.width = "0%";
      pl.textContent = "Uploading... 0%";
      xhr.upload.onprogress = function(e){
        if (e.lengthComputable){
          var pct = Math.round(e.loaded / e.total * 100);
          pb.style.width = pct + "%";
          pl.textContent = "Uploading... " + pct + "%";
        }
      };
      xhr.onload = function(){
        if (xhr.status !== 200){
          pl.textContent = "Error";
          reject(new Error("HTTP " + xhr.status));
          return;
        }
        var res = JSON.parse(xhr.responseText);
        pl.textContent = "Processing on server...";
        pollTask(res.task_id, pw, pb, pl).then(resolve, reject);
      };
      xhr.onerror = function(){ reject(new Error("network error")); };
      xhr.send(fd);
    });
  }

  // ---- Generic: poll a background task until done/error ----
  function pollTask(taskId, pw, pb, pl){
    pw.classList.add("active");
    return new Promise(function(resolve, reject){
      var timer = setInterval(function(){
        fetch("/api/task?id=" + encodeURIComponent(taskId)).then(function(r){ return r.json(); }).then(function(t){
          pb.style.width = (t.progress || 0) + "%";
          pl.textContent = (t.message || "") + " (" + (t.progress||0) + "%)";
          if (t.status === "done"){
            clearInterval(timer);
            setTimeout(function(){ pw.classList.remove("active"); }, 900);
            resolve(t);
          } else if (t.status === "error"){
            clearInterval(timer);
            pl.textContent = "Error: " + (t.error || "");
            reject(new Error(t.error || "task error"));
          }
        }).catch(function(err){
          clearInterval(timer);
          reject(err);
        });
      }, 350);
    });
  }

  function triggerDownload(taskId){
    var a = document.createElement("a");
    a.href = "/api/download?id=" + encodeURIComponent(taskId);
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  // ---- Upload ZIP: drag & drop + click-to-browse (auto-detected kind) ----
  var dropzone = $("dropzone");
  var fileInput = $("fileInput");

  function startUpload(file){
    if (!file){ return; }
    if (!/\.zip$/i.test(file.name)){
      toast("Please select a .zip file");
      return;
    }
    $("dzFileName").textContent = file.name;
    uploadFile(file, { tag: $("tagName").value || "", message: $("commitMessage").value || "" },
      $("pwUpload"), $("pbUpload"), $("plUpload"))
      .then(function(t){ toast(t.message || "Uploaded"); loadVersions(); })
      .catch(function(e){ toast("Failed: " + e.message); });
  }

  dropzone.addEventListener("click", function(){ fileInput.click(); });
  fileInput.addEventListener("change", function(){
    if (fileInput.files[0]) startUpload(fileInput.files[0]);
  });
  ["dragenter", "dragover"].forEach(function(ev){
    dropzone.addEventListener(ev, function(e){
      e.preventDefault(); e.stopPropagation();
      dropzone.classList.add("dragover");
    });
  });
  ["dragleave", "dragend"].forEach(function(ev){
    dropzone.addEventListener(ev, function(e){
      e.preventDefault(); e.stopPropagation();
      dropzone.classList.remove("dragover");
    });
  });
  dropzone.addEventListener("drop", function(e){
    e.preventDefault(); e.stopPropagation();
    dropzone.classList.remove("dragover");
    var files = e.dataTransfer && e.dataTransfer.files;
    if (files && files.length) startUpload(files[0]);
  });

  // ---- Clear: reset the staged upload so a new file can be selected ----
  // ---- Clear: reset the staged upload, clear the history display, and
  // delete all data stored on the server ----
  $("btnClearUpload").addEventListener("click", function(){
    if (!confirm("Clear the uploaded file and permanently delete all history on the server?")) return;
    var btn = this; btn.disabled = true;
    fetch("/api/clear", { method: "POST" })
      .then(function(r){ return r.json(); })
      .then(function(){
        fileInput.value = "";
        $("dzFileName").textContent = "";
        $("tagName").value = "";
        $("commitMessage").value = "";
        $("pwUpload").classList.remove("active");
        $("pbUpload").style.width = "0%";
        $("plUpload").textContent = "Waiting";
        loadVersions();
        toast("Cleared");
      })
      .catch(function(e){ toast("Failed: " + e.message); })
      .finally(function(){ btn.disabled = false; });
  });

  // ---- Download history ZIP ----
  $("btnDownloadHist").addEventListener("click", function(){
    var btn = this; btn.disabled = true;
    fetch("/api/zip_history", { method: "POST" })
      .then(function(r){ return r.json(); })
      .then(function(res){ return pollTask(res.task_id, $("pwZipHist"), $("pbZipHist"), $("plZipHist")); })
      .then(function(t){ triggerDownload(t.id); toast("Download started"); })
      .catch(function(e){ toast("Failed: " + e.message); })
      .finally(function(){ btn.disabled = false; });
  });

  // ---- Version list ----
  function loadVersions(){
    fetch("/api/log")
      .then(function(r){ return r.json(); })
      .then(function(res){
        var tbody = $("verTbody");
        tbody.innerHTML = "";
        if (!res.versions || !res.versions.length){
          tbody.innerHTML = '<tr><td colspan="5" class="muted">No history yet. Upload a snapshot ZIP from the left panel to get started.</td></tr>';
          return;
        }
        var onlyOneVersion = res.versions.length < 2;
        res.versions.forEach(function(v){
          var tr = document.createElement("tr");
          tr.setAttribute("data-hash", v.hash);
          // Versions with no explicit tag get a permanent, stable
          // auto-generated one from the server (e.g. "v3") so this column
          // is never blank. Custom tags get the bright badge; auto ones
          // (matching /^v\d+$/) get the muted badge so they're still
          // visually distinguishable from a name the user actually chose.
          var hasTag = v.tags && v.tags.length > 0;
          var tagText = hasTag ? v.tags[0] : "\u2014";
          var isAutoTag = hasTag && /^v\d+$/.test(v.tags[0]);
          var badgeClass = (hasTag && !isAutoTag) ? "tag-badge" : "tag-badge none";
          var actionsHtml =
            '<button data-action="download" data-hash="' + v.hash + '">Download snapshot</button>' +
            '<button class="secondary" data-action="edit" data-hash="' + v.hash + '">Edit</button>' +
            '<button class="danger" data-action="delete" data-hash="' + v.hash + '"' + (onlyOneVersion ? " disabled" : "") + '>Delete</button>';
          tr.innerHTML =
            '<td class="col-tag"><span class="' + badgeClass + '">' + escapeHtml(tagText) + '</span></td>' +
            '<td>' + (v.date||"").replace("T"," ").slice(0,19) + '</td>' +
            '<td class="col-message">' + escapeHtml(v.subject||"") + '</td>' +
            '<td class="muted">' + v.short + '</td>' +
            '<td class="actions">' + actionsHtml + '</td>';
          tbody.appendChild(tr);
        });

        tbody.querySelectorAll('button[data-action="download"]').forEach(function(btn){
          btn.addEventListener("click", function(){
            var hash = btn.getAttribute("data-hash");
            btn.disabled = true;
            fetch("/api/zip_snapshot?version=" + encodeURIComponent(hash), { method: "POST" })
              .then(function(r){ return r.json(); })
              .then(function(res){ return pollTask(res.task_id, $("pwZipSnap"), $("pbZipSnap"), $("plZipSnap")); })
              .then(function(t){ triggerDownload(t.id); toast("Download started"); })
              .catch(function(e){ toast("Failed: " + e.message); })
              .finally(function(){ btn.disabled = false; });
          });
        });

        // ---- Edit: turns the Tag and Message cells of that row into
        // inline inputs, with Save / Cancel replacing the row's actions ----
        tbody.querySelectorAll('button[data-action="edit"]').forEach(function(btn){
          btn.addEventListener("click", function(){
            var hash = btn.getAttribute("data-hash");
            var v = res.versions.find(function(x){ return x.hash === hash; });
            var tr = btn.closest("tr");
            var tagTd = tr.querySelector(".col-tag");
            var msgTd = tr.querySelector(".col-message");
            var actionsTd = tr.querySelector(".actions");
            var currentTagRaw = (v && v.tags && v.tags.length) ? v.tags[0] : "";
            var currentTag = /^v\d+$/.test(currentTagRaw) ? "" : currentTagRaw;
            var currentMessage = (v && v.subject) || "";

            tagTd.innerHTML = '<input type="text" class="edit-tag" placeholder="no tag">';
            msgTd.innerHTML = '<input type="text" class="edit-message">';
            tagTd.querySelector("input").value = currentTag;
            msgTd.querySelector("input").value = currentMessage;
            actionsTd.innerHTML =
              '<button data-action="save">Save</button>' +
              '<button class="secondary" data-action="cancel">Cancel</button>';

            actionsTd.querySelector('button[data-action="cancel"]').addEventListener("click", function(){
              loadVersions();
            });
            actionsTd.querySelector('button[data-action="save"]').addEventListener("click", function(){
              var newTag = tagTd.querySelector("input").value;
              var newMessage = msgTd.querySelector("input").value;
              var saveBtn = actionsTd.querySelector('button[data-action="save"]');
              saveBtn.disabled = true;
              var url = "/api/edit_version?version=" + encodeURIComponent(hash) +
                "&tag=" + encodeURIComponent(newTag) + "&message=" + encodeURIComponent(newMessage);
              fetch(url, { method: "POST" })
                .then(function(r){ return r.json(); })
                .then(function(res2){ return pollTask(res2.task_id, $("pwZipSnap"), $("pbZipSnap"), $("plZipSnap")); })
                .then(function(t){ toast(t.message || "Updated"); loadVersions(); })
                .catch(function(e){ toast("Failed: " + e.message); saveBtn.disabled = false; });
            });
          });
        });

        tbody.querySelectorAll('button[data-action="delete"]').forEach(function(btn){
          btn.addEventListener("click", function(){
            var hash = btn.getAttribute("data-hash");
            if (!confirm("Delete this version? This permanently removes it from history.")) return;
            btn.disabled = true;
            fetch("/api/delete_version?version=" + encodeURIComponent(hash), { method: "POST" })
              .then(function(r){ return r.json(); })
              .then(function(res2){ return pollTask(res2.task_id, $("pwZipSnap"), $("pbZipSnap"), $("plZipSnap")); })
              .then(function(t){ toast(t.message || "Deleted"); loadVersions(); })
              .catch(function(e){ toast("Failed: " + e.message); btn.disabled = false; });
          });
        });
      })
      .catch(function(){
        $("verTbody").innerHTML = '<tr><td colspan="5" class="muted">Failed to load history</td></tr>';
      });
  }

  function escapeHtml(s){
    return String(s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c];
    });
  }

  loadVersions();
})();
</script>
</body>
</html>
"""


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def clear_scratch_dirs(data_dir):
    """Wipe all temporary storage used by this app: the --data working
    directory and any leftover generated-ZIP files this app previously
    wrote to the system temp folder. Called once at process startup so
    nothing persists across restarts."""
    clear_data_dir(data_dir)

    tmp_dir = tempfile.gettempdir()
    for fp in glob.glob(os.path.join(tmp_dir, "historyzip_*.zip")):
        try:
            os.remove(fp)
        except OSError:
            pass


def main():
    ap = argparse.ArgumentParser(description=APP_NAME)
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--data", default=DEFAULT_DATA_DIR)
    args = ap.parse_args()

    # Both the --data directory and this app's own leftover files in the
    # system temp folder are temporary scratch space only: they are wiped
    # clean every time the server process starts, so no project state
    # survives a restart. Persistence across restarts should be handled by
    # the user via "Download history ZIP" / re-uploading that ZIP later.
    clear_scratch_dirs(args.data)
    Handler.data_dir = os.path.abspath(args.data)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    print("%s server running: http://localhost:%d/  (data dir: %s)" % (APP_NAME, args.port, Handler.data_dir))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
