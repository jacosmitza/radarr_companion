================================================================================
Radarr Companion — Multi-Drive Movie Organiser, Collections & Maintenance Tool
================================================================================
Author: Jaco Smit & Google Gemini
Date: 2026-11-25
Target OS: Windows 10 / 11 Pro (64-bit)
Runtime: 100% Local-First (http://127.0.0.1:8085)

--------------------------------------------------------------------------------
1. PROJECT DESCRIPTION
--------------------------------------------------------------------------------
Radarr Companion is a 100% local-first Windows utility designed to extend
Radarr v3 for multi-drive movie libraries—especially setups using internal and
external hard drives, manual downloads, or the Torrent Blackhole workflow where
Radarr's native importer and collection tools often fall short.

It runs a local Python HTTP server (or standalone Windows 64-bit executable)
bound strictly to 127.0.0.1:8085, serving an offline, zero-dependency HTML5
dashboard styled to match Radarr's native dark theme.

Security & Privacy:
- Zero external CDNs, zero telemetry, zero analytics, and zero cloud uploads.
- Communicates solely with your configured local Radarr v3 HTTP API (never
  opens or locks Radarr's SQLite database file directly).
- All Radarr API keys saved in config.json are encrypted at rest using 64-bit
  Windows DPAPI (CryptProtectData), bound strictly to your Windows user account.
- Every file move, import, or quarantine enforces strict configured root-folder
  boundary checks and file-lock verification.

--------------------------------------------------------------------------------
2. KEY FEATURES
--------------------------------------------------------------------------------
1. Multi-Drive Awareness & Radarr Root Folder Sync
   - One-click sync of Radarr root folders (/api/v3/rootfolder) into Library
     Folders, paired with browsable Download / Intake folders and a browsable
     Default New-Movie Staging Folder.
   - Displays drive letter, Online/Offline status (safely skipping disconnected
     external drives), and free disk space (GB).
   - Native Windows Explorer Browse dialogs for Intake folders, Staging root,
     and Sysinternals contig64.exe.

2. Smart Duplicate Elimination & Radarr DB Relocation
   - Indexes Radarr's movie library (/api/v3/movie) to match duplicates by
     established folder path, TMDB ID, title/year, and alternate titles (with
     local filename/IMDb fallback).
   - Objective media quality scoring via pymediainfo (resolution, video codec,
     bit depth, HDR10/Dolby Vision, bitrate, audio codecs/channels, source tags,
     and file size tie-breaker).
   - Automatically relocates winning/replaced files into the exact folder listed
     by Radarr's database if found in another location.
   - Cross-drive verified copies (100 MB free-space safety buffer, .rc_tmp copy,
     exact byte-size verification, atomic replace) and safe quarantine via
     Windows Recycle Bin (send2trash) or drive-local _RadarrCompanion_Quarantine.
   - Auto-renames promoted/relocated files to match Radarr's /api/v3/config/naming
     format, with pre-approval filename previews and Approve All Safe Swaps.

3. Stranded Intake Triage (Blackhole / Manual Download Sweep)
   - Detects single-copy stranded movies left in Download / Intake folders.
   - Organises each movie into a clean "Movie Title (Year)" folder inside your
     browsable Default Staging Folder (or Radarr DB folder if matched).
   - Auto-renames to Radarr format, moves matching subtitles (.srt, .sub, .idx,
     .ass, .ssa, .vtt) preserving language suffixes, quarantines leftover
     torrent clutter (samples, .nfo, .txt) + removes emptied intake subfolders,
     and triggers Radarr RescanMovie.

4. Collections Manager (Native Radarr + Custom Collections)
   - Bulk Move Collection to a Single Root Folder using Windows' external
     robocopy.exe (/E /MOVE) command, updating Radarr's v3 API strictly AFTER
     all disk moves finish (with an interactive prompt to create missing target
     root folders on online drives).
   - "Not in Collection Root Only" filter button to isolate and move only
     misplaced movies outside a collection's root folder.
   - Bulk Delete unwanted collections with confirmation checkboxes for:
       * Quarantine / Recycle movie files on disk
       * Add movies to Radarr Import Exclusion list (prevents re-downloading)
     and automatic unmonitoring of native collections.
   - Custom Collections (e.g., "Marvel MCU Movies", "DCU Movies") stored locally
     in config.json with a searchable movie picker, multi-tag Radarr collection
     support ("Collect Movies from Selected Tags"), and optional Radarr tag sync
     via Radarr's batch /api/v3/movie/editor endpoint.

5. Library Maintenance (Spinning HDD Scheduled Defragmentation)
   - Auto-detects physical drive media types (HDD vs SSD) via Windows PowerShell
     Get-PhysicalDisk and strictly skips SSDs and network shares.
   - Clickable HDD/SSD toggle for "Unknown" drives (e.g., USB enclosures or RAID
     controllers), persisted in config.json.
   - Uses Sysinternals contig64.exe / contig.exe (file-level movie defrag) when
     available, with automatic fallback to Windows built-in defrag.exe.
   - In-app background scheduler supporting Daily, Weekly, Bi-Weekly, and
     Monthly schedules, live progress bar, and clean stop-after-current-item.

6. Reliability & Radarr Database Lock Protection
   - Serialised Radarr API write pacing (150 ms) with automatic retry/backoff on
     transient busy responses (HTTP 409/429/500/503) and consolidated rescans.
   - Port 8085 conflict detection on startup (opens existing instance cleanly).
   - Persistent on-drive activity log (radarr_companion.log) with configurable
     retention trimming (default 30 days).
   - Compiled standalone 64-bit Windows executable (dist\RadarrCompanion.exe)
     with bundled HTML5 UI, pymediainfo, and send2trash.

--------------------------------------------------------------------------------
3. QUICK START
--------------------------------------------------------------------------------
Option A — Standalone Executable (No Python Required):
  1. Run dist\RadarrCompanion.exe (opens http://127.0.0.1:8085 automatically).
  2. Open Settings -> enter your Radarr URL and API Key -> Save & Test.
  3. Open Library & Drives -> Sync Root Folders from Radarr and add your
     Download / Intake folder(s) and optional Default Staging Folder.

Option B — Run from Python Source (Python 3.9+):
  1. Double-click start_radarr_companion.bat (or run: python radarr_companion.py).
  2. Optional command-line flag: --no-browser (starts server without opening tab).

Note for GitHub Sharing:
  Do NOT commit your local config.json or radarr_companion.log files.

--------------------------------------------------------------------------------
4. VERSION HISTORY
--------------------------------------------------------------------------------
v1.0 (2026-11-26) — First Stable Release (Roll-Up of v0.1 through v0.15)
  - Consolidated all multi-drive duplicate elimination, Stranded Intake Triage
    (with automatic Radarr registration for unmapped staging folders),
    Collections Manager (Native + Custom Collections, custom-tagged exclusion
    from general scans, dedicated Custom Collection root folder creation,
    two-phase Robocopy bulk moves with monotonic progress tracking, bulk delete
    with quarantine + import exclusions, per-movie location ignore), Library
    Maintenance (spinning HDD defrag with SSD protection), selective folder
    Enable/Ignore controls, Missing-on-Online-Drive recovery prompts, unified
    FIFO Job Queue, live Drive Storage pie charts, Quarantine viewer/purger,
    and session replacement into the v1.0 release.

v0.15 (2026-11-26)
  - Custom Collections Exclusion from General Scan: Movies belonging to a Custom
    Collection (matched via Radarr tags or explicit TMDB IDs) are now
    automatically excluded from Native Radarr Collection scanning so general
    collections no longer report conflicting location warnings for custom-tagged
    franchises. Native collections whose movies are all managed by Custom
    Collections are hidden from the general tab and appear cleanly in Custom
    Collections.
  - Dedicated Custom Collection Root Folder Creator: Added "+ New Root" /
    "+ Create Root..." controls in the Custom Collections window (both in the
    Create/Edit form and on every Custom Collection card) to create a new folder
    on disk, register it as a Radarr Root Folder (/api/v3/rootfolder), add it to
    Library folders in config.json, and assign it directly to the collection.
  - Preserved custom collection root_folder when editing a Custom Collection.

v0.14 (2026-11-26)
  - Added selective per-folder "Active / Ignore" toggle buttons in Library &
    Drives (persisted in config.json under ignored_folders) so special folders
    like Waiting For / Missing / Unreleased are skipped by scans and prompts.
  - Expanded the Library & Drives modal (1280px wide, 460px scrollable lists).
  - Enhanced Intake Triage to also surface unmapped movies sitting in your
    Staging Root and automatically look up + register them in Radarr via
    /api/v3/movie/lookup and POST /api/v3/movie on import.

v0.13 (2026-11-26)
  - Stabilised Robocopy move progress percentages in the status bar and Job
    Progress panel by replacing erratic stdout percentage parsing with a
    monotonic destination byte-monitor thread that smoothly blends file transfer
    progress into overall job progress.

v0.12 (2026-11-26)
  - Startup now checks for existing Radarr Companion processes (RadarrCompanion.exe
    or radarr_companion.py), logs a clear warning, requests graceful shutdown via
    POST /api/shutdown, and terminates any lingering old session before binding
    port 8085.
  - Added server_instance_id heartbeat and cache-busted browser launch so open
    dashboard tabs automatically refresh cleanly whenever a new session starts.

v0.11 (2026-11-26)
  - Added FIFO action queuing for Duplicate Swaps, Intake Imports, and
    Collection Bulk Moves: clicking any action button immediately changes it to
    "Queued" (disabled) and lists it as QUEUED #N inside the Job Queue dialog
    while jobs execute sequentially without disk thrashing.

v0.10 (2026-11-26)
  - Added unified Job Queue & Activity History dialog (accessible from the
    sidebar and top status bar) tracking Drive Scans, Duplicate Swaps, Intake
    Imports, Collection Bulk Moves/Deletes, Quarantine Purges, HDD Maintenance,
    Radarr Root Sync, Missing Folder Resolves, and Log Trims.
  - Added live Activity pill in the top status bar showing the currently running
    job + percentage or the most recently completed activity.

v0.9 (2026-11-26)
  - Distinguished disconnected drives ("Drive Offline") from deleted/missing
    folders on an online drive ("Missing (Drive Online)").
  - Added Missing Folders prompt dialog & status-bar pill allowing the user to
    Recreate deleted folders or Ignore them (persisted in config.json under
    ignored_missing_folders, expanding config.json as the local state database).

v0.8 (2026-11-26)
  - Added per-movie "Ignore" / "Restore" button in Collections Manager to
    remember movies the user does not want reported as outside the collection's
    root folder (persisted in config.json under collection_location_ignores).
  - Changing a collection's root folder dropdown now immediately updates the
    collection's root (in Radarr for native collections or config.json for
    custom collections), scans that root folder, displays a confirmation banner
    on the card, and refreshes misplaced movie counts immediately.

v0.7 (2026-11-25)
  - Added live Current Action / Status panel on the main dashboard with real-time
    Overall Job Progress and File Transfer Progress bars + percentages
    (chunked cross-drive copy & live robocopy percentage tracking via /api/job_status).
  - Added 100x100 px SVG Drive Storage pie charts docked in the bottom-right of
    the main dashboard showing used vs. free space for each configured drive.
  - Added Offline Folders / Drives alert badge directly in the top status bar.
  - Sorted all folder lists and root folder dropdowns alphabetically by drive
    then path, and merged all local Library folders with Radarr root folders in
    dropdowns.
  - Hid Activity Log by default (toggleable via the sidebar Activity Log button).
  - Changed "Ko-fi" link label to "Donate".

v0.6 (2026-11-25)
  - Redesigned Dashboard UI to closely match Radarr v3's native dark theme
    (#202020 / #262626 charcoal surfaces, #ffc230 gold accents, fixed left
    navigation sidebar, sticky top search bar, and compact status ribbon).
  - Added Quarantine view & Purge dialog (individual and Purge All) with
    live sidebar item count and strict exclusion of quarantined files from
    duplicate comparisons.
  - Created Readme.txt with full project description, feature overview, and
    version history.
  - Added .gitignore to exclude local config.json, logs, build/dist artifacts,
    third-party Sysinternals binaries, and Python caches from Git commits.

v0.5 (2026-11-25)
  - Added "Not in Collection Root Only" filter toggle button in Collections
    Manager to show and move only misplaced movies outside a collection's root.
  - Switched Collection Bulk Move to use Windows' external robocopy.exe
    (/E /MOVE) command in a strict two-phase workflow (updates Radarr v3 API
    only after all disk moves complete).
  - Made the Default New-Movie Staging Folder completely generic and browsable
    (removed hardcoded folder names; added Browse... buttons in Intake queue
    and Library & Drives).

v0.4 (2026-11-25)
  - Hardened Radarr v3 API client against SQLite database lock contention:
    added write serialisation (RADARR_WRITE_LOCK), 150 ms pacing, automatic
    retry/backoff on HTTP 409/429/500/503, consolidated post-move RescanMovie,
    and batch PUT /api/v3/movie/editor tag syncing.
  - Added Feature 3: Stranded Intake Triage review queue with subtitle moving,
    torrent clutter quarantine (samples/.nfo/.txt), emptied subfolder cleanup,
    and Approve All Safe Imports.
  - Added confirmation dialog to create missing target root folders on online
    drives before bulk moving a collection.

v0.3 (2026-11-25)
  - Added Collections Manager (Native Radarr + Custom Collections) supporting
    Bulk Move to a single root, Bulk Delete with disk quarantine & Radarr Import
    Exclusion checkboxes, Custom Collections ("Marvel MCU Movies", "DCU Movies"),
    multi-tag Radarr collection ("Collect Movies from Selected Tags"), and live
    movie/collection search across the dashboard.
  - Enhanced duplicate resolution to relocate winning/replaced files into the
    exact folder listed by Radarr's database when located elsewhere.

v0.2 (2026-11-23)
  - Converted dashboard.html to semantic HTML5 with native <dialog>, <main>,
    <nav>, <section>, <article>, <form>, <output>, and <progress> elements and
    unobtrusive JavaScript event listeners.
  - Added Library Maintenance module for scheduled spinning-HDD defragmentation
    using Sysinternals contig64.exe with automatic Windows defrag.exe fallback,
    PowerShell Get-PhysicalDisk SSD protection, clickable HDD/SSD override for
    "Unknown" drives, and full maintenance config persistence in config.json.
  - Added native Windows Explorer Browse... dialogs for Intake folders and
    contig64.exe.
  - Compiled standalone 64-bit Windows executable (dist\RadarrCompanion.exe)
    with PyInstaller frozen-path support.

v0.1 (2026-11-23)
  - Initial release: Multi-drive duplicate scanner, Radarr v3 TMDB matching,
    pymediainfo stream quality scoring, cross-drive verified safe swap &
    quarantine, Feature 2 Radarr naming convention auto-rename, DPAPI API key
    encryption, retention-trimmed persistent logging, port 8085 conflict
    handling, and Clear Saved Key support.
================================================================================
