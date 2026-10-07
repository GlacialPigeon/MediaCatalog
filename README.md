# MediaCatalog 3.0.2

Catalog your local media, analyze it with FFprobe, and generate reports. Runs on Windows and Linux.

**Author:** GlacialPigeon  
**GitHub:** https://github.com/GlacialPigeon

---

## 1. Overview

MediaCatalog is a local media cataloging app for **Windows and Linux**.

It scans your local and network media sources, analyzes video files with **FFprobe**, keeps a persistent catalog on your machine, and generates **Excel, JSON, and TXT reports**.

It's built with large libraries in mind, and it doesn't need any cloud service.

### Main features

- Works on Windows and Linux
- Local and network media sources
- Incremental Normal Scan that reuses cached metadata
- Force Reprobe to reanalyze media
- Scanning of multiple sources at once
- FFprobe analysis of video, audio, subtitles, codecs, resolution, HDR, languages, duration, bitrate, and other stream metadata
- Source normalization and duplicate detection
- Safe handling of sources that are temporarily unavailable
- Excel, JSON, and TXT reports
- English and Slovenian interface
- Adjustable FFprobe workers, timeout, and retries
- Adjustable logging levels

---

## 2. Requirements & Installation

MediaCatalog is distributed as a single archive:

```text
MediaCatalog-3.0.2.zip
```

Extract it to a folder you plan to keep before running the app.

### Windows

#### Python

You'll need Python 3.11.

Start MediaCatalog with:

```powershell
py -3 main.py
```

or, if Python is directly on your `PATH`:

```powershell
python main.py
```

#### FFmpeg / FFprobe

MediaCatalog uses `ffprobe.exe` to analyze media.

You can grab a prebuilt Windows FFmpeg package that includes `ffprobe.exe` here:

