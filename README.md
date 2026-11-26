# Radarr Companion (v1.0)

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
- **Dedicated Custom Root Folder Creator:** Create a new folder on disk directly from the Custom Collections window, automatically register it in Radarr (`/api/v3/rootfolder`), add it to your Library folders, and assign it to the collection.
- **Two-Phase Bulk Move (`robocopy.exe`):** Safely moves entire collections (or only misplaced movies via the *Not in Collection Root Only* filter) with monotonic destination byte-progress tracking, updating Radarr v3 API strictly after disk moves finish.
- **Bulk Delete & Per-Movie Ignore:** Bulk delete with optional disk quarantine and Radarr Import Exclusion, plus per-movie *Ignore* rules remembered in `config.json`.

### 5. Library Maintenance (Spinning HDD Defragmentation)
- Detects physical drive media types via Windows PowerShell (`Get-PhysicalDisk`) and **strictly skips SSDs and unverified drives**.
- Supports scheduled or on-demand defragmentation using Sysinternals `contig64.exe` (file-level) with automatic fallback to Windows `defrag.exe` (drive-level).

### 6. Live Job Queue & Session Safety
- Unified FIFO action queue with `Queued` button states, live overall & per-file progress bars, and full activity history.
- Automatic port `8085` session conflict detection that gracefully stops an older session and auto-refreshes open dashboard tabs.

---

## Privacy & Security

- **100% Local-First:** Strictly bound to `127.0.0.1:8085`. Zero external CDNs, zero telemetry, zero analytics, and zero cloud uploads.
- **DPAPI-Encrypted API Keys:** Saved Radarr API keys in `config.json` are encrypted at rest using 64-bit Windows DPAPI (`CryptProtectData`), bound to your Windows user account.
- **API-Only Radarr Integration:** Communicates exclusively with your local Radarr v3 HTTP API using serialised writes (`RADARR_WRITE_LOCK`) and retry/backoff — never opens or locks `radarr.db`.
- **Path Boundary & File-Lock Guards:** Every move, import, or purge verifies configured root boundaries and checks for active file locks before touching disk.

---

## Quick Start

### Requirements
- **OS:** Windows 10 / 11 Pro (64-bit)
- **Python:** Python 3.9+
- **Dependencies:** `pymediainfo`, `send2trash`

```powershell
pip install pymediainfo send2trash
```

### Running from Source
1. Double-click `start_radarr_companion.bat` or run:
   ```powershell
   python radarr_companion.py
   ```
   *(Optional flag: `--no-browser` starts the server without opening a browser tab.)*
2. Your browser opens `http://127.0.0.1:8085` automatically.
3. Open **Settings** → enter your Radarr URL and API Key → **Save & Test**.
4. Open **Library & Drives** → **Sync Root Folders from Radarr**, add your Download/Intake folder(s), and choose a Default Staging Folder.

### Building a Standalone Executable (Optional)
```powershell
pip install pyinstaller
pyinstaller --clean --noconfirm RadarrCompanion.spec
```

---

## Support & Disclaimer

- Support development: [Donate on Ko-fi](https://ko-fi.com/mrdiealotza)
- *This project was created with AI assistance (Google Gemini) and independently verified for local-first execution and data minimisation. Always keep backups of critical media configurations.*
