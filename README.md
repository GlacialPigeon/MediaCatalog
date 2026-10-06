# MediaCatalog 2.1.2

Local Windows media cataloging, FFprobe analysis, and report generation.

**Author:** GlacialPigeon  
**GitHub:** https://github.com/GlacialPigeon

---

## Overview

MediaCatalog scans one or more media sources, analyzes video files with FFprobe, stores the resulting catalog in SQLite, and generates Excel, JSON, and TXT reports.

The application is designed for local and network media libraries and keeps the catalog locally. It supports local drives, UNC shares, mapped drives, and Windows Network Shortcuts, with source normalization so the same storage is not accidentally cataloged multiple times through different aliases.

MediaCatalog 2.1.2 is the modular Windows release based on the v2 architecture.

---

## Main features

- Local Windows application; no cloud service is required.
- Persistent SQLite media catalog.
- Normal incremental scanning with reuse of unchanged catalog data.
- Force Reprobe for all configured sources or one selected source.
- Multi-source scanning with per-source results and final aggregate statistics.
- FFprobe worker pool with configurable workers, timeout, and retries.
- Source support for:
  - local folders and drives,
  - UNC paths,
  - mapped network drives,
  - Windows Network Shortcuts / `.lnk` targets.
- Canonical network-source storage using UNC paths.
- Source duplicate and verified nested-source detection.
- Persistent tracking of missing media files.
- Structured media metadata including container, video, audio, subtitle, codec, resolution, pixel format, bit depth, color/HDR, language, duration, bitrate, and related stream metadata.
- Excel, JSON, and TXT reports.
- Report scopes:
  - all sources combined,
  - all sources as separate reports,
  - one selected source.
- English and Slovenian UI (`en-US`, `sl-SI`).
- Configurable logging levels.
- Strict configuration validation and recovery.
- SQLite corruption recovery with preservation of the damaged database and available WAL/SHM sidecars.
- Single-instance protection on Windows.
- Deterministic build fingerprint recorded in every session log.

---

## v2.1.2 release changes

v2.1.2 is a focused maintenance release.

### Scan dashboard rendering

Multi-source **Normal Scan** and **Force Reprobe All** now keep completed source results append-only:

- each completed source is printed exactly once,
- previously completed source blocks are not redrawn,
- live refresh updates do not create duplicate output,
- live-area cleanup no longer adds blank lines to terminal scrollback,
- the final `SCAN COMPLETE` / `SCAN STOPPED` section remains appended below the completed source history.

Single-source Force Reprobe behavior remains unchanged.

### FFprobe version logging

FFprobe validation now keeps both forms of the version:

- **Short version** for compact UI/log display, for example:

  ```text
  ffmpeg-9.0.2-full_build
  ```

- **Full version** containing the complete original first line returned by `ffprobe -version`, for example:

  ```text
  ffprobe version 9.0.2-full_build-www.gyan.dev Copyright (c) 2007-2026 the FFmpeg developers
  ```

The full version is stored without shortening the first line.

---

## Platform

MediaCatalog 2.1.2 currently uses the Windows platform adapter and is intended to be run on Windows.

`main.py` starts the Windows implementation when `sys.platform == "win32"`.

### Validated v2.1.2 environment

The release acceptance run was performed with:

```text
Windows 11
Python 3.14.7
FFprobe 9.0.2 full_build
XlsxWriter 3.2.9
```

These versions describe the validated release environment; they are not a claim that no other compatible versions can work.

---

## Requirements

### Python

A Windows Python installation is required when running MediaCatalog from source.

Start the application with either:

```powershell
py -3 main.py
```

or:

```powershell
python main.py
```

### FFprobe

MediaCatalog expects FFprobe at exactly:

```text
ffmpeg\bin\ffprobe.exe
```

relative to the MediaCatalog application folder.

For example:

```text
MediaCatalog_2.1.2\
└── ffmpeg\
    └── bin\
        └── ffprobe.exe
```

MediaCatalog does **not** download or create the `ffmpeg` directory.

At startup, MediaCatalog validates FFprobe and the command-line capabilities required by the application. If FFprobe is missing or does not provide the required functionality, scan operations are blocked.

