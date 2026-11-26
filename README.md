# Radarr Companion (v1.5)

**A 100% local-first Windows 10/11 Pro utility extending Radarr v3 for multi-drive movie libraries, smart duplicate elimination, stranded intake triage, collection management, and spinning-HDD maintenance.**

> **Author:** Jaco Smit & Google Gemini  
> **Runtime:** Local-only Python HTTP server (`http://127.0.0.1:8085`) with an offline, zero-dependency semantic HTML5 dashboard styled to match Radarr v3's dark theme.  
> **User Manual:** [Download / View the PDF User Manual](Radarr_Companion_User_Manual.pdf)

---

## Why Radarr Companion?

Managing a large Radarr movie library spread across multiple internal and external hard drives — especially with manual downloads, Torrent Blackhole setups, or custom franchise folders (such as MCU / DCU) — often runs into gaps in Radarr's built-in tools. **Radarr Companion** fills those gaps safely without ever touching Radarr's SQLite database directly.

---

## Key Features

### 1. Multi-Drive Library Awareness & Selective Folder Controls
- **One-click Radarr Root Folder Sync** (`/api/v3/rootfolder`) plus browsable **Download / Intake** folders and a generic, browsable **Default Staging Folder**.
- **Selective Enable / Ignore Toggle:** Ignore special folders (e.g. *Waiting For*, *Missing*, *Downloading*) so they are skipped during scans and suppressed from offline alerts without removing them from your list.
- **Missing-on-Online-Drive Recovery:** Distinguishes between disconnected external drives and deleted folders on online drives, prompting you to recreate or ignore them.
- **Live Drive Storage Dock:** Compact SVG pie charts showing live used/free space across all mounted drives.

### 2. Smart Duplicate Elimination & Radarr DB Relocation
- Matches duplicates across all online drives using Radarr's TMDB IDs, established folder paths, title/year, and alternate titles.
- **Objective Quality Scoring (`pymediainfo`):** Evaluates resolution, video codec, bit depth, HDR10/Dolby Vision, bitrate, audio codecs/channels, source tags, and file size.
- **Safe Cross-Drive Swaps & Auto-Rename:** Uses a 100 MB free-space safety buffer, `.rc_tmp` size-verified copies, atomic replacement, and automatic renaming to match Radarr's `/api/v3/config/naming` convention.
- **Safe Quarantine:** Sends replaced files to the Windows Recycle Bin (`send2trash`) or a drive-local `_RadarrCompanion_Quarantine` folder, with a built-in Quarantine viewer and purger.

### 3. Stranded Intake Triage & Unmapped Folder Auto-Registration
- Detects stranded single-copy downloads and unmapped movies sitting in your staging folder.
- Automatically organises files into `Movie Title (Year)` folders, moves matching subtitles (`.srt`, `.sub`, `.idx`, `.ass`, `.ssa`, `.vtt`), quarantines leftover clutter (samples, `.nfo`, `.txt`), looks up unmapped movies via `/api/v3/movie/lookup`, registers them in Radarr (`searchForMovie: false`), and triggers `RescanMovie`.

### 4. Collections Manager (Native Radarr + Custom Collections)
- **Native & Custom Collections:** Manage Radarr's native TMDB collections alongside custom tag- or movie-based collections (e.g. *Marvel MCU Movies*, *DCU Movies*).
- **Custom-Tagged Exclusion:** Movies managed by Custom Collections are automatically excluded from general Native Collection scanning to prevent conflicting root-folder warnings.
- **One-Click `+ Create Folder` (Preset or Prompted Location):** Create a collection-named folder for any Native or Custom collection under a saved preset parent location or a browsed location, automatically register it in Radarr (`/api/v3/rootfolder`), add it to Library folders, and optionally move movies straight into it.
- **Two-Phase Bulk Move (`robocopy.exe`):** Safely moves entire collections (or only misplaced movies via the *Not in Collection Root Only* filter) with monotonic destination byte-progress tracking, updating Radarr v3 API strictly after disk moves finish.
- **Bulk Delete & Per-Movie Ignore:** Bulk delete with optional disk quarantine and Radarr Import Exclusion, plus per-movie *Ignore* rules remembered in `config.json`.

