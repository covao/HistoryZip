# HistoryZip
![Demo Image](https://covao.github.io/HistoryZip/demo.gif)

## Overview
HistoryZip is a single-file Python web app that lets you manage a codebase's Git version history using nothing but ZIP files and a browser, with no Git commands required.

## Quick Start
```bash
python3 historyzip.py
```
Then open http://localhost:8765/ in your browser.

## Features
- Upload a snapshot ZIP (plain source files) and it is automatically recorded as a new Git commit
- Upload a history ZIP (a full `project/.git` working tree) to restore or import existing history
- Snapshot vs. history ZIPs are detected automatically on upload, no need to choose
- Drag-and-drop or click-to-browse ZIP upload with a live progress bar
- Tag each version with a custom name and commit message
- Edit the tag name and commit message of any version, not just the latest
- Delete any version from history, not just the latest, without affecting any other version's content
- Download the full history ZIP (including `.git`) at any time
- Download a snapshot ZIP of any past version by tag, hash, or index
- Responsive UI that works on desktop, tablet, and smartphone screens
- The "Clear" button wipes the uploaded file, the history display, and the data folder on the server
- All working data is temporary: the data folder is deleted automatically every time the server starts

## Requirements
- OS: Linux, macOS, or Windows
- Python 3.8+
- `git` command available on the system PATH

## Installation
```bash
curl -O https://raw.githubusercontent.com/covao/HistoryZip/main/historyzip.py
```

## Uninstallation
Delete `historyzip.py` and its `data` folder. HistoryZip does not modify anything else on your system.

## Usage
Start the server and open it in your browser. Drop a snapshot ZIP of your project onto the upload area to record it as the first version; drop later snapshots to record further versions, optionally adding a tag name and commit message. Use "Download history ZIP" to save the full version history (including `.git`) at any point, and re-upload that ZIP later to resume work. Use "Download snapshot" next to any version in the history table to export just that version's files.