If the chosen FFmpeg build uses DLL dependencies, keep the required DLLs next to `ffprobe.exe` as supplied by that FFmpeg distribution.

A full FFmpeg distribution may also contain `ffmpeg.exe` and `ffplay.exe`; MediaCatalog itself uses `ffprobe.exe` for media analysis.

### XlsxWriter

`XlsxWriter` is optional and is only required for Excel (`.xlsx`) reports.

Install it with:

```powershell
py -3 -m pip install XlsxWriter
```

Without XlsxWriter:

- JSON reports continue to work,
- TXT reports continue to work,
- Excel export is unavailable.

MediaCatalog checks the dependency again when Excel output is requested, so installing XlsxWriter while the application is open does not require restarting MediaCatalog.

---

## Recommended release layout

The release folder name is:

```text
MediaCatalog_2.1.2
```

A normal installation can look like this:

```text
MediaCatalog_2.1.2\
│   main.py
│   README.md
│
├── ffmpeg\
│   └── bin\
│       ├── ffprobe.exe
│       └── ... required FFmpeg files / DLLs for the selected build
│
├── locales\
│   ├── en-US.json
│   └── sl-SI.json
│
└── modules\
    │   __init__.py
    │   application.py
    │   config.py
    │   constants.py
    │   database.py
    │   datetime_helpers.py
    │   localization.py
    │   logger.py
    │   platform_api.py
    │   platform_windows.py
    │   probe.py
    │   scan.py
    │   sources.py
    │   ui.py
    │
    └── reports\
        │   __init__.py
        │   report_common.py
        │   report_data.py
        │   report_excel.py
        │   report_json.py
        │   report_service.py
        └── report_txt.py
```

The application creates runtime directories when needed.

---

## Runtime directories and files

### `data/`

Created at startup.

Typical contents:

```text
data\
├── config.json
├── MediaCatalog.db
└── backups\                  # created when a database schema backup is required
```

SQLite may also create temporary/runtime sidecars such as:

```text
MediaCatalog.db-wal
MediaCatalog.db-shm
```

Do not delete or copy an active database while MediaCatalog is running.

### `logs/`

Created at startup.

Session logs use names such as:

```text
MediaCatalog_YYYY-MM-DD_HHMMSS.log
```

With `full_with_excluded_file`, an additional excluded-file diagnostic can be created:

```text
MediaCatalog_YYYY-MM-DD_HHMMSS_excluded.txt
```

### `reports/`

Created when reports are generated.

Reports are grouped by date and operation folder. Example:

```text
reports\
└── 2026-10-07\
    └── 003000_All_Combined\
        ├── MediaCatalog_All.xlsx
        ├── MediaCatalog_All.json
        └── MediaCatalog_All.txt
```

### `pathlists/`

Created when configured sources are exported.

The standard exported source list is:

```text
pathlists\media_sources.txt
```

Import can read a UTF-8 text file selected by the user. Each non-empty source path is normalized and validated before it is accepted.

### `ffmpeg/`

Never auto-created by MediaCatalog. The required FFprobe executable must be supplied by the user/release package.

---

## First run

1. Place MediaCatalog in its final folder, for example:

   ```text
   C:\Tools\MediaCatalog_2.1.2
   ```

2. Place FFprobe at:

   ```text
   C:\Tools\MediaCatalog_2.1.2\ffmpeg\bin\ffprobe.exe
   ```

3. Optionally install XlsxWriter for Excel reports:

   ```powershell
   py -3 -m pip install XlsxWriter
   ```

4. Start MediaCatalog:

   ```powershell
   cd C:\Tools\MediaCatalog_2.1.2
   py -3 main.py
   ```

5. Open:

   ```text
   Configuration -> Paths
   ```

   and add or import media sources.

6. Run:

   ```text
   Scan Media -> Start Scan
   ```

7. Generate reports from:

   ```text
   Generate Reports
   ```

---

## Main menu

The main application menu contains:

```text
[1] Scan Media
[2] Generate Reports
[3] Configuration
[4] Exit
```

---

## Configuring media sources

Open:

```text
Configuration -> Paths
```

Available actions:

```text
Add Source
Remove Source
Import Sources From File
Export Sources To File
```

### Supported source forms

MediaCatalog accepts paths such as:

```text
D:\Media
\\SERVER\Media
M:\Movies
```

Windows Network Shortcut / `.lnk` targets can also be resolved when the target is accessible.

### Canonical source handling

MediaCatalog separates two concepts:

- `config.json` = what the user wants to scan,
- `MediaCatalog.db` = what MediaCatalog knows about sources and media files.

Resolvable network aliases are normalized to canonical UNC paths. This prevents the same network location from being treated as different sources simply because it was entered as a mapped drive, UNC path, or shortcut.

During add/import and startup normalization MediaCatalog can detect:

- duplicate source aliases,
- multiple references to the same storage,
- sources already covered by a selected parent source,
- unavailable/unresolvable sources.

Unavailable configured sources are retained rather than silently deleted.

---

## Importing source lists

Source import reads a UTF-8 text file and treats each non-empty line as a source path.

Example:

```text
D:\Media\Movies
E:\Media\TV
\\SERVER\Media
```

Import uses the same source resolution, canonicalization, duplicate detection, and overlap rules as manually added sources.

When a batch contains a mixture of valid and invalid/unavailable entries, MediaCatalog shows the batch review before applying the valid result.

---

## Scanning

Open:

```text
Scan Media
```

Available scan modes:

```text
Start Scan
Force Reprobe
```

Before a scan starts, MediaCatalog performs preflight checks for:

- configured source availability,
- FFprobe availability/version,
- worker/timeout/retry settings,
- database status and schema.

If some configured sources are unavailable but at least one source is available, the preflight screen can continue with the available sources.

### Normal Scan

Normal Scan is the standard incremental catalog update.

It can reuse already-known unchanged media instead of probing every candidate again. New or changed candidates are analyzed with FFprobe, and the catalog is reconciled after a successfully completed source scan.

The scan summary includes:

- files seen,
- candidates,
- reused entries,
- cached non-video entries,
- files probed,
- successful probes,
- failed probes,
- non-video results,
- new catalog entries,
- changed catalog entries,
- missing catalog entries.

### Force Reprobe

Force Reprobe bypasses normal probe reuse for selected media candidates.

Two scopes are available:

```text
All Sources
Single Source
```

Use Force Reprobe when FFprobe has changed, parsing behavior has changed, or existing media metadata should be rebuilt from the source files.

---

## Scan stop behavior

During an active scan:

- the first `Ctrl+C` requests a graceful stop,
- the second `Ctrl+C` requests emergency process termination.

The graceful path is designed to stop without treating unfinished work as a successfully completed source scan.

Use the emergency path only when the normal graceful stop cannot complete.

---

## FFprobe settings

Open:

```text
Configuration -> FFprobe Settings
```

Available settings:

### Workers

Default:

```text
8
```

UI accepted range:

```text
1..32
```

### Timeout

Default:

```text
30 seconds
```

### Retries

Default:

```text
1
```

Retries are additional attempts, so the default produces a maximum of two attempts for a failing probe.

When editing Workers, Timeout, or Retries, submitting an empty value resets that setting to its factory default.

---

## Reports

Open:

```text
Generate Reports
```

### Formats

```text
Excel
JSON
TXT
All Formats
```

### Scopes

```text
All Sources - Combined
All Sources - Separate Files
Select Source
```

Report generation uses a database snapshot so one report operation sees a consistent catalog state.

A source must have suitable completed scan data before normal report generation is allowed. MediaCatalog can also warn when a source is currently unavailable or its latest scan state requires attention.

### Excel

Excel output requires XlsxWriter.

The Excel appearance is configurable in `config.json`, including:

- font name,
- global maximum column width,
- header colors,
- zebra-row colors,
- summary section colors,
- border color.

### JSON and TXT

JSON and TXT use only the Python standard library and remain available without XlsxWriter.

---

## Language

Open:

```text
Configuration -> Language
```

Bundled locales:

```text
en-US  English
sl-SI  Slovenščina
```

`en-US.json` is the canonical locale definition. Other locale files are validated against the canonical key set.

The selected locale is stored in `data/config.json`.

---