### 5. Library Maintenance (Spinning HDD Defragmentation)
- Detects physical drive media types via Windows PowerShell (`Get-PhysicalDisk`) and **strictly skips SSDs and unverified drives**.
- Supports scheduled or on-demand defragmentation using Sysinternals `contig64.exe` (file-level) with automatic fallback to Windows `defrag.exe` (drive-level).

### 6. Live Job Queue & Active-Job Shutdown Guard
- Unified FIFO action queue with `Queued` button states, live overall & per-file progress bars, and full activity history.
- **Corruption-Safe Session Guard:** Launching a second instance checks whether the existing session on port `8085` has active running jobs. If a job is in progress, it refuses silent termination (HTTP `409`) and prompts you to **Wait safely for completion (Recommended)**, **Force Close**, or **Cancel & keep existing**. Idle old sessions are replaced gracefully and open dashboard tabs auto-refresh.

---

## Privacy & Security

- **100% Local-First:** Strictly bound to `127.0.0.1:8085`. Zero external CDNs, zero telemetry, zero analytics, and zero cloud uploads.
- **DPAPI-Encrypted API Keys:** Saved Radarr API keys in `config.json` are encrypted at rest using 64-bit Windows DPAPI (`CryptProtectData`), bound to your Windows user account.
- **API-Only Radarr Integration:** Communicates exclusively with your local Radarr v3 HTTP API using serialised writes (`RADARR_WRITE_LOCK`) and retry/backoff — never opens or locks `radarr.db`.
- **Path Boundary & File-Lock Guards:** Every move, import, or purge verifies configured root boundaries and checks for active file locks before touching disk.

---

## Quick Start

### Option A — Run from Python Source (Python 3.9+)
1. Install dependencies:
   ```powershell
   pip install pymediainfo send2trash
   ```
2. Double-click `start_radarr_companion.bat` or run:
   ```powershell
   python radarr_companion.py
   ```
   *(Optional flag: `--no-browser` starts the server without opening a browser tab.)*
3. Your browser opens `http://127.0.0.1:8085` automatically.
4. Open **Settings** → enter your Radarr URL and API Key → **Save & Test**.
5. Open **Library & Drives** → **Sync Root Folders from Radarr**, add your Download/Intake folder(s), and choose a Default Staging Folder.

### Option B — Build a Standalone Windows Executable (Optional)
```powershell
pip install pyinstaller
pyinstaller --clean --noconfirm RadarrCompanion.spec
```
> **Note:** Never commit your local `config.json`, `radarr_companion.log`, or third-party Sysinternals binaries (`Contig64.exe`). All are excluded by `.gitignore`.

---

## Version History