[Gyan FFmpeg Builds](https://www.gyan.dev/ffmpeg/builds/)

If you'd rather build FFmpeg yourself, the official source repository is here:

[FFmpeg/FFmpeg on GitHub](https://github.com/FFmpeg/FFmpeg)

MediaCatalog looks for FFprobe in this location:

```text
MediaCatalog\
└── ffmpeg\
    └── bin\
        └── ffprobe.exe
```

MediaCatalog doesn't download FFmpeg for you.

If your FFmpeg build needs extra DLL files, keep everything in the layout that the distribution came with.

#### XlsxWriter

XlsxWriter is only needed for Excel (`.xlsx`) reports.

Install it with:

```powershell
py -3 -m pip install XlsxWriter
```

or:

```powershell
python -m pip install XlsxWriter
```

Without XlsxWriter:

- JSON reports still work
- TXT reports still work
- Excel reports aren't available

---

### Linux

You need Python 3.11 and FFprobe installed on the system.

#### Debian / Ubuntu

Install the base requirements:

```bash
sudo apt update
sudo apt install python3 ffmpeg
```

Then choose how you want to install XlsxWriter.

##### System package

Install XlsxWriter system-wide with:

```bash
sudo apt install python3-xlsxwriter
```

Start MediaCatalog with:

```bash
python3 main.py
```

##### Virtual environment

If you prefer to keep XlsxWriter inside a virtual environment, install venv support first:

```bash
sudo apt install python3-venv
```

Then create and use the environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install XlsxWriter
python main.py
```

Activate the environment again each time before starting MediaCatalog:

```bash
source .venv/bin/activate
python main.py
```

Other Linux distributions will be documented once they've been tested.

---

## 3. First Run

A typical MediaCatalog session looks like this:

```text
Start MediaCatalog
        ↓
Add media sources
        ↓
Run a scan
        ↓
Generate reports
```

### Release folder

A standard release folder looks like this:

```text
MediaCatalog/
├── main.py
├── README.md
├── locales/
├── modules/
└── ffmpeg/        # Windows FFprobe installation
```

The runtime folders `data`, `logs`, `reports`, and `pathlists` are created automatically when they're needed.

### Add media sources

Open:

```text
Configuration -> Paths
```

You can:

```text
Add Source
Remove Source
Import Sources from File
Export Sources to File
```

### Scan media

Open:

```text
Scan Media
```

For everyday use, choose:

```text
Start Scan
```

### Generate reports

Once a scan has finished, you can generate reports from:

```text
Generate Reports
```

---

## 4. Media Sources

You can scan multiple media sources into one catalog.

Sources are normalized, so equivalent paths don't end up cataloged more than once.

When you add or import a source, MediaCatalog can detect:

- duplicate sources
- equivalent aliases
- overlapping sources
- sources already covered by a broader source you selected
- unavailable sources

If a configured source is temporarily unavailable, it's kept rather than quietly removed.

### Import and Export

You can import a list of sources from a UTF-8 text file.

Every non-empty line is treated as one source:

```text
/path/to/media
/path/to/movies
/path/to/shows
```

or on Windows:

```text
D:\Media
E:\Movies
\\SERVER\Media
```

Imported sources go through the same normalization and duplicate detection as ones you add by hand.

Exported source lists are stored under:

```text
pathlists/
```

### Windows

These source forms are supported:

```text
C:\Media
D:\Movies
\\SERVER\Media
M:\Movies
```

MediaCatalog works with:

- local drives and folders
- UNC network paths
- mapped network drives
- Windows Network Shortcuts

Mapped drives, UNC paths, and supported Network Shortcuts that point to the same network storage are resolved to one canonical network source.

So if several aliases lead to the same share, they won't be treated as separate media libraries.

MediaCatalog also notices when a path you pick is already covered by a broader source.

### Linux

MediaCatalog works with:

- local folders
- mounted local filesystems
- CIFS network shares
- symbolic links
- relative paths
- home-directory paths using `~`

Examples:

```text
/media/storage
/mnt/media
~/Media
./Media
../Media
```

Path normalization takes care of redundant separators and trailing slashes.

Symbolic links are resolved to their real target, so the same source doesn't get cataloged twice, once via a symlink and once via its actual path.

Linux paths follow normal POSIX rules and are case-sensitive:

```text
/home/user/Media
/home/user/media
```

If both exist, they're treated as two different paths.

For local mounted filesystems, MediaCatalog uses a stable storage identity where one is available, so a change in device name doesn't make it look like a new storage.

CIFS sources are identified by their network share, not just by where they're mounted locally. This means several mount points that point to the same share are recognized as the same storage.

System-managed automounts also work once the underlying filesystem or network share becomes available.

---

## 5. Scanning

There are two scan modes:

```text
Normal Scan
Force Reprobe
```

Before a scan starts, MediaCatalog checks that your configured sources and FFprobe are available.

Any unavailable sources are shown during this preflight check and are skipped.

### Normal Scan

Normal Scan is the standard mode for keeping your catalog up to date.

For media that hasn't changed, MediaCatalog reuses the metadata it already has instead of probing every file again.

New or changed media is analyzed with FFprobe.

Files that were previously identified as non-video can also be reused from the cache.

A failed probe isn't saved as successfully cached media, so it can be retried on later scans.

When a scan completes, you'll see statistics such as:

- files seen
- media candidates
- reused entries
- cached non-video entries
- probed files
- successful probes
- failed probes
- non-video results
- new entries
- changed entries
- missing entries

### Force Reprobe

Force Reprobe skips the usual metadata reuse and runs FFprobe on the media candidates again.

You can choose the scope:

```text
All Sources
Single Source
```

It's useful when you want to:

- reanalyze media after changing FFmpeg / FFprobe
- rebuild existing metadata
- troubleshoot media analysis

### Unavailable sources

If a source goes offline, MediaCatalog won't treat it as a library where every file has suddenly vanished.

This protects your catalog from false mass-missing results caused by disconnected disks, unavailable network shares, mount failures, or permission problems.

### Stopping a scan

While a scan is running:

- the first `Ctrl+C` asks for a graceful stop
- the second `Ctrl+C` forces an emergency termination

The graceful stop is what you'll want in most cases.

---

## 6. Reports

MediaCatalog can generate:

```text
Excel (.xlsx)
JSON
TXT
```

or all formats in one go.

### Report scopes

You can create reports for:

```text
All Sources - Combined
All Sources - Separate Files
Select Source
```

### Report location

Reports are saved under:

```text
reports/
```

and grouped by date and by report operation.

Example:

```text
reports/
└── 2026-10-07/
    └── 065122_All_Combined/
        ├── MediaCatalog_All.xlsx
        ├── MediaCatalog_All.json
        └── MediaCatalog_All.txt
```

Excel output requires XlsxWriter. Installation is covered in **Requirements & Installation**.

JSON and TXT reports don't require XlsxWriter.

---

## 7. Settings

### Language

Open:

```text
Configuration -> Language
```

MediaCatalog 3.0.2 ships with:

```text
en-US  English
sl-SI  Slovenščina
```

English (`en-US`) is the fallback locale.

### Community translations

You can add more locale files, and you don't have to translate every UI string.

A community translation can include just the strings the translator wants to cover. Anything missing falls back to English.

That means you can build and test a translation step by step instead of translating the whole app up front.

A compatible locale can be:

- kept locally in the `locales/` folder
- shared with the project, so it may be included in a future MediaCatalog release

When you create a locale, keep the existing key structure and translate only the values, not the keys.

Community translations are very welcome, and they don't have to be complete to be useful.

### FFprobe settings

Open:

```text
Configuration -> FFprobe Settings
```

You can adjust the following:

#### Workers

Sets how many FFprobe workers run in parallel.

Default:

```text
8
```

Accepted range:

```text
1..32
```

#### Timeout

Sets how long a single FFprobe attempt may run.

Default:

```text
30 seconds
```

#### Retries

Sets how many extra attempts are made after a failed probe.

Default:

```text
1
```

With the default, a failing probe is tried at most twice.

---

## 8. Logging

Session logs are written to:

```text
logs/
```

The available logging levels are:

### `error_only`

Minimal logging, mostly for warnings, failures, and essential session info.

### `normal`

The default level.

Records regular app activity, with enough detail to review scans, reports, source handling, and app state.

### `full`

Detailed diagnostic logging.

Use it when you're digging into source resolution, scanning, FFprobe failures, path handling, or anything else that behaves unexpectedly.

### `full_with_excluded_file`

Everything `full` logs, plus a separate diagnostic file listing the files that were excluded by extension.

Use it when you need to see exactly which files were skipped while media was being enumerated.

For most people:

```text
normal
```

is the recommended setting.

Logging level is stored in:

```text
data/config.json
```

Look for:

```json
"logging": {
    "level": "normal"
}
```

Change the `level` value to one of:

```text
error_only
normal
full
full_with_excluded_file
```

---

## 9. Troubleshooting

### Windows

#### FFprobe not found

Check that the FFprobe layout matches the Windows setup described in **Requirements & Installation**.

### Linux

#### FFprobe not found

Check:

```bash
ffprobe -version
```

If FFprobe isn't available, install it as described in **Requirements & Installation**.

#### CIFS source unavailable

Make sure the share is mounted and accessible outside MediaCatalog.

For example:

```bash
ls /mnt/media
```

If the path is managed by systemd automount, accessing it may trigger the CIFS mount behind it.

#### Permission denied

Make sure the user running MediaCatalog can read and traverse the entire media source.

MediaCatalog treats inaccessible sources as unavailable, rather than marking all media from that source as missing.

---

## 10. Release Notes - v3.0.2

MediaCatalog 3.0.2 adds Linux support alongside Windows.

### Highlights

- Added Linux platform support
- Added Linux local filesystem, CIFS, symlink, and automount handling
- Improved cross-platform source identity and path normalization
- Preserved Windows mapped-drive, UNC, and Network Shortcut handling
- Validated Normal Scan, Force Reprobe, cache reuse, unavailable-source protection, restart persistence, and report generation on Windows and Linux

MediaCatalog 3.0.2 passed the final Windows and Linux regression testing performed for this release.