## Configuration file

The configuration file is:

```text
data\config.json
```

Current schema:

```text
ConfigSchemaVersion = 4
```

A default v2.1.2 configuration contains the following structure:

```json
{
  "ConfigSchemaVersion": 4,
  "locale": "en-US",
  "sources": [],
  "scan": {
    "workers": 8,
    "timeout_seconds": 30,
    "retries": 1,
    "queue_capacity": 256,
    "progress_refresh_seconds": 0.5,
    "follow_reparse_points": false,
    "excluded_directories": [],
    "excluded_non_video_extensions": []
  },
  "logging": {
    "level": "normal"
  },
  "excel": {
    "max_width_global": 80,
    "font_name": "Arial",
    "theme": {
      "header_background": "#1F4E78",
      "header_text": "#FFFFFF",
      "zebra_a_background": "#F2F2F2",
      "zebra_a_text": "#000000",
      "zebra_b_background": "#FFFFFF",
      "zebra_b_text": "#000000",
      "summary_section_fill": "#D9EAF7",
      "summary_section_text": "#000000",
      "border": "#D9D9D9"
    }
  }
}
```

> Note: the real generated default `excluded_non_video_extensions` list is populated with MediaCatalog's built-in non-video extensions. The shortened example above shows the schema shape rather than reproducing the complete default list.

### Strict schema validation

v2.1.2 validates `config.json` strictly.

The following make the configuration invalid:

- unknown keys,
- misspelled keys,
- missing required keys,
- invalid value types,
- unsupported schema version,
- structurally invalid sections.

When an invalid configuration is detected:

1. the original file is preserved as a timestamped backup such as:

   ```text
   config.invalid_YYYY-MM-DD_HHMMSS.json
   ```

2. a fresh default configuration is created,
3. if the complete `sources` block is structurally valid, that entire source list is salvaged,
4. partial source salvage is not performed.

---

## Startup source normalization

Before the database is synchronized, MediaCatalog normalizes configured sources.

The startup process can:

- resolve mapped drives,
- resolve Windows Network Shortcuts,
- canonicalize network storage to UNC,
- remove verified duplicates,
- collapse verified nested sources already covered by a parent source,
- retain currently unavailable or unresolvable configured sources.

If normalization changes the configured source list, the normalized configuration is saved.

---

## Database

The catalog database is:

```text
data\MediaCatalog.db
```

MediaCatalog uses SQLite and WAL journal mode.

The database stores source/storage identities, scan history, media-file catalog state, stream metadata, probe errors, cached non-video identities, and related catalog information.

Do not manually edit the database unless you are intentionally performing database-level maintenance and have a backup.

### Database corruption recovery

MediaCatalog only enters automatic corruption recovery for confirmed SQLite corruption conditions.

When confirmed corruption is detected, MediaCatalog preserves the invalid database and any available WAL/SHM recovery evidence before creating a replacement database.

Files are preserved with timestamped invalid names.

Ordinary access, permission, or locking failures are **not** silently treated as database corruption.

### Schema migration backups

When a supported database schema migration requires a backup, MediaCatalog creates a pre-migration database backup under:

```text
data\backups\
```

Migration is aborted if the required backup cannot be created successfully.

---

## Logging

Logs are stored under:

```text
logs\
```

The logging level is controlled by:

```json
"logging": {
  "level": "normal"
}
```

Supported levels are:

### `error_only`

Minimal diagnostic logging.

Keeps warnings, errors, critical failures, and mandatory INFO context needed to identify the session and operation, including:

- session/build identity,
- Python/OS/architecture/CWD,
- startup health,
- dependency health,
- scan/report operation start and finish,
- recovery and interrupt events,
- session end status.

Routine UI navigation and detailed successful-operation noise are filtered out.

### `normal`

Default level.

Includes enough information to reconstruct a normal application session, including:

- environment/build details,
- config/locale/database state,
- dependency state,
- source synchronization,
- semantic UI selections,
- preflight,
- scan/report summaries,
- final per-source scan results,
- settings changes,
- interrupts and shutdown status.

### `full`

Includes `normal` plus detailed diagnostic events such as:

- source-resolution details,
- mapped-drive / shortcut / UNC matching,
- overlap and deduplication reasoning,
- detailed exclusions,
- FFprobe retries,
- HDR diagnostic passes,
- invalid UI input,
- other DEBUG-level details.

### `full_with_excluded_file`

Same as `full`, plus a separate file containing every extension-excluded file, grouped by source and extension.

Excluded file paths are kept out of the main session log so the primary diagnostic timeline remains readable.

---

## FFprobe version information

On fresh FFprobe validation the log records:

```text
[FFPROBE] Path=...
[FFPROBE] Short version=...
[FFPROBE] Full version=...
[FFPROBE] SHA-256=...
```

`Short version` is intended for compact display.

`Full version` is the complete first line returned by:

```powershell
ffprobe -version
```

MediaCatalog also records an FFprobe SHA-256 hash after validation.

---

## Build fingerprint

Every MediaCatalog session records a deterministic SHA-256 build fingerprint.

The fingerprint is calculated from the application code and locale files:

```text
main.py
modules/**/*.py
locales/*.json
```

This allows logs from different candidate/release builds to be distinguished even when the visible version string is identical.

---

## Single-instance behavior

MediaCatalog uses a Windows global mutex to prevent multiple application instances from using the same runtime at the same time.

If the global instance lock cannot be acquired because another MediaCatalog instance is already active, the second instance is not allowed to continue normally.

---

## Updating an existing v2.1.x installation

When moving an existing tested v2.1.x catalog into a new release folder:

1. close MediaCatalog completely,
2. keep a backup of the old installation,
3. copy the existing:

   ```text
   data\config.json
   data\MediaCatalog.db
   ```

   and, if present and intentionally being preserved during a direct database move, its SQLite sidecars only while the application is fully stopped,
4. copy/reuse the required `ffmpeg\bin` installation,
5. start the new release and review the startup log before deleting the old installation.

For upgrades from substantially older or unrelated schemas, preserve the original installation and validate the migration path before replacing it.

---

## Git / development notes

Runtime state should not be committed to source control.

The repository `.gitignore` should normally exclude:

- `data/`,
- `logs/`,
- `reports/`,
- `pathlists/`,
- `ffmpeg/`,
- `__pycache__/` and `*.pyc`,
- local virtual environments,
- IDE/editor metadata,
- generated release archives and temporary files.

The application source, locale files, README, and `.gitignore` should remain tracked.

---

## Troubleshooting

### FFprobe not found

Verify that this exists:

```text
MediaCatalog_2.1.2\ffmpeg\bin\ffprobe.exe
```

If using a DLL-based FFmpeg build, verify the required DLLs are also present in the same distribution layout.

### Excel output unavailable

Install XlsxWriter:

```powershell
py -3 -m pip install XlsxWriter
```

Then select Excel output again and use MediaCatalog's dependency re-check.

### Source appears more than once through mapped drive and UNC

Use the normal source Add/Import flow. MediaCatalog resolves known aliases and stores network sources canonically so equivalent aliases can be deduplicated.

### Source is offline at startup

An unavailable configured source is retained. MediaCatalog does not automatically remove it merely because it cannot currently be resolved or accessed.

### Invalid `config.json`

Check `data/` for the timestamped `config.invalid_*.json` backup. MediaCatalog creates a clean default configuration and salvages the complete sources list only when that block is structurally valid.

### Database corruption recovery occurred

Do not immediately delete the preserved invalid database/WAL/SHM files. They are intentionally retained as recovery evidence. Review the session log first.

### Need a deeper diagnostic log

Change:

```json
"logging": {
  "level": "full"
}
```

For extension-excluded file paths as a separate diagnostic file, use:

```json
"logging": {
  "level": "full_with_excluded_file"
}
```

---

## Release status

**MediaCatalog 2.1.2**

The focused v2.1.2 acceptance covered:

- FFprobe short/full version logging,
- Normal multi-source scan,
- Force Reprobe All,
- Force Reprobe Single Source,
- append-only completed-source rendering,
- removal of terminal whitespace accumulation,
- expected scan result counters and normal session shutdown.

The wider v2.1.x behavior is inherited from the previously accepted modular release line.