- **v1.5 (2026-11-26)** — Replaced background Windows Explorer browse dialogs with an instant, Radarr-style **In-App Folder & File Browser** (`POST /api/browse_dir` + `POST /api/browse_mkdir`) featuring quick-jump drive buttons, free-space readouts, breadcrumb/path navigation, inline `+ New Folder` creation, and `.exe` file selection for Sysinternals `contig64.exe`.
- **v1.4 (2026-11-26)** — Made `+ Create Folder` immediately create the collection folder on disk, register it via Radarr `/api/v3/rootfolder`, assign it to the collection (`/api/v3/collection/{id}` or `config.json`), and immediately select the newly created path in that collection's root-folder dropdown (with trailing-slash-safe path matching). If no preset parent folder is set yet, it prompts via the Create Collection Folder dialog first.
- **v1.3 (2026-11-26)** — Added Active-Transfer Exclusion Guard (`track_active_transfer` + `.rc_tmp` sibling check) so files currently being moved by Collection Bulk Moves, Intake Imports, or Duplicate Swaps are never falsely detected as duplicate entries during a scan.
- **v1.2 (2026-11-26)** — Added Active-Job Shutdown Guard (`POST /api/shutdown` returns HTTP `409 Conflict` when a job or maintenance defrag is active unless `force: true`; launcher prompts user to **Wait (Recommended)**, **Force Close**, or **Cancel** via console or Windows dialog). Consolidated `Readme.txt` into `README.md` and removed the old `Readme.txt`.
- **v1.1 (2026-11-26)** — Added `+ Create Folder` button to every Native and Custom Collection card next to Bulk Move, persistent preset parent location (`collections_root_parent`), and one-click **Create & Move Movies**.
- **v1.0 (2026-11-26)** — First stable release rolling up `v0.1` through `v0.15`, with GitHub publication and PDF User Manual.
- **v0.15 (2026-11-26)** — Excluded custom-tagged collection movies from general Native Collection scanning; added dedicated Custom Collection root folder creator (`+ New Root` / `+ Create Root...`); preserved `root_folder` on custom collection edits.
- **v0.14 (2026-11-26)** — Added selective per-folder **Active / Ignore** toggle in Library & Drives (`ignored_folders`), expanded Library & Drives modal (`modal-xl`), and added automatic Radarr registration for unmapped movies sitting in the Staging Root.
- **v0.13 (2026-11-26)** — Stabilised Robocopy move progress percentages with a monotonic destination byte-monitor thread.
- **v0.12 (2026-11-26)** — Added startup detection and graceful replacement of existing Radarr Companion sessions plus `server_instance_id` heartbeat auto-refresh.
- **v0.11 (2026-11-26)** — Added FIFO action queuing with `Queued` button states and sequential execution.
- **v0.10 (2026-11-26)** — Added unified Job Queue & Activity History dialog and live top-bar Activity pill.
- **v0.9 (2026-11-26)** — Distinguished offline drives from missing folders on online drives with Recreate / Ignore prompts (`ignored_missing_folders`).
- **v0.8 (2026-11-26)** — Added per-movie **Ignore / Restore** location rules in Collections Manager and immediate root-folder scan on selection change.
- **v0.7 (2026-11-25)** — Added live Current Action progress panel, 100×100 px SVG Drive Storage pie charts, alphabetical drive+path sorting, hidden-by-default Activity Log, and "Donate" label.
- **v0.6 (2026-11-25)** — Radarr v3 dark-theme dashboard redesign, Quarantine viewer/purger, `.gitignore`, and initial documentation.
- **v0.5 (2026-11-25)** — Added *Not in Collection Root Only* filter, two-phase `robocopy.exe` bulk moves, and generic browsable Staging Folder.
- **v0.4 (2026-11-25)** — Hardened Radarr v3 API client (`RADARR_WRITE_LOCK`, retry/backoff), Stranded Intake Triage with subtitle move & clutter cleanup, and missing target root confirmation.
- **v0.3 (2026-11-25)** — Added Collections Manager (Native + Custom Collections, Bulk Move, Bulk Delete with quarantine + Import Exclusion, multi-tag picker, live search) and Radarr DB relocation on duplicate swaps.
- **v0.2 (2026-11-23)** — Semantic HTML5 conversion, Library Maintenance spinning-HDD defrag module with SSD protection, native Windows Browse dialogs, and PyInstaller support.
- **v0.1 (2026-11-23)** — Initial release: multi-drive duplicate scanner, TMDB matching, `pymediainfo` scoring, cross-drive safe swap & quarantine, Radarr naming auto-rename, DPAPI encryption, and retention-trimmed logging.

---

## Support & Disclaimer

- Support development: [Donate on Ko-fi](https://ko-fi.com/mrdiealotza)
- *This project was created with AI assistance (Google Gemini) and independently verified for local-first execution and data minimisation. Always keep backups of critical media configurations.*
