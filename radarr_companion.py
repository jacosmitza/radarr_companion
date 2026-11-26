# Author: Jaco Smit & Google Gemini
# Date: 2026-11-23

"""
Radarr Companion — Multi-Drive Movie Organiser, Smart Duplicate Eliminator & Auto-Renamer
Built for Windows 10 Pro. Runs 100% locally on 127.0.0.1:8085.

Security & Network Transparency:
- All API keys and secrets saved in config.json are encrypted at rest using Windows DPAPI
  (CryptProtectData), bound strictly to the current Windows user account.
- Activity logs are stored locally in radarr_companion.log and automatically trimmed
  to the user-configured retention period (default 30 days). No secrets are logged.
- Communicates solely with your configured local Radarr v3 instance
  (/api/v3/system/status, /api/v3/rootfolder, /api/v3/movie,
  /api/v3/config/naming, /api/v3/command).
- On first run only, invokes pip to install 'pymediainfo' and 'send2trash' if missing.
- No telemetry, analytics, or external cloud uploads.
"""

import subprocess
import sys


# --- AUTOMATED DEPENDENCY BOOTSTRAPPER ---
def ensure_package(import_name, pip_name=None):
    pip_target = pip_name or import_name
    try:
        __import__(import_name)
        return True
    except ImportError:
        if getattr(sys, "frozen", False):
            print(f"[!] Package '{pip_target}' is not bundled in this executable. Continuing with fallback mode.")
            return False
        print(f"[*] Installing missing package '{pip_target}' via pip...")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", pip_target])
            return True
        except Exception as exc:
            print(f"[!] Could not install '{pip_target}': {exc}. Continuing with fallback mode.")
            return False


HAS_MEDIAINFO = ensure_package("pymediainfo")
HAS_SEND2TRASH = ensure_package("send2trash")

import base64
import ctypes
import json
import os
import re
import shutil
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from collections import defaultdict
from ctypes import wintypes
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

if HAS_MEDIAINFO:
    try:
        from pymediainfo import MediaInfo
    except Exception:
        HAS_MEDIAINFO = False

if HAS_SEND2TRASH:
    try:
        from send2trash import send2trash
    except Exception:
        HAS_SEND2TRASH = False


# --- CONFIGURATION & PORTABLE PATHS ---
if getattr(sys, "frozen", False):
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
    RESOURCE_DIR = getattr(sys, "_MEIPASS", BASE_DIR)
else:
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    RESOURCE_DIR = BASE_DIR

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")
LOG_FILE = os.path.join(BASE_DIR, "radarr_companion.log")
HTML_FILE = os.path.join(BASE_DIR, "dashboard.html") if os.path.exists(os.path.join(BASE_DIR, "dashboard.html")) else os.path.join(RESOURCE_DIR, "dashboard.html")
HTTP_HOST = "127.0.0.1"
HTTP_PORT = 8085
SERVER_INSTANCE_ID = f"{os.getpid()}-{int(time.time() * 1000)}"
ACTIVE_HTTP_SERVER = None

VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}
SUBTITLE_EXTENSIONS = {".srt", ".sub", ".idx", ".ass", ".ssa", ".vtt"}
MIN_MOVIE_SIZE_BYTES = 50 * 1024 * 1024  # 50 MB threshold to ignore samples/trailers
CROSS_DRIVE_FREE_BUFFER_BYTES = 100 * 1024 * 1024  # 100 MB safety buffer

DEFAULT_CONFIG = {
    "library_folders": [],
    "download_folders": [],
    "staging_folder": "",  # Optional default staging root folder for new intake movies
    "radarr_url": "http://127.0.0.1:7878",
    "radarr_api_key": "",
    "quarantine_mode": "recycle_bin",  # "recycle_bin" or "drive_quarantine"
    "auto_rename_promoted": True,
    "cached_naming_format": "",
    "log_retention_days": 30,
    "maintenance_enabled": False,
    "maintenance_schedule": "weekly",  # "daily", "weekly", "biweekly", "monthly"
    "maintenance_day_of_week": "Sunday",
    "maintenance_day_of_month": 1,
    "maintenance_time": "03:00",
    "contig_path": "",
    "maintenance_engine": "auto",  # "contig", "windows_defrag", or "auto"
    "maintenance_engine_path": "",
    "maintenance_drive_media_types": {},  # e.g. {"C:": "SSD", "D:": "HDD"}
    "maintenance_drive_overrides": {},  # User overrides for Unknown drives: {"E:": "HDD"|"SSD"}
    "maintenance_last_run": "",
    "custom_collections": [],  # [{"id": "mcu", "name": "Marvel MCU Movies", "tmdb_ids": [...], "radarr_tags": [...], "root_folder": ""}]
    "collection_location_ignores": [],  # ["native:123:456", "custom:mcu:1726"] movies ignored for misplaced checks
    "collections_root_parent": "",  # Optional preset parent folder where new collection root folders are created
    "ignored_missing_folders": [],  # Configured folders deleted on an online drive that user chose to ignore
    "ignored_folders": []  # Configured folders selectively ignored by the user (e.g. Waiting For / zz Missing)
}

LOG_LOCK = threading.Lock()
RADARR_WRITE_LOCK = threading.Lock()
ACTIVITY_LOG = []
LOG_LINE_REGEX = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] (.*)$")

JOB_LOCK = threading.Lock()
JOB_SEQ = 0
JOB_HISTORY = []  # Most recent first, capped at 100 entries
ACTIVE_TRANSFER_LOCK = threading.Lock()
ACTIVE_TRANSFER_PATHS = {}  # normcase(abspath) -> refcount of active moves/copies


class _ActiveTransferTracker:
    """Context manager registering source & destination paths while a move/copy is in flight."""
    def __init__(self, paths):
        self._keys = [
            os.path.normcase(os.path.abspath(str(p)))
            for p in paths
            if p and str(p).strip()
        ]

    def __enter__(self):
        with ACTIVE_TRANSFER_LOCK:
            for k in self._keys:
                ACTIVE_TRANSFER_PATHS[k] = ACTIVE_TRANSFER_PATHS.get(k, 0) + 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        with ACTIVE_TRANSFER_LOCK:
            for k in self._keys:
                cnt = ACTIVE_TRANSFER_PATHS.get(k, 0) - 1
                if cnt <= 0:
                    ACTIVE_TRANSFER_PATHS.pop(k, None)
                else:
                    ACTIVE_TRANSFER_PATHS[k] = cnt
        return False


def track_active_transfer(*paths):
    """Tracks one or more file/folder paths as actively being moved so scans exclude them."""
    return _ActiveTransferTracker(paths)


def is_path_in_active_transfer(path):
    """Returns True if path matches or lies inside any folder/file currently being moved."""
    if not path:
        return False
    norm_p = os.path.normcase(os.path.abspath(str(path)))
    with ACTIVE_TRANSFER_LOCK:
        active_keys = list(ACTIVE_TRANSFER_PATHS.keys())
    for k in active_keys:
        if norm_p == k or norm_p.startswith(k + os.sep):
            return True
    return False
JOB_STATE = {
    "active": False,
    "job_id": None,
    "job_name": "",
    "status": "Idle",
    "overall_current": 0,
    "overall_total": 0,
    "overall_percent": 0,
    "current_file": "",
    "file_percent": 0,
    "file_copied_mb": 0.0,
    "file_total_mb": 0.0
}


def begin_job(job_name, status_text, total=1):
    """Registers a new active job in JOB_STATE and JOB_HISTORY."""
    global JOB_SEQ
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with JOB_LOCK:
        JOB_SEQ += 1
        jid = JOB_SEQ
        entry = {
            "id": jid,
            "name": job_name,
            "state": "running",
            "detail": status_text,
            "percent": 0,
            "started_at": now_str,
            "finished_at": ""
        }
        JOB_HISTORY.insert(0, entry)
        if len(JOB_HISTORY) > 100:
            JOB_HISTORY.pop()
        JOB_STATE.update({
            "active": True,
            "job_id": jid,
            "job_name": job_name,
            "status": status_text,
            "overall_current": 0,
            "overall_total": total,
            "overall_percent": 0,
            "current_file": "",
            "file_percent": 0,
            "file_copied_mb": 0.0,
            "file_total_mb": 0.0
        })
        return jid


def update_job_progress(**kwargs):
    """Updates the live shared job & file transfer progress state polled by the dashboard."""
    with JOB_LOCK:
        JOB_STATE.update(kwargs)
        total = int(JOB_STATE.get("overall_total") or 0)
        cur = int(JOB_STATE.get("overall_current") or 0)
        if "overall_percent" not in kwargs:
            if total > 0:
                JOB_STATE["overall_percent"] = max(0, min(100, int(round((cur / total) * 100))))
            elif not JOB_STATE.get("active"):
                JOB_STATE["overall_percent"] = 0
        jid = JOB_STATE.get("job_id")
        if jid is not None:
            for item in JOB_HISTORY:
                if item["id"] == jid:
                    item["detail"] = JOB_STATE.get("status") or item["detail"]
                    item["percent"] = JOB_STATE.get("overall_percent", 0)
                    break


def finish_job(job_id=None, final_status="Completed", state="completed"):
    """Marks the specified (or active) job finished in JOB_STATE and JOB_HISTORY."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with JOB_LOCK:
        target_id = job_id if job_id is not None else JOB_STATE.get("job_id")
        if target_id is not None:
            for item in JOB_HISTORY:
                if item["id"] == target_id:
                    item["state"] = state
                    item["detail"] = final_status
                    item["percent"] = 100 if state == "completed" else item.get("percent", 0)
                    item["finished_at"] = now_str
                    break
        JOB_STATE.update({
            "active": False,
            "status": final_status,
            "overall_percent": 100 if state == "completed" else JOB_STATE.get("overall_percent", 0),
            "current_file": "",
            "file_percent": 100 if state == "completed" else 0
        })


def reset_job_progress(final_status="Idle", state="completed"):
    """Backward-compatible wrapper finishing the current active job."""
    finish_job(job_id=None, final_status=final_status, state=state)


def sort_paths_by_drive_and_path(paths):
    """Sorts filesystem paths alphabetically by drive label first, then full path."""
    return sorted(paths, key=lambda p: (get_drive_label(p).lower(), os.path.normpath(p).lower()))


def copy_file_with_progress(src_path, dst_path, label=""):
    """Copies a file in 4 MiB chunks while updating live JOB_STATE file_percent."""
    src_lp = win_long_path(src_path)
    dst_lp = win_long_path(dst_path)
    total_bytes = os.path.getsize(src_lp)
    copied = 0
    chunk_size = 4 * 1024 * 1024  # 4 MiB
    display_name = label or os.path.basename(src_path)
    total_mb = round(total_bytes / (1024 ** 2), 1)
    update_job_progress(
        current_file=display_name,
        file_percent=0,
        file_copied_mb=0.0,
        file_total_mb=total_mb
    )
    with open(src_lp, "rb") as fsrc, open(dst_lp, "wb") as fdst:
        while True:
            buf = fsrc.read(chunk_size)
            if not buf:
                break
            fdst.write(buf)
            copied += len(buf)
            pct = int(round((copied / total_bytes) * 100)) if total_bytes > 0 else 100
            update_job_progress(
                current_file=display_name,
                file_percent=min(100, pct),
                file_copied_mb=round(copied / (1024 ** 2), 1),
                file_total_mb=total_mb
            )
    shutil.copystat(src_lp, dst_lp)
    update_job_progress(file_percent=100)

MAINTENANCE_LOCK = threading.Lock()
MAINTENANCE_STOP_EVENT = threading.Event()
MAINTENANCE_STATE = {
    "running": False,
    "started_at": None,
    "current_drive": "",
    "current_file": "",
    "processed_files": 0,
    "total_files": 0,
    "defragged_files": 0,
    "skipped_locked": 0,
    "skipped_ssd_drives": [],
    "last_result": "Idle",
    "last_run_at": ""
}
DRIVE_MEDIA_CACHE = {}
DRIVE_MEDIA_CACHE_TS = 0.0
VALID_SCHEDULES = {"daily", "weekly", "biweekly", "monthly"}
VALID_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


# --- PERSISTENT ON-DRIVE LOGGING & RETENTION TRIMMING ---
def clamp_retention_days(val):
    try:
        days = int(val)
    except (TypeError, ValueError):
        days = 30
    return max(1, min(days, 3650))


def log_event(message, level="INFO"):
    """Appends a timestamped entry to radarr_companion.log on disk and in-memory buffer."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    clean_msg = str(message).replace("\r", " ").replace("\n", " ")
    line = f"[{now_str}] [{level}] {clean_msg}"
    entry = {"time": now_str, "level": level, "message": clean_msg}

    with LOG_LOCK:
        ACTIVITY_LOG.insert(0, entry)
        if len(ACTIVITY_LOG) > 250:
            ACTIVITY_LOG.pop()
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as exc:
            print(f"[!] Could not write to log file: {exc}")

    print(line)


def trim_log_file(retention_days):
    """
    Trims radarr_companion.log on disk to keep only entries within retention_days
    and refreshes the in-memory ACTIVITY_LOG buffer from disk.
    """
    days = clamp_retention_days(retention_days)
    cutoff = datetime.now() - timedelta(days=days)
    kept_lines = []
    parsed_entries = []
    removed_count = 0

    with LOG_LOCK:
        if os.path.exists(LOG_FILE):
            try:
                with open(LOG_FILE, "r", encoding="utf-8", errors="replace") as f:
                    for raw_line in f:
                        stripped = raw_line.strip()
                        if not stripped:
                            continue
                        match = LOG_LINE_REGEX.match(stripped)
                        if match:
                            ts_str, lvl, msg = match.groups()
                            try:
                                entry_dt = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
                                if entry_dt < cutoff:
                                    removed_count += 1
                                    continue
                            except ValueError:
                                pass
                            kept_lines.append(stripped)
                            parsed_entries.append({"time": ts_str, "level": lvl, "message": msg})
                        else:
                            kept_lines.append(stripped)

                tmp_log = LOG_FILE + ".tmp"
                with open(tmp_log, "w", encoding="utf-8") as f:
                    if kept_lines:
                        f.write("\n".join(kept_lines) + "\n")
                os.replace(tmp_log, LOG_FILE)
            except OSError as exc:
                print(f"[!] Failed to trim log file: {exc}")

        ACTIVITY_LOG.clear()
        for entry in reversed(parsed_entries[-250:]):
            ACTIVITY_LOG.append(entry)

    return removed_count, len(kept_lines)


def open_dashboard_browser(url):
    """Launches the local dashboard in the system's default web browser."""
    try:
        webbrowser.open(url, new=2)
        log_event(f"Launched default web browser at {url}", "INFO")
    except Exception as exc:
        log_event(f"Could not auto-launch browser (open {url} manually): {exc}", "WARN")


# --- WINDOWS DPAPI CREDENTIAL ENCRYPTION (64-BIT SAFE) ---
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char))
    ]


if os.name == "nt":
    _crypt32 = ctypes.windll.crypt32
    _kernel32 = ctypes.windll.kernel32

    _crypt32.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        wintypes.LPCWSTR,
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptProtectData.restype = wintypes.BOOL

    _crypt32.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB),
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(DATA_BLOB),
    ]
    _crypt32.CryptUnprotectData.restype = wintypes.BOOL

    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL


def dpapi_encrypt(plaintext):
    """Encrypts a secret string using Windows DPAPI (bound to current Windows user)."""
    if not plaintext:
        return ""
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI encryption requires Windows.")

    raw = plaintext.encode("utf-8")
    buf = ctypes.create_string_buffer(raw, len(raw))
    blob_in = DATA_BLOB(len(raw), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not _crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        "RadarrCompanionSecret",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out)
    ):
        raise OSError(f"Windows DPAPI CryptProtectData failed (WinError {ctypes.GetLastError()}).")

    try:
        encrypted_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return "dpapi:" + base64.b64encode(encrypted_bytes).decode("ascii")
    finally:
        if blob_out.pbData:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def dpapi_decrypt(token):
    """Decrypts a Windows DPAPI-protected secret string."""
    if not token:
        return ""
    if not token.startswith("dpapi:"):
        return token
    if os.name != "nt":
        raise RuntimeError("Windows DPAPI decryption requires Windows.")

    encrypted_bytes = base64.b64decode(token[6:].encode("ascii"))
    buf = ctypes.create_string_buffer(encrypted_bytes, len(encrypted_bytes))
    blob_in = DATA_BLOB(len(encrypted_bytes), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = DATA_BLOB()

    if not _crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None,
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(blob_out)
    ):
        raise OSError(f"Windows DPAPI CryptUnprotectData failed (WinError {ctypes.GetLastError()}).")

    try:
        decrypted_bytes = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return decrypted_bytes.decode("utf-8")
    finally:
        if blob_out.pbData:
            _kernel32.LocalFree(ctypes.cast(blob_out.pbData, ctypes.c_void_p))


def normalise_maintenance_config(cfg):
    """Validates and normalises Library Maintenance schedule fields."""
    sched = str(cfg.get("maintenance_schedule") or "weekly").strip().lower()
    if sched not in VALID_SCHEDULES:
        sched = "weekly"

    dow = str(cfg.get("maintenance_day_of_week") or "Sunday").strip().title()
    if dow not in VALID_WEEKDAYS:
        dow = "Sunday"

    try:
        dom = int(cfg.get("maintenance_day_of_month", 1))
    except (TypeError, ValueError):
        dom = 1
    dom = max(1, min(dom, 28))

    time_raw = str(cfg.get("maintenance_time") or "03:00").strip()
    if not re.match(r"^(?:[01]\d|2[0-3]):[0-5]\d$", time_raw):
        time_raw = "03:00"

    raw_media_types = cfg.get("maintenance_drive_media_types")
    clean_media_types = {}
    if isinstance(raw_media_types, dict):
        for drv, mtype in raw_media_types.items():
            drv_key = str(drv or "").strip().upper()
            mt_val = str(mtype or "").strip()
            if drv_key and mt_val in ("HDD", "SSD", "Network", "Unknown"):
                clean_media_types[drv_key] = mt_val

    raw_overrides = cfg.get("maintenance_drive_overrides")
    clean_overrides = {}
    if isinstance(raw_overrides, dict):
        for drv, mtype in raw_overrides.items():
            drv_key = str(drv or "").strip().upper()
            mt_val = str(mtype or "").strip().upper()
            if drv_key and mt_val in ("HDD", "SSD"):
                clean_overrides[drv_key] = mt_val

    engine_val = str(cfg.get("maintenance_engine") or "auto").strip().lower()
    if engine_val not in ("auto", "contig", "windows_defrag", "none"):
        engine_val = "auto"

    return {
        "maintenance_enabled": bool(cfg.get("maintenance_enabled", False)),
        "maintenance_schedule": sched,
        "maintenance_day_of_week": dow,
        "maintenance_day_of_month": dom,
        "maintenance_time": time_raw,
        "contig_path": str(cfg.get("contig_path") or "").strip(),
        "maintenance_engine": engine_val,
        "maintenance_engine_path": str(cfg.get("maintenance_engine_path") or "").strip(),
        "maintenance_drive_media_types": clean_media_types,
        "maintenance_drive_overrides": clean_overrides,
        "maintenance_last_run": str(cfg.get("maintenance_last_run") or "").strip()
    }


def normalise_custom_collections(raw_list):
    """Validates and normalises user-defined custom collections stored in config.json."""
    result = []
    seen_ids = set()
    if not isinstance(raw_list, list):
        return result
    for entry in raw_list:
        if not isinstance(entry, dict):
            continue
        name = str(entry.get("name") or "").strip()
        if not name:
            continue
        cid = str(entry.get("id") or "").strip().lower()
        if not cid:
            cid = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or f"custom-{len(result) + 1}"
        if cid in seen_ids:
            cid = f"{cid}-{len(result) + 1}"
        seen_ids.add(cid)
        raw_tmdbs = entry.get("tmdb_ids") or []
        tmdb_ids = []
        seen_t = set()
        for tid in raw_tmdbs:
            s = str(tid).strip()
            if s.isdigit() and s not in seen_t:
                seen_t.add(s)
                tmdb_ids.append(s)
        tag = re.sub(r"[^a-z0-9_-]", "", str(entry.get("radarr_tag") or "").strip().lower())
        raw_tags = entry.get("radarr_tags") or []
        radarr_tags = []
        seen_tags = set()
        if isinstance(raw_tags, list):
            for t in raw_tags:
                ct = re.sub(r"[^a-z0-9_-]", "", str(t or "").strip().lower())
                if ct and ct not in seen_tags:
                    seen_tags.add(ct)
                    radarr_tags.append(ct)
        if tag and tag not in seen_tags:
            radarr_tags.insert(0, tag)
        primary_tag = radarr_tags[0] if radarr_tags else ""
        raw_root = str(entry.get("root_folder") or "").strip()
        result.append({
            "id": cid,
            "name": name,
            "tmdb_ids": tmdb_ids,
            "radarr_tag": primary_tag,
            "radarr_tags": radarr_tags,
            "root_folder": os.path.normpath(raw_root) if raw_root else ""
        })
    return result


def normalise_collection_ignores(raw_list):
    """Normalises the list of ignored collection:tmdb_id keys."""
    seen = set()
    out = []
    if not isinstance(raw_list, list):
        return out
    for item in raw_list:
        s = str(item or "").strip().lower()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def load_config():
    """Loads config.json (BOM-safe), decrypting the Radarr API key into memory only."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)

            merged = dict(DEFAULT_CONFIG)
            merged.update(data)
            merged["log_retention_days"] = clamp_retention_days(merged.get("log_retention_days", 30))
            merged.update(normalise_maintenance_config(merged))
            merged["custom_collections"] = normalise_custom_collections(merged.get("custom_collections", []))
            merged["collection_location_ignores"] = normalise_collection_ignores(merged.get("collection_location_ignores", []))
            raw_coll_parent = str(merged.get("collections_root_parent") or "").strip()
            merged["collections_root_parent"] = os.path.normpath(raw_coll_parent) if raw_coll_parent else ""
            merged["ignored_missing_folders"] = [
                os.path.normpath(str(p).strip())
                for p in (merged.get("ignored_missing_folders") or [])
                if str(p).strip()
            ]
            merged["ignored_folders"] = [
                os.path.normpath(str(p).strip())
                for p in (merged.get("ignored_folders") or [])
                if str(p).strip()
            ]

            needs_migration = False
            enc_key = str(data.get("radarr_api_key_encrypted") or "").strip()
            legacy_plain = str(data.get("radarr_api_key") or "").strip()

            if enc_key:
                try:
                    merged["radarr_api_key"] = dpapi_decrypt(enc_key)
                except Exception as exc:
                    log_event(f"Could not decrypt saved Radarr API key (please re-enter it): {exc}", "WARN")
                    merged["radarr_api_key"] = ""
            elif legacy_plain:
                merged["radarr_api_key"] = legacy_plain
                needs_migration = True
            else:
                merged["radarr_api_key"] = ""

            merged.pop("radarr_api_key_encrypted", None)

            if needs_migration:
                log_event("Migrating legacy plaintext API key in config.json to Windows DPAPI encryption.", "INFO")
                save_config(merged)

            return merged
        except Exception as exc:
            log_event(f"Failed to read {CONFIG_FILE}, using defaults: {exc}", "ERROR")
    return dict(DEFAULT_CONFIG)


def save_config(cfg):
    """Saves config.json atomically with the Radarr API key strictly encrypted via Windows DPAPI."""
    plain_api_key = str(cfg.get("radarr_api_key") or "").strip()
    encrypted_api_key = dpapi_encrypt(plain_api_key) if plain_api_key else ""
    retention_days = clamp_retention_days(cfg.get("log_retention_days", 30))
    maint = normalise_maintenance_config(cfg)

    raw_staging = str(cfg.get("staging_folder") or "").strip()
    staging_folder = os.path.normpath(raw_staging) if raw_staging else ""

    disk_payload = {
        "library_folders": [os.path.normpath(p.strip()) for p in cfg.get("library_folders", []) if p.strip()],
        "download_folders": [os.path.normpath(p.strip()) for p in cfg.get("download_folders", []) if p.strip()],
        "staging_folder": staging_folder,
        "radarr_url": str(cfg.get("radarr_url") or "http://127.0.0.1:7878").strip().rstrip("/"),
        "radarr_api_key_encrypted": encrypted_api_key,
        "quarantine_mode": cfg.get("quarantine_mode", "recycle_bin"),
        "auto_rename_promoted": bool(cfg.get("auto_rename_promoted", True)),
        "cached_naming_format": str(cfg.get("cached_naming_format") or "").strip(),
        "log_retention_days": retention_days,
        "maintenance_enabled": maint["maintenance_enabled"],
        "maintenance_schedule": maint["maintenance_schedule"],
        "maintenance_day_of_week": maint["maintenance_day_of_week"],
        "maintenance_day_of_month": maint["maintenance_day_of_month"],
        "maintenance_time": maint["maintenance_time"],
        "contig_path": maint["contig_path"],
        "maintenance_engine": maint["maintenance_engine"],
        "maintenance_engine_path": maint["maintenance_engine_path"],
        "maintenance_drive_media_types": maint["maintenance_drive_media_types"],
        "maintenance_drive_overrides": maint["maintenance_drive_overrides"],
        "maintenance_last_run": maint["maintenance_last_run"],
        "custom_collections": normalise_custom_collections(cfg.get("custom_collections", [])),
        "collection_location_ignores": normalise_collection_ignores(cfg.get("collection_location_ignores", [])),
        "collections_root_parent": os.path.normpath(str(cfg.get("collections_root_parent") or "").strip()) if str(cfg.get("collections_root_parent") or "").strip() else "",
        "ignored_missing_folders": [
            os.path.normpath(str(p).strip())
            for p in (cfg.get("ignored_missing_folders") or [])
            if str(p).strip()
        ],
        "ignored_folders": [
            os.path.normpath(str(p).strip())
            for p in (cfg.get("ignored_folders") or [])
            if str(p).strip()
        ]
    }

    tmp_cfg = CONFIG_FILE + ".tmp"
    with open(tmp_cfg, "w", encoding="utf-8") as f:
        json.dump(disk_payload, f, indent=4)
    os.replace(tmp_cfg, CONFIG_FILE)

    runtime_cfg = dict(disk_payload)
    runtime_cfg.pop("radarr_api_key_encrypted", None)
    runtime_cfg["radarr_api_key"] = plain_api_key
    return runtime_cfg


def win_long_path(path):
    """Applies Windows extended-length path prefix when paths exceed 240 characters."""
    norm = os.path.abspath(path)
    if os.name == "nt" and len(norm) >= 240 and not norm.startswith("\\\\?\\"):
        if norm.startswith("\\\\"):
            return "\\\\?\\UNC\\" + norm[2:]
        return "\\\\?\\" + norm
    return norm


def get_drive_label(path):
    drive, _ = os.path.splitdrive(os.path.abspath(path))
    if drive:
        return drive.upper()
    if path.startswith("\\\\"):
        parts = path.strip("\\").split("\\")
        if len(parts) >= 2:
            return f"\\\\{parts[0]}\\{parts[1]}"
    return "LOCAL"


def is_same_drive(path_a, path_b):
    return get_drive_label(path_a).lower() == get_drive_label(path_b).lower()


def is_file_locked(filepath):
    """Tests whether Windows or another process (Plex/Jellyfin/qBittorrent) holds a lock."""
    try:
        with open(win_long_path(filepath), "r+b"):
            return False
    except PermissionError:
        return True
    except OSError:
        return True


def is_path_within_configured_roots(target_path, cfg):
    """Security guard ensuring file actions only occur within user-configured folders."""
    norm_target = os.path.normcase(os.path.abspath(target_path))
    all_roots = list(cfg.get("library_folders", [])) + list(cfg.get("download_folders", []))
    staging = str(cfg.get("staging_folder") or "").strip()
    if staging:
        all_roots.append(staging)
    for root in all_roots:
        norm_root = os.path.normcase(os.path.abspath(root))
        try:
            if os.path.commonpath([norm_target, norm_root]) == norm_root:
                return True
        except ValueError:
            continue
    return False


def is_drive_mounted(path):
    """Returns True if the underlying drive/share for path is currently mounted/online."""
    abs_p = os.path.abspath(path)
    drive_root, _ = os.path.splitdrive(abs_p)
    if drive_root:
        return os.path.isdir(drive_root + os.sep)
    if path.startswith("\\\\"):
        parts = path.strip("\\").split("\\")
        if len(parts) >= 2:
            return os.path.isdir(f"\\\\{parts[0]}\\{parts[1]}")
    return os.path.isdir(os.path.dirname(abs_p))


def get_folder_statuses(cfg):
    """
    Returns sorted Online/Offline state, free/total space (GB), unique drive summaries,
    offline drives, and folders missing/deleted on an online drive.
    """
    result = {
        "library_folders": [],
        "download_folders": [],
        "drives": [],
        "offline_folders": [],
        "missing_on_online_drive": []
    }
    drives_map = {}
    ignored_missing_norm = {
        os.path.normcase(os.path.normpath(p))
        for p in (cfg.get("ignored_missing_folders") or [])
        if str(p).strip()
    }
    ignored_folders_norm = {
        os.path.normcase(os.path.normpath(p))
        for p in (cfg.get("ignored_folders") or [])
        if str(p).strip()
    }

    for key in ("library_folders", "download_folders"):
        sorted_folders = sort_paths_by_drive_and_path(cfg.get(key, []))
        for folder in sorted_folders:
            online = os.path.isdir(folder)
            drive = get_drive_label(folder)
            drive_online = True if online else is_drive_mounted(folder)
            norm_f = os.path.normcase(os.path.normpath(folder))
            is_user_ignored = norm_f in ignored_folders_norm
            is_ignored_missing = (not online) and drive_online and (norm_f in ignored_missing_norm or is_user_ignored)
            is_missing_prompt = (not online) and drive_online and (not is_ignored_missing)

            free_gb = None
            total_gb = None
            used_gb = None
            usage_target = folder if online else ((drive + os.sep) if (drive_online and drive.endswith(":")) else "")
            if usage_target:
                try:
                    usage = shutil.disk_usage(usage_target)
                    free_gb = round(usage.free / (1024 ** 3), 2)
                    total_gb = round(usage.total / (1024 ** 3), 2)
                    used_gb = round((usage.total - usage.free) / (1024 ** 3), 2)
                except Exception:
                    pass

            if not online and not is_user_ignored:
                if drive_online:
                    if is_missing_prompt:
                        result["missing_on_online_drive"].append({"path": folder, "drive": drive, "role": key})
                else:
                    result["offline_folders"].append({"path": folder, "drive": drive, "role": key})

            result[key].append({
                "path": folder,
                "drive": drive,
                "online": online,
                "drive_online": drive_online,
                "ignored": is_user_ignored,
                "missing_on_online_drive": (not online) and drive_online,
                "ignored_missing": is_ignored_missing,
                "free_gb": free_gb,
                "total_gb": total_gb
            })

            drv_key = drive.upper()
            if drv_key not in drives_map:
                drives_map[drv_key] = {
                    "drive": drive,
                    "online": drive_online,
                    "free_gb": free_gb,
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "used_pct": round((used_gb / total_gb) * 100, 1) if (total_gb and used_gb is not None) else 0.0
                }
            elif drive_online and not drives_map[drv_key]["online"]:
                drives_map[drv_key].update({
                    "online": True,
                    "free_gb": free_gb,
                    "total_gb": total_gb,
                    "used_gb": used_gb,
                    "used_pct": round((used_gb / total_gb) * 100, 1) if (total_gb and used_gb is not None) else 0.0
                })

    # Also include staging_folder's drive if configured
    staging = str(cfg.get("staging_folder") or "").strip()
    if staging:
        drv = get_drive_label(staging)
        drv_key = drv.upper()
        online = os.path.isdir(staging)
        drive_online = True if online else is_drive_mounted(staging)
        norm_s = os.path.normcase(os.path.normpath(staging))
        if not online:
            if drive_online:
                if norm_s not in ignored_missing_norm and not any(m["path"].lower() == staging.lower() for m in result["missing_on_online_drive"]):
                    result["missing_on_online_drive"].append({"path": staging, "drive": drv, "role": "staging_folder"})
            elif not any(o["path"].lower() == staging.lower() for o in result["offline_folders"]):
                result["offline_folders"].append({"path": staging, "drive": drv, "role": "staging_folder"})
        if drv_key not in drives_map:
            free_gb = total_gb = used_gb = None
            usage_target = staging if online else ((drv + os.sep) if (drive_online and drv.endswith(":")) else "")
            if usage_target:
                try:
                    usage = shutil.disk_usage(usage_target)
                    free_gb = round(usage.free / (1024 ** 3), 2)
                    total_gb = round(usage.total / (1024 ** 3), 2)
                    used_gb = round((usage.total - usage.free) / (1024 ** 3), 2)
                except Exception:
                    pass
            drives_map[drv_key] = {
                "drive": drv,
                "online": drive_online,
                "free_gb": free_gb,
                "total_gb": total_gb,
                "used_gb": used_gb,
                "used_pct": round((used_gb / total_gb) * 100, 1) if (total_gb and used_gb is not None) else 0.0
            }

    result["drives"] = sorted(drives_map.values(), key=lambda d: d["drive"].lower())
    return result


def resolve_missing_folder(payload, cfg):
    """Recreates or ignores a configured folder whose drive is online but folder was deleted."""
    folder_path = os.path.normpath(str(payload.get("path") or "").strip())
    action = str(payload.get("action") or "").strip().lower()
    if not folder_path or action not in ("recreate", "ignore", "restore"):
        return False, "Invalid folder path or action."

    if not is_path_within_configured_roots(folder_path, cfg):
        return False, f"Blocked: Path is not a configured folder ({folder_path})."

    norm_target = os.path.normcase(os.path.normpath(folder_path))
    ignored = [os.path.normpath(p) for p in (cfg.get("ignored_missing_folders") or []) if str(p).strip()]

    if action == "recreate":
        if not is_drive_mounted(folder_path):
            return False, f"Cannot recreate: drive {get_drive_label(folder_path)} is offline."
        try:
            os.makedirs(win_long_path(folder_path), exist_ok=True)
            cfg["ignored_missing_folders"] = [p for p in ignored if os.path.normcase(p) != norm_target]
            save_config(cfg)
            log_event(f"Recreated deleted folder on online drive: {folder_path}", "INFO")
            return True, f"Recreated folder: {folder_path}"
        except OSError as exc:
            return False, f"Failed to recreate folder: {exc}"

    if action == "ignore":
        if not any(os.path.normcase(p) == norm_target for p in ignored):
            ignored.append(folder_path)
        cfg["ignored_missing_folders"] = ignored
        save_config(cfg)
        log_event(f"Ignored missing folder (remembered in config.json): {folder_path}", "INFO")
        return True, f"Ignored missing folder: {folder_path}"

    # action == "restore"
    cfg["ignored_missing_folders"] = [p for p in ignored if os.path.normcase(p) != norm_target]
    save_config(cfg)
    log_event(f"Restored missing-folder warning for: {folder_path}", "INFO")
    return True, f"Restored warning for: {folder_path}"


def toggle_folder_ignored(payload, cfg):
    """Enables or disables ignoring a configured folder during scans, intake, and maintenance."""
    folder_path = os.path.normpath(str(payload.get("path") or "").strip())
    ignore_flag = bool(payload.get("ignore", True))
    if not folder_path:
        return False, "Missing folder path."

    norm_target = os.path.normcase(folder_path)
    current_ignored = [os.path.normpath(p) for p in (cfg.get("ignored_folders") or []) if str(p).strip()]
    if ignore_flag:
        if not any(os.path.normcase(p) == norm_target for p in current_ignored):
            current_ignored.append(folder_path)
        msg = f"Folder ignored by Radarr Companion: {folder_path}"
    else:
        current_ignored = [p for p in current_ignored if os.path.normcase(p) != norm_target]
        msg = f"Folder re-enabled for Radarr Companion: {folder_path}"

    cfg["ignored_folders"] = current_ignored
    save_config(cfg)
    log_event(msg, "INFO")
    return True, msg


def register_movie_in_radarr(cfg, movie_title, year="", tmdb_id="", target_folder="", root_folder=""):
    """
    Looks up an unmapped movie via Radarr's /api/v3/movie/lookup and registers it into Radarr
    so Radarr immediately recognises the folder instead of leaving it as an Unmapped Folder.
    """
    if not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        return {"registered": False, "radarr_id": None, "detail": "Radarr API not configured."}

    try:
        term = f"tmdb:{tmdb_id}" if str(tmdb_id or "").strip().isdigit() else f"{movie_title} {year}".strip()
        if not term:
            return {"registered": False, "radarr_id": None, "detail": "No title/TMDB ID for Radarr lookup."}

        encoded_term = urllib.request.quote(term)
        _, candidates = radarr_api_request(cfg, f"/api/v3/movie/lookup?term={encoded_term}", method="GET", timeout=12)
        if not candidates or not isinstance(candidates, list):
            return {"registered": False, "radarr_id": None, "detail": f"Radarr lookup found no match for '{term}'."}

        chosen = candidates[0]
        if year:
            for cand in candidates:
                if str(cand.get("year") or "") == str(year):
                    chosen = cand
                    break

        # If already in Radarr DB, return its ID
        if chosen.get("id"):
            return {"registered": True, "radarr_id": chosen["id"], "detail": f"Already in Radarr (ID {chosen['id']})."}

        # Pick first available quality profile from Radarr
        _, profiles = radarr_api_request(cfg, "/api/v3/qualityprofile", method="GET", timeout=8)
        profile_id = profiles[0]["id"] if (isinstance(profiles, list) and profiles) else 1

        root_to_use = os.path.normpath(root_folder or os.path.dirname(os.path.abspath(target_folder)))
        add_payload = dict(chosen)
        add_payload.update({
            "qualityProfileId": profile_id,
            "rootFolderPath": root_to_use,
            "path": os.path.normpath(target_folder) if target_folder else "",
            "monitored": True,
            "addOptions": {"searchForMovie": False}
        })
        _, created = radarr_api_request(cfg, "/api/v3/movie", method="POST", payload=add_payload, timeout=15)
        new_id = created.get("id") if isinstance(created, dict) else None
        log_event(f"Registered unmapped movie in Radarr: {chosen.get('title')} ({chosen.get('year')}) -> {target_folder} (ID {new_id}).", "INFO")
        return {"registered": True, "radarr_id": new_id, "detail": f"Added to Radarr (ID {new_id})."}
    except Exception as exc:
        log_event(f"Radarr auto-register warning for '{movie_title}': {exc}", "WARN")
        return {"registered": False, "radarr_id": None, "detail": f"Radarr register skipped: {exc}"}


# --- RADARR V3 API CLIENT (DB-LOCK-SAFE WITH WRITE PACING & BACKOFF) ---
def radarr_api_request(cfg, endpoint, method="GET", payload=None, timeout=10, max_retries=3):
    """
    Sends an authenticated request to the configured local Radarr v3 API.
    Never touches radarr.db directly. Serialises write requests (POST/PUT/DELETE)
    with gentle pacing and automatic retry/backoff on transient DB-busy (409/500/503) responses
    so Radarr's SQLite database is never locked out.
    """
    base_url = cfg.get("radarr_url", "").strip().rstrip("/")
    api_key = cfg.get("radarr_api_key", "").strip()
    if not base_url or not api_key:
        raise ValueError("Radarr URL or API Key is not configured.")

    url = f"{base_url}{endpoint}"
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    is_write = method.upper() in ("POST", "PUT", "DELETE")

    def _execute_once():
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return resp.getcode(), (json.loads(raw) if raw else {})

    last_exc = None
    for attempt in range(max_retries):
        try:
            if is_write:
                with RADARR_WRITE_LOCK:
                    res = _execute_once()
                    time.sleep(0.15)  # Gentle pacing so Radarr's SQLite WAL/write queue breathes
                    return res
            return _execute_once()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            # Retry gently if Radarr reports transient busy/lock (409/429/500/503)
            if exc.code in (409, 429, 500, 503) and attempt < (max_retries - 1):
                backoff = 0.6 * (attempt + 1)
                log_event(f"Radarr API busy (HTTP {exc.code}) on {method} {endpoint}; retrying in {backoff:.1f}s...", "WARN")
                time.sleep(backoff)
                continue
            raise
        except Exception as exc:
            last_exc = exc
            raise
    raise last_exc


def sync_root_folders_from_radarr(cfg):
    """Fetches all root folders from Radarr (/api/v3/rootfolder) and merges them locally."""
    jid = begin_job("Sync Radarr Roots", "Fetching root folders from Radarr...", total=1)
    try:
        _, root_folders = radarr_api_request(cfg, "/api/v3/rootfolder", method="GET", timeout=8)
    except Exception as exc:
        finish_job(jid, f"Root folder sync failed: {exc}", state="failed")
        raise
    existing_norm = {os.path.normcase(os.path.normpath(p)) for p in cfg.get("library_folders", [])}
    merged_list = list(cfg.get("library_folders", []))
    added_count = 0

    for rf in root_folders:
        raw_path = rf.get("path", "").strip()
        if not raw_path:
            continue
        norm_path = os.path.normpath(raw_path)
        norm_key = os.path.normcase(norm_path)
        if norm_key not in existing_norm:
            merged_list.append(norm_path)
            existing_norm.add(norm_key)
            added_count += 1

    cfg["library_folders"] = merged_list
    saved = save_config(cfg)
    msg = f"Synced Radarr root folders: {len(root_folders)} found, {added_count} new folder(s) added."
    finish_job(jid, msg, state="completed")
    log_event(msg, "INFO")
    return {
        "ok": True,
        "total_in_radarr": len(root_folders),
        "added_count": added_count,
        "library_folders": saved["library_folders"],
        "folder_status": get_folder_statuses(saved)
    }


def fetch_radarr_naming_config(cfg):
    """Fetches Radarr's movie naming configuration from /api/v3/config/naming."""
    fallback = {
        "connected": False,
        "rename_movies": True,
        "standard_movie_format": cfg.get("cached_naming_format") or "{Movie CleanTitle} ({Release Year}) [{Quality Full}]",
        "colon_replacement_format": "smart",
        "replace_illegal_characters": True
    }
    if not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        return fallback

    try:
        _, naming = radarr_api_request(cfg, "/api/v3/config/naming", method="GET", timeout=8)
        std_format = naming.get("standardMovieFormat") or fallback["standard_movie_format"]
        colon_fmt = naming.get("colonReplacementFormat", "smart")

        if std_format != cfg.get("cached_naming_format"):
            cfg["cached_naming_format"] = std_format
            save_config(cfg)

        log_event(f"Loaded Radarr naming convention: {std_format}", "INFO")
        return {
            "connected": True,
            "rename_movies": bool(naming.get("renameMovies", True)),
            "standard_movie_format": std_format,
            "colon_replacement_format": colon_fmt,
            "replace_illegal_characters": bool(naming.get("replaceIllegalCharacters", True))
        }
    except Exception as exc:
        log_event(f"Could not fetch Radarr naming config (using fallback format): {exc}", "WARN")
        return fallback


def fetch_radarr_movie_index(cfg):
    """Loads Radarr's movie library (/api/v3/movie) for TMDB duplicate matching."""
    index = {
        "connected": False,
        "count": 0,
        "path_to_movie": {},
        "tmdb_to_movie": {},
        "title_year_to_movie": {}
    }
    if not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        return index

    try:
        _, movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=12)
        for m in movies:
            tmdb_id = m.get("tmdbId")
            movie_id = m.get("id")
            title = m.get("title", "")
            original_title = m.get("originalTitle") or title
            year = str(m.get("year") or "")
            imdb_id = m.get("imdbId") or ""
            folder_path = m.get("path", "")

            if not tmdb_id:
                continue

            entry = {
                "tmdb_id": str(tmdb_id),
                "imdb_id": str(imdb_id),
                "radarr_id": movie_id,
                "title": title,
                "original_title": original_title,
                "year": year,
                "path": os.path.normpath(folder_path) if folder_path else ""
            }

            index["tmdb_to_movie"][str(tmdb_id)] = entry

            if entry["path"]:
                index["path_to_movie"][os.path.normcase(entry["path"])] = entry

            clean_primary = clean_title_text(title)
            if clean_primary and year:
                index["title_year_to_movie"][(clean_primary, year)] = entry

            for alt in m.get("alternateTitles", []):
                alt_title = clean_title_text(alt.get("title", ""))
                if alt_title and year and (alt_title, year) not in index["title_year_to_movie"]:
                    index["title_year_to_movie"][(alt_title, year)] = entry

        index["connected"] = True
        index["count"] = len(movies)
        log_event(f"Loaded {len(movies)} movies from Radarr API for TMDB duplicate matching.", "INFO")
    except Exception as exc:
        log_event(f"Could not fetch Radarr movie index (using local filename matching): {exc}", "WARN")

    return index


# --- MOVIE IDENTITY PARSING ---
TAG_STOP_REGEX = re.compile(
    r"\b(2160p|1080p|720p|480p|4k|uhd|bluray|blu-ray|bdrip|brrip|web-dl|webdl|webrip|"
    r"remux|hdtv|dvdrip|x264|x265|h264|h265|hevc|av1|xvid|divx|10bit|hdr|hdr10|dv|"
    r"dolby|atmos|truehd|dts|aac|ac3|ddp|proper|repack|extended|unrated|directors|cut|"
    r"imax|internal|multi|dual)\b",
    re.IGNORECASE
)
TMDB_REGEX = re.compile(r"\{?tmdb[-_]?(\d+)\}?", re.IGNORECASE)
IMDB_REGEX = re.compile(r"\{?(?:imdb[-_]?)?(tt\d{7,8})\}?", re.IGNORECASE)
YEAR_REGEX = re.compile(r"(?:^|[\s._(\[-])((?:19|20)\d{2})(?:[\s._)\]-]|$)")
RELEASE_GROUP_REGEX = re.compile(r"-([A-Za-z0-9]{2,20})(?:\[[^\]]*\])?$")


def clean_title_text(raw_name):
    text = re.sub(r"[\._]+", " ", raw_name)
    text = re.sub(r"\[[^\]]*\]|\{[^\}]*\}", " ", text)
    stop_match = TAG_STOP_REGEX.search(text)
    if stop_match:
        text = text[:stop_match.start()]
    text = re.sub(r"[^a-zA-Z0-9\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text


def parse_movie_identity(filepath, radarr_index=None):
    """Resolves a file to a canonical grouping key, display title, Radarr metadata dict, and match source."""
    radarr_index = radarr_index or {}
    path_map = radarr_index.get("path_to_movie", {})
    tmdb_map = radarr_index.get("tmdb_to_movie", {})
    title_year_map = radarr_index.get("title_year_to_movie", {})

    parent_norm = os.path.normcase(os.path.normpath(os.path.dirname(filepath)))
    if parent_norm in path_map:
        m = path_map[parent_norm]
        return (
            f"tmdb:{m['tmdb_id']}",
            f"{m['title']} ({m['year']}) [TMDB:{m['tmdb_id']}]",
            m,
            "Radarr Path + TMDB"
        )

    filename = os.path.splitext(os.path.basename(filepath))[0]
    parent_name = os.path.basename(os.path.dirname(filepath))
    combined = f"{parent_name} {filename}"

    tmdb_match = TMDB_REGEX.search(combined)
    if tmdb_match:
        tmdb_id = tmdb_match.group(1)
        if tmdb_id in tmdb_map:
            m = tmdb_map[tmdb_id]
            return (
                f"tmdb:{tmdb_id}",
                f"{m['title']} ({m['year']}) [TMDB:{tmdb_id}]",
                m,
                "Radarr TMDB Tag"
            )
        year_match = YEAR_REGEX.search(filename) or YEAR_REGEX.search(parent_name)
        year = year_match.group(1) if year_match else ""
        display_title = (clean_title_text(filename) or clean_title_text(parent_name)).title()
        meta = {"title": display_title, "original_title": display_title, "year": year, "tmdb_id": tmdb_id, "imdb_id": "", "radarr_id": None}
        return f"tmdb:{tmdb_id}", f"{display_title} ({year}) [TMDB:{tmdb_id}]".strip(), meta, "Filename TMDB Tag"

    for candidate in (filename, parent_name):
        year_matches = list(YEAR_REGEX.finditer(candidate))
        if year_matches:
            ym = year_matches[-1]
            year = ym.group(1)
            raw_title = candidate[:ym.start()]
            norm_title = clean_title_text(raw_title)
            if norm_title:
                if (norm_title, year) in title_year_map:
                    m = title_year_map[(norm_title, year)]
                    return (
                        f"tmdb:{m['tmdb_id']}",
                        f"{m['title']} ({m['year']}) [TMDB:{m['tmdb_id']}]",
                        m,
                        "Radarr Title+Year -> TMDB"
                    )
                imdb_m = IMDB_REGEX.search(combined)
                meta = {
                    "title": norm_title.title(),
                    "original_title": norm_title.title(),
                    "year": year,
                    "tmdb_id": "",
                    "imdb_id": imdb_m.group(1).lower() if imdb_m else "",
                    "radarr_id": None
                }
                return f"{norm_title} ({year})", f"{norm_title.title()} ({year})", meta, "Filename Title+Year"

    imdb_match = IMDB_REGEX.search(combined)
    if imdb_match:
        imdb_id = imdb_match.group(1).lower()
        display_title = (clean_title_text(filename) or clean_title_text(parent_name)).title()
        meta = {"title": display_title, "original_title": display_title, "year": "", "tmdb_id": "", "imdb_id": imdb_id, "radarr_id": None}
        return f"imdb:{imdb_id}", f"{display_title} [{imdb_id}]".strip(), meta, "Filename IMDb Tag"

    fallback_title = clean_title_text(filename) or clean_title_text(parent_name)
    if not fallback_title:
        return None, None, None, None
    meta = {"title": fallback_title.title(), "original_title": fallback_title.title(), "year": "", "tmdb_id": "", "imdb_id": "", "radarr_id": None}
    return f"{fallback_title} (unknown)", f"{fallback_title.title()} (Year Unknown)", meta, "Filename Fallback"


# --- OBJECTIVE MEDIA QUALITY SCORING & TOKEN EXTRACTION ---
def extract_release_group(filename_stem):
    """Extracts a trailing scene/P2P release group tag (e.g., -SPARKS) while ignoring codecs."""
    match = RELEASE_GROUP_REGEX.search(filename_stem)
    if not match:
        return ""
    candidate = match.group(1)
    ignored = {"2160p", "1080p", "720p", "480p", "x264", "x265", "h264", "h265", "hevc", "av1", "webdl", "webrip", "bluray", "remux", "aac", "ac3", "dts", "hdr", "10bit"}
    if candidate.lower() in ignored:
        return ""
    return candidate


def inspect_media_file(filepath):
    """Inspects true video/audio streams using pymediainfo (with filename/size fallback)."""
    size_bytes = os.path.getsize(win_long_path(filepath))
    size_gb = round(size_bytes / (1024 ** 3), 2)
    stem = os.path.splitext(os.path.basename(filepath))[0]
    filename_lower = os.path.basename(filepath).lower()

    resolution_label = "Unknown"
    res_token = ""
    res_score = 0
    codec_label = "Unknown"
    vcodec_token = ""
    codec_score = 0
    hdr_label = "SDR"
    hdr_token = ""
    hdr_score = 0
    bit_depth_token = ""
    bitrate_mbps = 0.0
    bitrate_score = 0
    audio_label = "Unknown"
    acodec_token = ""
    achannels_token = ""
    audio_score = 0
    inspection_method = "Filename Fallback"

    if HAS_MEDIAINFO:
        try:
            mi = MediaInfo.parse(win_long_path(filepath))
            inspection_method = "MediaInfo"
            video_tracks = [t for t in mi.tracks if t.track_type == "Video"]
            audio_tracks = [t for t in mi.tracks if t.track_type == "Audio"]
            general_tracks = [t for t in mi.tracks if t.track_type == "General"]

            if video_tracks:
                vt = video_tracks[0]
                width = int(vt.width or 0)
                height = int(vt.height or 0)

                if width >= 3600 or height >= 2000:
                    resolution_label, res_token, res_score = "2160p (4K)", "2160p", 4000
                elif width >= 1800 or height >= 1000:
                    resolution_label, res_token, res_score = "1080p", "1080p", 3000
                elif width >= 1200 or height >= 680:
                    resolution_label, res_token, res_score = "720p", "720p", 2000
                elif width > 0:
                    resolution_label, res_token, res_score = f"{height}p (SD)", f"{height}p", 1000

                fmt = str(vt.format or "").upper()
                bit_depth = int(vt.bit_depth or 8)
                bit_depth_token = str(bit_depth)
                if "AV1" in fmt:
                    codec_label, vcodec_token, codec_score = f"AV1 {bit_depth}-bit", "AV1", 350
                elif "HEVC" in fmt or "H.265" in fmt or "265" in fmt:
                    codec_label, vcodec_token, codec_score = f"HEVC {bit_depth}-bit", "x265", 320
                elif "AVC" in fmt or "H.264" in fmt or "264" in fmt:
                    codec_label, vcodec_token, codec_score = f"H.264 {bit_depth}-bit", "x264", 220
                elif fmt:
                    codec_label, vcodec_token, codec_score = fmt, fmt, 100

                hdr_info = str(vt.hdr_format or vt.hdr_format_commercial or "").upper()
                if "DOLBY VISION" in hdr_info and "HDR10" in hdr_info:
                    hdr_label, hdr_token, hdr_score = "DV HDR10", "DV HDR10", 310
                elif "DOLBY VISION" in hdr_info:
                    hdr_label, hdr_token, hdr_score = "Dolby Vision", "DV", 300
                elif "HDR10+" in hdr_info:
                    hdr_label, hdr_token, hdr_score = "HDR10+", "HDR10Plus", 260
                elif "HDR" in hdr_info:
                    hdr_label, hdr_token, hdr_score = "HDR10", "HDR10", 200
                elif bit_depth >= 10:
                    hdr_label, hdr_token, hdr_score = "10-bit", "", 80

                raw_br = vt.bit_rate or (general_tracks[0].overall_bit_rate if general_tracks else 0)
                if raw_br:
                    bitrate_mbps = round(float(raw_br) / 1_000_000.0, 2)
                    bitrate_score = min(int(bitrate_mbps * 25), 600)

            best_audio = ("Unknown", "", "", 0)
            for at in audio_tracks:
                afmt = str(at.commercial_name or at.format or "").upper()
                ch_raw = str(at.channel_s or "")
                ch_fmt = "7.1" if "8" in ch_raw else ("5.1" if "6" in ch_raw else ("2.0" if "2" in ch_raw else ("1.0" if "1" in ch_raw else ch_raw)))
                score = 50
                label = f"{afmt} {ch_fmt}".strip()
                a_tok = afmt
                if "ATMOS" in afmt and "TRUEHD" in afmt:
                    score, label, a_tok = 310, "TrueHD Atmos", "TrueHD Atmos"
                elif "ATMOS" in afmt:
                    score, label, a_tok = 300, "Dolby Atmos", "EAC3 Atmos"
                elif "TRUEHD" in afmt:
                    score, label, a_tok = 280, "Dolby TrueHD", "TrueHD"
                elif "DTS-HD" in afmt or "MASTER AUDIO" in afmt or "DTS:X" in afmt:
                    score, label, a_tok = 260, "DTS-HD MA", "DTS-HD MA"
                elif "E-AC-3" in afmt or "DD+" in afmt or "EAC3" in afmt:
                    score, label, a_tok = 180, f"DD+ ({ch_fmt})", "EAC3"
                elif "DTS" in afmt:
                    score, label, a_tok = 170, f"DTS ({ch_fmt})", "DTS"
                elif "AC-3" in afmt or "AC3" in afmt:
                    score, label, a_tok = 140, f"Dolby Digital ({ch_fmt})", "AC3"
                elif "AAC" in afmt:
                    score, label, a_tok = 100, f"AAC ({ch_fmt})", "AAC"
                if ch_fmt in ("5.1", "7.1"):
                    score += 40
                if score > best_audio[3]:
                    best_audio = (label, a_tok, ch_fmt, score)
            audio_label, acodec_token, achannels_token, audio_score = best_audio

        except Exception as exc:
            log_event(f"MediaInfo parse fallback for {os.path.basename(filepath)}: {exc}", "WARN")

    if res_score == 0:
        if "2160p" in filename_lower or "4k" in filename_lower:
            resolution_label, res_token, res_score = "2160p (4K)", "2160p", 4000
        elif "1080p" in filename_lower:
            resolution_label, res_token, res_score = "1080p", "1080p", 3000
        elif "720p" in filename_lower:
            resolution_label, res_token, res_score = "720p", "720p", 2000
        else:
            resolution_label, res_token, res_score = "SD / Unknown", "SD", 1000

    if codec_score == 0:
        if any(k in filename_lower for k in ("x265", "h265", "hevc")):
            codec_label, vcodec_token, codec_score = "HEVC", "x265", 300
        elif "av1" in filename_lower:
            codec_label, vcodec_token, codec_score = "AV1", "AV1", 320
        elif any(k in filename_lower for k in ("x264", "h264")):
            codec_label, vcodec_token, codec_score = "H.264", "x264", 200

    source_bonus = 0
    source_token = ""
    if "remux" in filename_lower:
        source_bonus, source_token = 250, "Remux"
    elif "bluray" in filename_lower or "blu-ray" in filename_lower or "bdrip" in filename_lower:
        source_bonus, source_token = 150, "Bluray"
    elif "web-dl" in filename_lower or "webdl" in filename_lower:
        source_bonus, source_token = 100, "WEBDL"
    elif "webrip" in filename_lower:
        source_bonus, source_token = 80, "WEBRip"
    elif "hdtv" in filename_lower:
        source_bonus, source_token = 50, "HDTV"
    elif "dvdrip" in filename_lower:
        source_bonus, source_token = 20, "DVD"

    quality_full = f"{source_token}-{res_token}" if (source_token and res_token) else (res_token or source_token)
    release_group = extract_release_group(stem)

    size_tiebreak = min(int(size_gb * 10), 150)
    total_score = res_score + codec_score + hdr_score + bitrate_score + audio_score + source_bonus + size_tiebreak

    summary_parts = [resolution_label, codec_label]
    if hdr_label != "SDR":
        summary_parts.append(hdr_label)
    if bitrate_mbps > 0:
        summary_parts.append(f"{bitrate_mbps} Mbps")
    if audio_label != "Unknown":
        summary_parts.append(audio_label)
    summary_parts.append(f"{size_gb} GB")

    return {
        "score": total_score,
        "resolution": resolution_label,
        "codec": codec_label,
        "hdr": hdr_label,
        "bitrate_mbps": bitrate_mbps,
        "audio": audio_label,
        "size_bytes": size_bytes,
        "size_gb": size_gb,
        "method": inspection_method,
        "summary": " | ".join(summary_parts),
        "tokens": {
            "quality_full": quality_full,
            "vcodec": vcodec_token,
            "hdr": hdr_token,
            "bit_depth": bit_depth_token,
            "acodec": acodec_token,
            "achannels": achannels_token,
            "release_group": release_group
        }
    }


# --- FEATURE 2: RADARR NAMING CONVENTION RENDERER ---
def apply_colon_replacement(text, colon_format):
    """Applies Radarr's colonReplacementFormat rule to movie titles."""
    fmt = str(colon_format).lower()
    if fmt in ("0", "delete"):
        return text.replace(":", "")
    if fmt in ("1", "dash"):
        return text.replace(":", "-")
    if fmt in ("2", "spacedash"):
        return re.sub(r":\s*", " -", text)
    if fmt in ("3", "spacedashspace"):
        return re.sub(r"\s*:\s*", " - ", text)
    return re.sub(r":\s+", " - ", text).replace(":", "-")


def sanitise_windows_filename(name):
    """Removes characters forbidden by Windows NTFS/exFAT and trims trailing spaces/dots."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "", name)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    cleaned = cleaned.strip(" .")
    return cleaned


def render_radarr_filename(winner_item, movie_meta, naming_cfg):
    """Renders a Windows-safe filename matching Radarr's /api/v3/config/naming template."""
    orig_basename = os.path.basename(winner_item["path"])
    _, ext = os.path.splitext(orig_basename)
    template = (naming_cfg or {}).get("standard_movie_format", "").strip()
    if not template:
        return orig_basename

    meta = movie_meta or {}
    tok = winner_item.get("tokens", {})
    colon_fmt = (naming_cfg or {}).get("colon_replacement_format", "smart")

    raw_title = meta.get("title") or clean_title_text(os.path.splitext(orig_basename)[0]).title()
    title_with_colons = apply_colon_replacement(raw_title, colon_fmt)
    clean_title = re.sub(r"[^A-Za-z0-9\s\-']", "", title_with_colons).strip()

    title_the = title_with_colons
    clean_title_the = clean_title
    if title_with_colons.lower().startswith("the "):
        title_the = f"{title_with_colons[4:]}, The"
        clean_title_the = f"{clean_title[4:]}, The"

    orig_title = apply_colon_replacement(meta.get("original_title") or raw_title, colon_fmt)
    year = str(meta.get("year") or "")
    imdb_id = str(meta.get("imdb_id") or "")
    tmdb_id = str(meta.get("tmdb_id") or "")

    hdr_val = tok.get("hdr", "")
    simple_mi = " ".join(x for x in (tok.get("vcodec", ""), tok.get("acodec", "")) if x)
    full_mi = " ".join(x for x in (tok.get("vcodec", ""), tok.get("acodec", ""), tok.get("achannels", "")) if x)

    token_values = {
        "movie cleantitlethe": clean_title_the,
        "movie titlethe": title_the,
        "movie cleantitle": clean_title,
        "movie originaltitle": orig_title,
        "movie title": title_with_colons,
        "release year": year,
        "imdbid": imdb_id,
        "tmdbid": tmdb_id,
        "quality full": tok.get("quality_full", ""),
        "quality title": tok.get("quality_full", ""),
        "mediainfo videodynamicrangetype": hdr_val,
        "mediainfo videodynamicrange": "HDR" if hdr_val else "",
        "mediainfo videobitdepth": tok.get("bit_depth", ""),
        "mediainfo videocodec": tok.get("vcodec", ""),
        "mediainfo audiocodec": tok.get("acodec", ""),
        "mediainfo audiochannels": tok.get("achannels", ""),
        "mediainfo simple": simple_mi,
        "mediainfo full": full_mi,
        "mediainfo 3d": "",
        "release group": tok.get("release_group", ""),
        "edition tags": "",
        "custom formats": ""
    }

    rendered = template

    def replace_nested(match):
        prefix = match.group(1)
        inner_key = match.group(2).strip().lower()
        val = token_values.get(inner_key, "")
        if not val:
            return ""
        return "{" + prefix + val + "}"

    rendered = re.sub(r"\{([^{}]+)\{([^{}]+)\}\}", replace_nested, rendered)

    sorted_keys = sorted(token_values.keys(), key=len, reverse=True)

    def replace_single(match):
        block = match.group(1)
        if block.lower().startswith(("imdb-tt", "tmdb-")):
            return "{" + block + "}"

        block_lower = block.lower()
        for key in sorted_keys:
            idx = block_lower.find(key)
            if idx != -1:
                val = token_values[key]
                if not val:
                    return ""
                return block[:idx] + val + block[idx + len(key):]
        return ""

    rendered = re.sub(r"\{([^{}]+)\}", replace_single, rendered)

    rendered = re.sub(r"\[\s*\]|\(\s*\)", "", rendered)
    rendered = re.sub(r"\[\s+", "[", rendered)
    rendered = re.sub(r"\s+\]", "]", rendered)
    rendered = re.sub(r"\s{2,}", " ", rendered).strip()
    rendered = re.sub(r"[\-\s]+$", "", rendered).strip()

    if rendered.count("[") > rendered.count("]"):
        rendered += "]" * (rendered.count("[") - rendered.count("]"))
    elif rendered.count("]") > rendered.count("["):
        rendered = rendered.replace("]", "", rendered.count("]") - rendered.count("["))

    safe_stem = sanitise_windows_filename(rendered)
    if not safe_stem:
        return orig_basename

    return f"{safe_stem}{ext}"


# --- MULTI-DRIVE DUPLICATE SCANNER ---
def scan_all_drives(cfg):
    """Scans all online library and download folders, matching duplicates against Radarr TMDB IDs."""
    jid = begin_job("Drive Scan", "Indexing Radarr library and scanning online drives...", total=1)
    radarr_index = fetch_radarr_movie_index(cfg)
    naming_cfg = fetch_radarr_naming_config(cfg)

    grouped = defaultdict(list)
    seen_paths = set()
    scanned_files = 0
    skipped_active_transfers = 0
    offline_roots = []

    sources = [
        ("library", cfg.get("library_folders", [])),
        ("download", cfg.get("download_folders", []))
    ]
    ignored_folders_norm = {
        os.path.normcase(os.path.normpath(p))
        for p in (cfg.get("ignored_folders") or [])
        if str(p).strip()
    }

    for role, roots in sources:
        for root in roots:
            if os.path.normcase(os.path.normpath(root)) in ignored_folders_norm:
                log_event(f"Skipped user-ignored folder: {root}", "INFO")
                continue
            if not os.path.isdir(root):
                offline_roots.append(root)
                log_event(f"Skipped offline folder: {root}", "WARN")
                continue

            for dirpath, dirnames, filenames in os.walk(root):
                if "_radarrcompanion_quarantine" in dirpath.lower():
                    dirnames[:] = []
                    continue
                dirnames[:] = [
                    d for d in dirnames
                    if not d.lower().startswith("_radarrcompanion_quarantine")
                    and d not in ("$RECYCLE.BIN", "System Volume Information")
                ]
                for fname in filenames:
                    ext = os.path.splitext(fname)[1].lower()
                    if ext not in VIDEO_EXTENSIONS:
                        continue
                    if "sample" in fname.lower() and "sample" not in os.path.basename(dirpath).lower():
                        continue

                    full_path = os.path.abspath(os.path.join(dirpath, fname))
                    norm_key = os.path.normcase(full_path)
                    if norm_key in seen_paths:
                        continue
                    seen_paths.add(norm_key)

                    # Exclude files currently being moved/copied so in-flight transfers are never flagged as duplicates
                    if is_path_in_active_transfer(full_path) or os.path.exists(win_long_path(full_path + ".rc_tmp")):
                        skipped_active_transfers += 1
                        continue

                    try:
                        fsize = os.path.getsize(win_long_path(full_path))
                    except OSError:
                        continue
                    if fsize < MIN_MOVIE_SIZE_BYTES:
                        continue

                    scanned_files += 1
                    group_key, display_name, movie_meta, match_source = parse_movie_identity(full_path, radarr_index)
                    if not group_key:
                        continue

                    grouped[group_key].append({
                        "path": full_path,
                        "filename": fname,
                        "folder": dirpath,
                        "root": root,
                        "role": role,
                        "drive": get_drive_label(full_path),
                        "display_name": display_name,
                        "movie_meta": movie_meta,
                        "radarr_movie_id": (movie_meta or {}).get("radarr_id"),
                        "match_source": match_source
                    })

    if skipped_active_transfers > 0:
        log_event(f"Excluded {skipped_active_transfers} file(s) currently being moved from duplicate detection.", "INFO")

    duplicate_groups = []
    intake_candidates = []
    auto_rename = bool(cfg.get("auto_rename_promoted", True))
    default_staging = resolve_default_staging_folder(cfg)

    for group_key, items in grouped.items():
        if len(items) < 2:
            # Single-copy stranded movie in a Download / Intake folder OR unmapped in Staging Root -> Intake Triage queue
            is_stranded_download = len(items) == 1 and items[0]["role"] == "download"
            is_unmapped_staging = (
                len(items) == 1
                and items[0]["role"] == "library"
                and items[0].get("radarr_movie_id") is None
                and default_staging
                and os.path.normcase(os.path.normpath(items[0]["root"])) == os.path.normcase(os.path.normpath(default_staging))
            )
            if is_stranded_download or is_unmapped_staging:
                single = items[0]
                specs = inspect_media_file(single["path"])
                item_copy = dict(single)
                item_copy.update(specs)
                item_copy["locked"] = is_file_locked(single["path"])

                meta = item_copy.get("movie_meta") or {}
                radarr_movie_id = item_copy.get("radarr_movie_id")
                radarr_db_folder = os.path.normpath(meta.get("path") or "").strip()
                radarr_db_valid = bool(radarr_db_folder) and is_path_within_configured_roots(radarr_db_folder, cfg)

                if radarr_db_valid:
                    target_folder = radarr_db_folder
                    dest_source = "Radarr DB Folder"
                else:
                    folder_title = sanitise_windows_filename(
                        f"{meta.get('title') or clean_title_text(os.path.splitext(item_copy['filename'])[0]).title()}"
                        + (f" ({meta['year']})" if meta.get("year") else "")
                    )
                    target_folder = os.path.join(default_staging, folder_title) if default_staging else folder_title
                    dest_source = "Unmapped in Staging" if is_unmapped_staging else "Staging Root"

                proposed_name = render_radarr_filename(item_copy, meta, naming_cfg) if auto_rename else item_copy["filename"]
                subs = find_matching_subtitles(item_copy["path"])
                cross_drv = bool(default_staging or radarr_db_valid) and not is_same_drive(item_copy["path"], target_folder)

                intake_candidates.append({
                    "group_key": group_key,
                    "title": item_copy["display_name"],
                    "movie_meta": meta,
                    "tmdb_matched": group_key.startswith("tmdb:"),
                    "radarr_movie_id": radarr_movie_id,
                    "source": item_copy,
                    "dest_source": dest_source,
                    "target_folder": target_folder,
                    "staging_root": default_staging,
                    "folder_name": os.path.basename(target_folder),
                    "proposed_filename": proposed_name,
                    "subtitle_count": len(subs),
                    "cross_drive_transfer": cross_drv
                })
            continue

        enriched = []
        radarr_movie_id = None
        best_meta = None
        tmdb_matched = group_key.startswith("tmdb:")

        for item in items:
            if item.get("radarr_movie_id") is not None:
                radarr_movie_id = item["radarr_movie_id"]
                best_meta = item.get("movie_meta")
            elif best_meta is None and item.get("movie_meta"):
                best_meta = item.get("movie_meta")

            specs = inspect_media_file(item["path"])
            item_copy = dict(item)
            item_copy.update(specs)
            item_copy["locked"] = is_file_locked(item["path"])
            enriched.append(item_copy)

        enriched.sort(key=lambda x: (x["score"], 1 if x["role"] == "library" else 0, x["size_bytes"]), reverse=True)

        winner = enriched[0]
        losers = enriched[1:]

        established_losers = [l for l in losers if l["role"] == "library"]
        radarr_db_folder = os.path.normpath((best_meta or {}).get("path") or "").strip()
        radarr_db_valid = bool(radarr_db_folder) and is_path_within_configured_roots(radarr_db_folder, cfg)

        target_library_folder = None
        action_type = "QUARANTINE_LOSERS"

        if radarr_db_valid:
            target_library_folder = radarr_db_folder
            winner_in_radarr_folder = (
                os.path.normcase(os.path.abspath(winner["folder"]))
                == os.path.normcase(os.path.abspath(radarr_db_folder))
            )
            if winner_in_radarr_folder:
                action_type = "KEEP_EXISTING_LIBRARY"
            elif winner["role"] == "library":
                action_type = "RELOCATE_TO_RADARR_FOLDER"
            else:
                action_type = "UPGRADE_ESTABLISHED_FOLDER"
        elif winner["role"] == "download" and established_losers:
            target_library_folder = established_losers[0]["folder"]
            action_type = "UPGRADE_ESTABLISHED_FOLDER"
        elif winner["role"] == "library":
            target_library_folder = winner["folder"]
            action_type = "KEEP_EXISTING_LIBRARY"

        cross_drive = False
        if target_library_folder:
            cross_drive = not is_same_drive(winner["path"], target_library_folder)

        proposed_filename = None
        if auto_rename and action_type in ("UPGRADE_ESTABLISHED_FOLDER", "RELOCATE_TO_RADARR_FOLDER"):
            proposed_filename = render_radarr_filename(winner, best_meta, naming_cfg)

        duplicate_groups.append({
            "group_key": group_key,
            "title": winner["display_name"],
            "tmdb_matched": tmdb_matched,
            "radarr_movie_id": radarr_movie_id,
            "radarr_db_folder": radarr_db_folder if radarr_db_valid else "",
            "action_type": action_type,
            "target_library_folder": target_library_folder,
            "proposed_filename": proposed_filename,
            "cross_drive_transfer": cross_drive,
            "winner": winner,
            "losers": losers
        })

    duplicate_groups.sort(key=lambda g: g["title"].lower())
    intake_candidates.sort(key=lambda c: c["title"].lower())
    scan_summary = (
        f"Scan complete: {scanned_files} movie file(s) indexed, {len(duplicate_groups)} duplicate group(s), "
        f"{len(intake_candidates)} stranded intake movie(s)."
    )
    finish_job(jid, scan_summary, state="completed")
    log_event(scan_summary, "INFO")
    return {
        "scanned_files": scanned_files,
        "radarr_movies_indexed": radarr_index.get("count", 0),
        "radarr_connected": radarr_index.get("connected", False),
        "naming_format": naming_cfg.get("standard_movie_format", ""),
        "default_staging_folder": default_staging,
        "offline_roots": offline_roots,
        "duplicate_groups": duplicate_groups,
        "intake_candidates": intake_candidates
    }


# --- FEATURE 3: INTAKE TRIAGE (SWEEP STRANDED BLACKHOLE DOWNLOADS TO STAGING ROOT) ---
def resolve_default_staging_folder(cfg):
    """Returns the user's configured staging_folder, or falls back to the first configured library root."""
    configured = str(cfg.get("staging_folder") or "").strip()
    if configured:
        return os.path.normpath(configured)
    libs = cfg.get("library_folders", [])
    return os.path.normpath(libs[0]) if libs else ""


def find_matching_subtitles(video_path):
    """Finds subtitle files in the same folder (or immediate Subs/Subtitles subfolder) as video_path."""
    parent = os.path.dirname(os.path.abspath(video_path))
    subs = []
    if not os.path.isdir(win_long_path(parent)):
        return subs

    # 1) Sibling subtitle files in the same folder
    try:
        for entry in os.listdir(win_long_path(parent)):
            full = os.path.join(parent, entry)
            if os.path.isfile(win_long_path(full)) and os.path.splitext(entry)[1].lower() in SUBTITLE_EXTENSIONS:
                subs.append(full)
    except OSError:
        pass

    # 2) Common torrent subtitle subfolders (Subs, Subtitles)
    for sub_dir_name in ("Subs", "Subtitles", "subs", "subtitles"):
        sub_dir = os.path.join(parent, sub_dir_name)
        if os.path.isdir(win_long_path(sub_dir)):
            try:
                for root, _, files in os.walk(win_long_path(sub_dir)):
                    for fname in files:
                        if os.path.splitext(fname)[1].lower() in SUBTITLE_EXTENSIONS:
                            subs.append(os.path.join(root, fname))
            except OSError:
                pass

    # Deduplicate preserving order
    seen = set()
    unique = []
    for s in subs:
        k = os.path.normcase(os.path.abspath(s))
        if k not in seen:
            seen.add(k)
            unique.append(s)
    return unique


def _move_file_safely_cross_drive(src_path, dst_path):
    """Moves a single file safely: instant move on same drive, size-verified copy+replace across drives."""
    with track_active_transfer(src_path, dst_path):
        same_drv = is_same_drive(src_path, dst_path)
        os.makedirs(win_long_path(os.path.dirname(dst_path)), exist_ok=True)
        if same_drv:
            shutil.move(win_long_path(src_path), win_long_path(dst_path))
            return

        src_size = os.path.getsize(win_long_path(src_path))
        usage = shutil.disk_usage(os.path.dirname(dst_path))
        if usage.free < (src_size + CROSS_DRIVE_FREE_BUFFER_BYTES):
            need_gb = round((src_size + CROSS_DRIVE_FREE_BUFFER_BYTES) / (1024 ** 3), 2)
            have_gb = round(usage.free / (1024 ** 3), 2)
            raise OSError(f"Insufficient free space on {get_drive_label(dst_path)} (Need {need_gb} GB, Free {have_gb} GB).")

        tmp_dst = dst_path + ".rc_tmp"
        copy_file_with_progress(src_path, tmp_dst, label=os.path.basename(src_path))
        if os.path.getsize(win_long_path(tmp_dst)) != src_size:
            os.remove(win_long_path(tmp_dst))
            raise OSError(f"Cross-drive size verification failed for {os.path.basename(src_path)}.")
        os.replace(win_long_path(tmp_dst), win_long_path(dst_path))
        os.remove(win_long_path(src_path))


def _cleanup_intake_clutter_after_import(source_folder, cfg):
    """
    After moving a movie and its subtitles out of a dedicated intake subfolder:
    - Never touches a configured root download folder itself,
    - If no other real movie videos (>= 50 MB) remain in that subfolder, quarantines leftover
      clutter (samples, .nfo, .txt, .jpg, etc.) and removes the empty intake subfolder.
    """
    norm_src_folder = os.path.normcase(os.path.abspath(source_folder))
    download_roots = {os.path.normcase(os.path.abspath(r)) for r in cfg.get("download_folders", [])}
    if norm_src_folder in download_roots:
        return 0, False

    if not os.path.isdir(win_long_path(source_folder)):
        return 0, False

    # Check if any other real movie file remains in this subfolder tree
    for root, _, files in os.walk(win_long_path(source_folder)):
        for fname in files:
            if os.path.splitext(fname)[1].lower() in VIDEO_EXTENSIONS:
                fpath = os.path.join(root, fname)
                if "sample" in fname.lower():
                    continue
                try:
                    if os.path.getsize(win_long_path(fpath)) >= MIN_MOVIE_SIZE_BYTES:
                        return 0, False  # Another real movie shares this folder; leave it alone
                except OSError:
                    return 0, False

    quarantined_count = 0
    for root, _, files in os.walk(win_long_path(source_folder), topdown=False):
        for fname in files:
            fpath = os.path.join(root, fname)
            if not is_file_locked(fpath):
                try:
                    quarantine_file(fpath, cfg)
                    quarantined_count += 1
                except Exception as exc:
                    log_event(f"Could not quarantine intake clutter '{fname}': {exc}", "WARN")
        try:
            if not os.listdir(win_long_path(root)):
                os.rmdir(win_long_path(root))
        except OSError:
            pass

    removed_folder = not os.path.exists(win_long_path(source_folder))
    if removed_folder:
        log_event(f"Removed emptied intake folder after cleanup: {source_folder}", "INFO")
    return quarantined_count, removed_folder


def execute_intake_import(payload, cfg):
    """
    Sweeps a stranded new movie from a Download/Intake folder into the user's chosen staging root:
    1) Creates 'Movie Title (Year)' folder inside staging root (or uses Radarr DB folder if matched),
    2) Auto-renames to Radarr's naming convention (if enabled) and moves safely (verified cross-drive copy),
    3) Moves matching subtitle files alongside the movie,
    4) Quarantines leftover torrent clutter (.nfo/.txt/samples) and removes the emptied intake subfolder,
    5) Triggers Radarr RescanMovie (or DownloadedMoviesScan fallback).
    """
    source_path = payload.get("source_path", "")
    staging_root = os.path.normpath(str(payload.get("staging_root") or resolve_default_staging_folder(cfg) or "").strip())
    target_folder = os.path.normpath(str(payload.get("target_folder") or "").strip())
    folder_name = sanitise_windows_filename(str(payload.get("folder_name") or "").strip())
    proposed_filename = payload.get("proposed_filename")
    movie_title = payload.get("title", "")
    radarr_movie_id = payload.get("radarr_movie_id")

    if not source_path:
        return False, "Missing source movie file path."
    if not is_path_within_configured_roots(source_path, cfg):
        return False, f"Blocked: Source file is outside configured folders ({source_path})."
    if not os.path.exists(win_long_path(source_path)):
        return False, f"Source file no longer exists or drive is offline: {source_path}"
    if is_file_locked(source_path):
        return False, f"File is currently locked by another process (e.g. torrent client): {os.path.basename(source_path)}"

    if staging_root and folder_name and payload.get("dest_source") != "Radarr DB Folder":
        final_folder = os.path.join(staging_root, folder_name)
    elif target_folder:
        final_folder = target_folder
    else:
        return False, "No staging root folder configured. Browse for or select a staging root folder first."

    if not is_path_within_configured_roots(final_folder, cfg):
        return False, f"Blocked: Target staging folder is outside configured Library folders ({final_folder})."

    # Ensure destination drive is online
    drive_root, _ = os.path.splitdrive(os.path.abspath(final_folder))
    drive_anchor = (drive_root + os.sep) if drive_root else os.path.dirname(os.path.abspath(final_folder))
    if not os.path.isdir(drive_anchor):
        return False, f"Target drive ({get_drive_label(final_folder)}) is offline or disconnected."

    try:
        orig_name = os.path.basename(source_path)
        orig_stem, orig_ext = os.path.splitext(orig_name)
        final_name = orig_name
        if cfg.get("auto_rename_promoted", True) and proposed_filename:
            safe_cand = sanitise_windows_filename(os.path.basename(proposed_filename))
            cand_stem, _ = os.path.splitext(safe_cand)
            if cand_stem:
                final_name = f"{cand_stem}{orig_ext}"
        final_stem, _ = os.path.splitext(final_name)

        os.makedirs(win_long_path(final_folder), exist_ok=True)
        dest_movie_path = os.path.join(final_folder, final_name)
        already_in_place = os.path.normcase(os.path.abspath(source_path)) == os.path.normcase(os.path.abspath(dest_movie_path))
        if not already_in_place and os.path.exists(win_long_path(dest_movie_path)):
            return False, f"Destination movie file already exists: {dest_movie_path}"

        source_folder = os.path.dirname(os.path.abspath(source_path))
        subtitles = find_matching_subtitles(source_path)

        log_event(f"Intake Import started: {orig_name} -> {dest_movie_path}", "INFO")
        jid = begin_job(f"Intake Import: {movie_title}", f"Importing {orig_name} -> {final_folder}", total=1)
        if not already_in_place:
            _move_file_safely_cross_drive(source_path, dest_movie_path)

        moved_subs = 0
        if not already_in_place:
            for sub_path in subtitles:
                if not os.path.exists(win_long_path(sub_path)) or is_file_locked(sub_path):
                    continue
                sub_base = os.path.basename(sub_path)
                sub_stem, sub_ext = os.path.splitext(sub_base)
                if sub_stem.lower().startswith(orig_stem.lower()):
                    suffix = sub_stem[len(orig_stem):]
                    new_sub_name = f"{final_stem}{suffix}{sub_ext}"
                else:
                    new_sub_name = sub_base
                dst_sub = os.path.join(final_folder, new_sub_name)
                if os.path.exists(win_long_path(dst_sub)):
                    dst_sub = os.path.join(final_folder, f"{final_stem}_{sub_base}")
                try:
                    _move_file_safely_cross_drive(sub_path, dst_sub)
                    moved_subs += 1
                except Exception as exc:
                    log_event(f"Subtitle move warning ({sub_base}): {exc}", "WARN")

        clutter_count, removed_dir = (0, False) if already_in_place else _cleanup_intake_clutter_after_import(source_folder, cfg)

        # If not yet in Radarr DB, look it up via Radarr /api/v3/movie/lookup and register it automatically
        reg_detail = ""
        if radarr_movie_id is None and cfg.get("radarr_url") and cfg.get("radarr_api_key"):
            meta = payload.get("movie_meta") or {}
            reg_res = register_movie_in_radarr(
                cfg,
                movie_title=meta.get("title") or movie_title,
                year=meta.get("year") or "",
                tmdb_id=meta.get("tmdb_id") or "",
                target_folder=final_folder,
                root_folder=staging_root or os.path.dirname(final_folder)
            )
            if reg_res.get("radarr_id"):
                radarr_movie_id = reg_res["radarr_id"]
            reg_detail = reg_res.get("detail") or ""

        radarr_res = trigger_radarr_rescan(cfg, radarr_movie_id=radarr_movie_id, movie_title_hint=movie_title)
        detail_parts = [f"Imported to {final_folder}"]
        if moved_subs:
            detail_parts.append(f"{moved_subs} subtitle(s) moved")
        if clutter_count or removed_dir:
            detail_parts.append(f"{clutter_count} clutter file(s) quarantined" + (", intake folder cleaned" if removed_dir else ""))
        if reg_detail:
            detail_parts.append(reg_detail)
        detail_parts.append(radarr_res["detail"])
        summary = " | ".join(detail_parts)
        finish_job(jid, f"Imported {movie_title}", state="completed")
        log_event(f"Intake Import complete ({movie_title}): {summary}", "INFO")
        return True, summary
    except Exception as exc:
        finish_job(None, f"Intake import failed for {movie_title}: {exc}", state="failed")
        log_event(f"Intake Import failed for {movie_title}: {exc}", "ERROR")
        return False, f"Import failed safely: {exc}"


# --- SAFE QUARANTINE & CROSS-DRIVE SWAP ENGINE ---
def quarantine_file(filepath, cfg):
    """Moves an inferior duplicate to Recycle Bin or a drive-local quarantine folder."""
    mode = cfg.get("quarantine_mode", "recycle_bin")
    if mode == "recycle_bin" and HAS_SEND2TRASH:
        send2trash(os.path.normpath(filepath))
        log_event(f"Sent to Recycle Bin: {filepath}", "INFO")
        return "Recycle Bin"

    drive = get_drive_label(filepath)
    if drive.endswith(":"):
        q_root = os.path.join(drive + os.sep, "_RadarrCompanion_Quarantine")
    else:
        q_root = os.path.join(os.path.dirname(filepath), "_RadarrCompanion_Quarantine")

    os.makedirs(win_long_path(q_root), exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest_name = f"{stamp}_{os.path.basename(filepath)}"
    dest_path = os.path.join(q_root, dest_name)
    shutil.move(win_long_path(filepath), win_long_path(dest_path))
    log_event(f"Moved to drive quarantine: {dest_path}", "INFO")
    return dest_path


def get_quarantine_roots(cfg):
    """Discovers all online _RadarrCompanion_Quarantine folders across configured drives and folders."""
    roots = []
    seen = set()
    all_configured = list(cfg.get("library_folders", [])) + list(cfg.get("download_folders", []))
    staging = str(cfg.get("staging_folder") or "").strip()
    if staging:
        all_configured.append(staging)

    for folder in all_configured:
        drv = get_drive_label(folder)
        if drv.endswith(":"):
            cand = os.path.join(drv + os.sep, "_RadarrCompanion_Quarantine")
        else:
            cand = os.path.join(os.path.abspath(folder), "_RadarrCompanion_Quarantine")
        norm = os.path.normcase(os.path.abspath(cand))
        if norm not in seen:
            seen.add(norm)
            if os.path.isdir(win_long_path(cand)):
                roots.append(os.path.abspath(cand))
    return sorted(roots)


def _is_path_inside_quarantine(target_path, cfg):
    """Strict security guard ensuring purge actions ONLY delete items inside _RadarrCompanion_Quarantine."""
    norm_target = os.path.normcase(os.path.abspath(target_path))
    for q_root in get_quarantine_roots(cfg):
        norm_root = os.path.normcase(os.path.abspath(q_root))
        if norm_target == norm_root:
            return False  # Never delete the quarantine root itself
        try:
            if os.path.commonpath([norm_target, norm_root]) == norm_root:
                return True
        except ValueError:
            continue
    return False


def list_quarantined_items(cfg):
    """Lists all quarantined files and folders across online _RadarrCompanion_Quarantine directories."""
    q_roots = get_quarantine_roots(cfg)
    items = []
    total_bytes = 0

    for q_root in q_roots:
        drv = get_drive_label(q_root)
        try:
            entries = sorted(os.listdir(win_long_path(q_root)))
        except OSError:
            continue
        for name in entries:
            full_path = os.path.join(q_root, name)
            is_dir = os.path.isdir(win_long_path(full_path))
            size_bytes = 0
            try:
                if is_dir:
                    for r, _, files in os.walk(win_long_path(full_path)):
                        for fn in files:
                            try:
                                size_bytes += os.path.getsize(os.path.join(r, fn))
                            except OSError:
                                pass
                else:
                    size_bytes = os.path.getsize(win_long_path(full_path))
                mtime = datetime.fromtimestamp(os.path.getmtime(win_long_path(full_path))).strftime("%Y-%m-%d %H:%M")
            except OSError:
                mtime = ""
            total_bytes += size_bytes
            items.append({
                "path": full_path,
                "name": name,
                "drive": drv,
                "quarantine_root": q_root,
                "is_dir": is_dir,
                "size_bytes": size_bytes,
                "size_mb": round(size_bytes / (1024 ** 2), 2),
                "size_gb": round(size_bytes / (1024 ** 3), 2),
                "modified": mtime
            })

    items.sort(key=lambda x: (x["modified"], x["name"].lower()), reverse=True)
    return {
        "quarantine_mode": cfg.get("quarantine_mode", "recycle_bin"),
        "quarantine_roots": q_roots,
        "item_count": len(items),
        "total_bytes": total_bytes,
        "total_gb": round(total_bytes / (1024 ** 3), 2),
        "items": items
    }


def purge_quarantined_items(payload, cfg):
    """Permanently deletes selected items or all items inside _RadarrCompanion_Quarantine folders."""
    purge_all = bool(payload.get("purge_all", False))
    requested_paths = payload.get("paths") or []

    if purge_all:
        listing = list_quarantined_items(cfg)
        targets = [it["path"] for it in listing["items"]]
    else:
        targets = [str(p).strip() for p in requested_paths if str(p).strip()]

    if not targets:
        return True, "No quarantined items to purge.", {"purged": 0, "freed_gb": 0.0, "failed": []}

    purged = 0
    freed_bytes = 0
    failed = []

    for target in targets:
        if not _is_path_inside_quarantine(target, cfg):
            failed.append(f"Blocked (outside quarantine): {os.path.basename(target)}")
            continue
        lp = win_long_path(target)
        if not os.path.exists(lp):
            continue
        try:
            if os.path.isdir(lp):
                dir_size = 0
                for r, _, files in os.walk(lp):
                    for fn in files:
                        try:
                            dir_size += os.path.getsize(os.path.join(r, fn))
                        except OSError:
                            pass
                shutil.rmtree(lp)
                freed_bytes += dir_size
            else:
                fsize = os.path.getsize(lp)
                os.remove(lp)
                freed_bytes += fsize
            purged += 1
        except Exception as exc:
            failed.append(f"{os.path.basename(target)}: {exc}")
            log_event(f"Failed to purge quarantined item '{target}': {exc}", "WARN")

    freed_gb = round(freed_bytes / (1024 ** 3), 2)
    summary = f"Purged {purged} quarantined item(s) ({freed_gb} GB freed)" + (f", {len(failed)} failed." if failed else ".")
    jid = begin_job("Quarantine Purge", summary, total=len(targets))
    finish_job(jid, summary, state="completed" if not failed else "warning")
    log_event(summary, "INFO" if not failed else "WARN")
    return (len(failed) == 0), summary, {"purged": purged, "freed_gb": freed_gb, "failed": failed}


def trigger_radarr_rescan(cfg, radarr_movie_id=None, movie_title_hint=""):
    """Pings local Radarr v3 API to rescan the exact movieId (or library fallback) after a swap."""
    if not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        return {"triggered": False, "detail": "Radarr API not configured (skipped)."}

    try:
        movie_id = radarr_movie_id
        if movie_id is None and movie_title_hint:
            _, movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=8)
            clean_hint = clean_title_text(movie_title_hint)
            for m in movies:
                if clean_title_text(m.get("title", "")) == clean_hint:
                    movie_id = m.get("id")
                    break

        payload = {"name": "RescanMovie"}
        if movie_id is not None:
            payload["movieId"] = int(movie_id)

        status_code, _ = radarr_api_request(cfg, "/api/v3/command", method="POST", payload=payload, timeout=8)
        target_desc = f"movieId {movie_id}" if movie_id is not None else "all movies"
        log_event(f"Radarr RescanMovie triggered ({target_desc}, HTTP {status_code}).", "INFO")
        return {"triggered": True, "detail": f"Radarr rescan queued ({target_desc})."}

    except Exception as exc:
        log_event(f"Radarr rescan failed (files were still processed): {exc}", "WARN")
        return {"triggered": False, "detail": f"Radarr API error: {exc}"}


def execute_duplicate_resolution(payload, cfg):
    """Safely executes a user-approved duplicate swap, quarantine, and optional Radarr-format rename."""
    winner_path = payload.get("winner_path", "")
    loser_paths = payload.get("loser_paths", [])
    target_library_folder = payload.get("target_library_folder")
    proposed_filename = payload.get("proposed_filename")
    movie_title = payload.get("title", "")
    radarr_movie_id = payload.get("radarr_movie_id")

    if not winner_path or not loser_paths:
        return False, "Missing winner or loser file paths."

    for p in [winner_path] + loser_paths:
        if not is_path_within_configured_roots(p, cfg):
            return False, f"Blocked: Path is outside configured folders ({p})."
        if not os.path.exists(win_long_path(p)):
            return False, f"File no longer exists or drive is offline: {p}"
        if is_file_locked(p):
            return False, f"File is currently locked by another process: {os.path.basename(p)}"

    if target_library_folder:
        if not is_path_within_configured_roots(target_library_folder, cfg):
            return False, f"Blocked: Target folder is outside configured folders ({target_library_folder})."
        if not os.path.isdir(win_long_path(target_library_folder)):
            # Allow auto-creating a missing movie subfolder if its parent configured root drive is online
            parent_online = False
            norm_target = os.path.normcase(os.path.abspath(target_library_folder))
            for root in cfg.get("library_folders", []):
                norm_root = os.path.normcase(os.path.abspath(root))
                try:
                    if os.path.commonpath([norm_target, norm_root]) == norm_root and os.path.isdir(root):
                        parent_online = True
                        break
                except ValueError:
                    continue
            if parent_online:
                try:
                    os.makedirs(win_long_path(target_library_folder), exist_ok=True)
                    log_event(f"Created missing Radarr movie folder: {target_library_folder}", "INFO")
                except OSError as exc:
                    return False, f"Could not create Radarr target folder ({target_library_folder}): {exc}"
            else:
                return False, f"Target established folder is offline: {target_library_folder}"

    winner_needs_move = (
        target_library_folder
        and os.path.normcase(os.path.dirname(os.path.abspath(winner_path)))
        != os.path.normcase(os.path.abspath(target_library_folder))
    )

    try:
        if winner_needs_move:
            orig_name = os.path.basename(winner_path)
            _, orig_ext = os.path.splitext(orig_name)

            final_name = orig_name
            if cfg.get("auto_rename_promoted", True) and proposed_filename:
                safe_candidate = sanitise_windows_filename(os.path.basename(proposed_filename))
                cand_stem, _ = os.path.splitext(safe_candidate)
                if cand_stem:
                    final_name = f"{cand_stem}{orig_ext}"

            dest_path = os.path.join(target_library_folder, final_name)
            same_drv = is_same_drive(winner_path, target_library_folder)
            winner_size = os.path.getsize(win_long_path(winner_path))

            if not same_drv:
                usage = shutil.disk_usage(target_library_folder)
                if usage.free < (winner_size + CROSS_DRIVE_FREE_BUFFER_BYTES):
                    need_gb = round((winner_size + CROSS_DRIVE_FREE_BUFFER_BYTES) / (1024 ** 3), 2)
                    have_gb = round(usage.free / (1024 ** 3), 2)
                    return False, f"Insufficient free space on {get_drive_label(target_library_folder)} (Need {need_gb} GB, Free {have_gb} GB)."

                tmp_dest = dest_path + ".rc_tmp"
                log_event(f"Cross-drive copy started: {orig_name} -> {dest_path}", "INFO")
                update_job_progress(
                    active=True,
                    job_name="Duplicate Swap",
                    status=f"Copying {orig_name} across drives...",
                    overall_current=0,
                    overall_total=1
                )
                copy_file_with_progress(winner_path, tmp_dest, label=orig_name)

                if os.path.getsize(win_long_path(tmp_dest)) != winner_size:
                    os.remove(win_long_path(tmp_dest))
                    return False, "Cross-drive copy size verification failed. Aborted safely; no files deleted."

                for lp in loser_paths:
                    quarantine_file(lp, cfg)

                if os.path.exists(win_long_path(dest_path)):
                    quarantine_file(dest_path, cfg)
                os.replace(win_long_path(tmp_dest), win_long_path(dest_path))

                os.remove(win_long_path(winner_path))
                log_event(f"Upgraded & renamed in established folder: {dest_path}", "INFO")
            else:
                for lp in loser_paths:
                    quarantine_file(lp, cfg)
                if os.path.exists(win_long_path(dest_path)):
                    quarantine_file(dest_path, cfg)
                shutil.move(win_long_path(winner_path), win_long_path(dest_path))
                log_event(f"Moved & renamed on same drive to: {dest_path}", "INFO")
        else:
            for lp in loser_paths:
                quarantine_file(lp, cfg)

        radarr_res = trigger_radarr_rescan(cfg, radarr_movie_id=radarr_movie_id, movie_title_hint=movie_title)
        reset_job_progress(f"Duplicate swap complete: {movie_title}")
        return True, f"Completed successfully. {radarr_res['detail']}"

    except Exception as exc:
        reset_job_progress(f"Swap failed: {movie_title}")
        log_event(f"Swap failed for {movie_title}: {exc}", "ERROR")
        return False, f"Operation failed safely: {exc}"


# --- COLLECTIONS MANAGER (NATIVE RADARR + CUSTOM COLLECTIONS) ---
def _radarr_movie_summary(m):
    """Extracts a lightweight summary dict from a Radarr /api/v3/movie record."""
    folder_path = os.path.normpath(m.get("path") or "") if m.get("path") else ""
    root_path = os.path.normpath(m.get("rootFolderPath") or "") if m.get("rootFolderPath") else ""
    size_bytes = int(m.get("sizeOnDisk") or 0)
    return {
        "radarr_id": m.get("id"),
        "tmdb_id": str(m.get("tmdbId") or ""),
        "title": m.get("title") or "",
        "year": m.get("year") or "",
        "path": folder_path,
        "root_folder": root_path,
        "drive": get_drive_label(folder_path) if folder_path else "",
        "has_file": bool(m.get("hasFile")),
        "size_gb": round(size_bytes / (1024 ** 3), 2) if size_bytes else 0.0,
        "size_bytes": size_bytes,
        "tags": m.get("tags") or []
    }


def fetch_collections_overview(cfg):
    """
    Loads Radarr's native collections (/api/v3/collection), movie library (/api/v3/movie),
    root folders (/api/v3/rootfolder), tags (/api/v3/tag), and merges local Custom Collections.
    """
    custom_defs = normalise_custom_collections(cfg.get("custom_collections", []))
    result = {
        "connected": False,
        "error": "",
        "root_folders": [],
        "tags": [],
        "movies": [],
        "native_collections": [],
        "custom_collections": []
    }

    if not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        result["error"] = "Radarr URL or API Key is not configured."
        result["custom_collections"] = [
            {
                "id": c["id"],
                "name": c["name"],
                "radarr_tag": c["radarr_tag"],
                "radarr_tags": c.get("radarr_tags", []),
                "tmdb_ids": c["tmdb_ids"],
                "movie_count": len(c["tmdb_ids"]),
                "total_size_gb": 0.0,
                "drives": [],
                "movies": []
            }
            for c in custom_defs
        ]
        return result

    try:
        _, raw_movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=15)
        _, raw_roots = radarr_api_request(cfg, "/api/v3/rootfolder", method="GET", timeout=10)
        try:
            _, raw_collections = radarr_api_request(cfg, "/api/v3/collection", method="GET", timeout=12)
        except Exception:
            raw_collections = []
        try:
            _, raw_tags = radarr_api_request(cfg, "/api/v3/tag", method="GET", timeout=8)
        except Exception:
            raw_tags = []

        tag_label_to_id = {str(t.get("label") or "").strip().lower(): t.get("id") for t in raw_tags if t.get("id") is not None}
        tag_id_movie_counts = defaultdict(int)
        for m in raw_movies:
            for tid in (m.get("tags") or []):
                tag_id_movie_counts[tid] += 1
        tags_out = sorted(
            [
                {
                    "id": t.get("id"),
                    "label": str(t.get("label") or "").strip().lower(),
                    "movie_count": tag_id_movie_counts.get(t.get("id"), 0)
                }
                for t in raw_tags
                if t.get("id") is not None and str(t.get("label") or "").strip()
            ],
            key=lambda x: (-x["movie_count"], x["label"])
        )

        movies_by_tmdb = {}
        movies_by_coll_tmdb = defaultdict(list)
        all_movies_summary = []

        for m in raw_movies:
            summary = _radarr_movie_summary(m)
            all_movies_summary.append(summary)
            if summary["tmdb_id"]:
                movies_by_tmdb[summary["tmdb_id"]] = summary
            coll = m.get("collection") or {}
            coll_tmdb = coll.get("tmdbId")
            if coll_tmdb:
                movies_by_coll_tmdb[str(coll_tmdb)].append(summary)

        all_movies_summary.sort(key=lambda x: (x["title"].lower(), str(x["year"])))

        roots_out = []
        seen_root_keys = set()
        for rf in raw_roots:
            rp = os.path.normpath(rf.get("path") or "")
            if not rp:
                continue
            rkey = os.path.normcase(os.path.abspath(rp))
            if rkey in seen_root_keys:
                continue
            seen_root_keys.add(rkey)
            online = os.path.isdir(rp)
            free_gb = None
            if online:
                try:
                    free_gb = round(shutil.disk_usage(rp).free / (1024 ** 3), 2)
                except Exception:
                    pass
            roots_out.append({
                "path": rp,
                "drive": get_drive_label(rp),
                "online": online,
                "free_gb": free_gb
            })
        # Merge local library_folders so ALL user library folders always appear in dropdowns
        for lf in cfg.get("library_folders", []):
            rp = os.path.normpath(lf or "")
            if not rp:
                continue
            rkey = os.path.normcase(os.path.abspath(rp))
            if rkey in seen_root_keys:
                continue
            seen_root_keys.add(rkey)
            online = os.path.isdir(rp)
            free_gb = None
            if online:
                try:
                    free_gb = round(shutil.disk_usage(rp).free / (1024 ** 3), 2)
                except Exception:
                    pass
            roots_out.append({
                "path": rp,
                "drive": get_drive_label(rp),
                "online": online,
                "free_gb": free_gb
            })
        roots_out.sort(key=lambda r: (r["drive"].lower(), r["path"].lower()))

        ignored_keys = set(normalise_collection_ignores(cfg.get("collection_location_ignores", [])))

        # Build Custom Collections first so movies managed by Custom Collections (especially
        # custom-tagged collections) are excluded from general Native Radarr collection scanning.
        custom_out = []
        custom_claimed_tmdbs = set()
        for cd in custom_defs:
            member_map = {}
            for tid in cd["tmdb_ids"]:
                if tid in movies_by_tmdb:
                    member_map[tid] = movies_by_tmdb[tid]
            cd_tags = cd.get("radarr_tags") or ([cd["radarr_tag"]] if cd.get("radarr_tag") else [])
            matched_tag_ids = {tag_label_to_id[tl] for tl in cd_tags if tl in tag_label_to_id}
            if matched_tag_ids:
                for mv in all_movies_summary:
                    if mv["tmdb_id"] and any(tid in matched_tag_ids for tid in mv["tags"]):
                        member_map[mv["tmdb_id"]] = mv
            for claimed_tid in member_map.keys():
                if claimed_tid:
                    custom_claimed_tmdbs.add(str(claimed_tid))

            cmovies_raw = sorted(member_map.values(), key=lambda x: (str(x["year"]), x["title"].lower()))
            coll_key = f"custom:{cd['id']}"
            # Use saved custom root_folder if set, otherwise infer most common root among its movies
            saved_root = str(cd.get("root_folder") or "").strip()
            if saved_root:
                inferred_root = os.path.normpath(saved_root)
            else:
                root_counts = defaultdict(int)
                for mv in cmovies_raw:
                    if mv.get("root_folder"):
                        root_counts[mv["root_folder"]] += 1
                inferred_root = max(root_counts.items(), key=lambda kv: kv[1])[0] if root_counts else ""
            inferred_root_norm = os.path.normcase(os.path.abspath(inferred_root)) if inferred_root else ""
            cmovies = []
            misplaced_count = 0
            ignored_count = 0
            for mv in cmovies_raw:
                mv_copy = dict(mv)
                mv_root_norm = os.path.normcase(os.path.abspath(mv_copy["root_folder"])) if mv_copy.get("root_folder") else ""
                ignore_key = f"{coll_key}:{mv_copy['tmdb_id']}".lower()
                is_ignored = ignore_key in ignored_keys
                is_misplaced = bool(inferred_root_norm and mv_root_norm != inferred_root_norm)
                mv_copy["in_collection_root"] = (not is_misplaced) or is_ignored
                mv_copy["location_ignored"] = is_ignored
                mv_copy["ignore_key"] = ignore_key
                if is_ignored:
                    ignored_count += 1
                elif is_misplaced:
                    misplaced_count += 1
                cmovies.append(mv_copy)
            drives = sorted({mv["drive"] for mv in cmovies if mv["drive"]})
            total_gb = round(sum(mv["size_bytes"] for mv in cmovies) / (1024 ** 3), 2)
            custom_out.append({
                "id": cd["id"],
                "coll_key": coll_key,
                "name": cd["name"],
                "radarr_tag": cd["radarr_tag"],
                "radarr_tags": cd_tags,
                "tmdb_ids": cd["tmdb_ids"],
                "root_folder": inferred_root,
                "movie_count": len(cmovies),
                "misplaced_count": misplaced_count,
                "ignored_count": ignored_count,
                "total_size_gb": total_gb,
                "drives": drives,
                "movies": cmovies
            })

        native_out = []
        seen_coll_tmdb = set()
        excluded_custom_movie_hits = 0
        for c in raw_collections:
            ctmdb = str(c.get("tmdbId") or "")
            if not ctmdb:
                continue
            seen_coll_tmdb.add(ctmdb)
            coll_key = f"native:{ctmdb}"
            coll_root = os.path.normpath(c.get("rootFolderPath") or "") if c.get("rootFolderPath") else ""
            coll_root_norm = os.path.normcase(os.path.abspath(coll_root)) if coll_root else ""
            cmovies_all = sorted(movies_by_coll_tmdb.get(ctmdb, []), key=lambda x: (str(x["year"]), x["title"].lower()))
            cmovies_raw = [mv for mv in cmovies_all if str(mv.get("tmdb_id") or "") not in custom_claimed_tmdbs]
            excluded_here = len(cmovies_all) - len(cmovies_raw)
            excluded_custom_movie_hits += excluded_here
            if cmovies_all and not cmovies_raw:
                # Every movie in this native collection is managed by a Custom Collection — exclude from general scan
                continue
            cmovies = []
            misplaced_count = 0
            ignored_count = 0
            for mv in cmovies_raw:
                mv_copy = dict(mv)
                mv_root_norm = os.path.normcase(os.path.abspath(mv_copy["root_folder"])) if mv_copy.get("root_folder") else ""
                ignore_key = f"{coll_key}:{mv_copy['tmdb_id']}".lower()
                is_ignored = ignore_key in ignored_keys
                is_misplaced = bool(coll_root_norm and mv_root_norm != coll_root_norm)
                mv_copy["in_collection_root"] = (not is_misplaced) or is_ignored
                mv_copy["location_ignored"] = is_ignored
                mv_copy["ignore_key"] = ignore_key
                if is_ignored:
                    ignored_count += 1
                elif is_misplaced:
                    misplaced_count += 1
                cmovies.append(mv_copy)
            drives = sorted({mv["drive"] for mv in cmovies if mv["drive"]})
            total_gb = round(sum(mv["size_bytes"] for mv in cmovies) / (1024 ** 3), 2)
            native_out.append({
                "id": c.get("id"),
                "tmdb_id": ctmdb,
                "coll_key": coll_key,
                "name": c.get("title") or f"Collection {ctmdb}",
                "monitored": bool(c.get("monitored", False)),
                "root_folder": coll_root,
                "movie_count": len(cmovies),
                "misplaced_count": misplaced_count,
                "ignored_count": ignored_count,
                "total_size_gb": total_gb,
                "drives": drives,
                "movies": cmovies
            })

        # Also surface collections referenced by movies even if /api/v3/collection didn't list them
        for ctmdb, cmovies in movies_by_coll_tmdb.items():
            if ctmdb in seen_coll_tmdb:
                continue
            cmovies_filtered = [mv for mv in cmovies if str(mv.get("tmdb_id") or "") not in custom_claimed_tmdbs]
            excluded_custom_movie_hits += (len(cmovies) - len(cmovies_filtered))
            if not cmovies_filtered:
                continue
            cmovies_sorted = sorted(cmovies_filtered, key=lambda x: (str(x["year"]), x["title"].lower()))
            drives = sorted({mv["drive"] for mv in cmovies_sorted if mv["drive"]})
            total_gb = round(sum(mv["size_bytes"] for mv in cmovies_sorted) / (1024 ** 3), 2)
            native_out.append({
                "id": None,
                "tmdb_id": ctmdb,
                "name": f"Collection TMDB:{ctmdb}",
                "monitored": False,
                "root_folder": "",
                "movie_count": len(cmovies_sorted),
                "total_size_gb": total_gb,
                "drives": drives,
                "movies": cmovies_sorted
            })

        native_out.sort(key=lambda x: (-x["movie_count"], x["name"].lower()))

        result.update({
            "connected": True,
            "root_folders": roots_out,
            "tags": tags_out,
            "movies": all_movies_summary,
            "native_collections": native_out,
            "custom_collections": custom_out,
            "excluded_custom_count": excluded_custom_movie_hits,
            "collections_root_parent": str(cfg.get("collections_root_parent") or "").strip()
        })
        return result
    except Exception as exc:
        log_event(f"Failed to load Radarr collections overview: {exc}", "WARN")
        result["error"] = str(exc)
        return result


def _ensure_radarr_tag_id(cfg, tag_label):
    """Returns the Radarr tag ID for tag_label, creating the tag in Radarr if needed."""
    clean = re.sub(r"[^a-z0-9_-]", "", str(tag_label or "").strip().lower())
    if not clean:
        return None
    _, tags = radarr_api_request(cfg, "/api/v3/tag", method="GET", timeout=8)
    for t in tags:
        if str(t.get("label") or "").strip().lower() == clean:
            return t.get("id")
    _, created = radarr_api_request(cfg, "/api/v3/tag", method="POST", payload={"label": clean}, timeout=8)
    return created.get("id")


def _sync_custom_collection_tag_to_radarr(cfg, collection_def):
    """
    Applies all linked Radarr tags to all Radarr movies listed in a custom collection
    using Radarr's single batch /api/v3/movie/editor endpoint to avoid SQLite lock contention.
    """
    tag_labels = collection_def.get("radarr_tags") or ([collection_def["radarr_tag"]] if collection_def.get("radarr_tag") else [])
    tmdb_ids = set(collection_def.get("tmdb_ids") or [])
    if not tag_labels or not tmdb_ids or not cfg.get("radarr_url") or not cfg.get("radarr_api_key"):
        return 0

    tag_ids = []
    for tl in tag_labels:
        tid = _ensure_radarr_tag_id(cfg, tl)
        if tid is not None and tid not in tag_ids:
            tag_ids.append(tid)
    if not tag_ids:
        return 0

    _, raw_movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=15)
    target_movie_ids = []
    for m in raw_movies:
        if str(m.get("tmdbId") or "") not in tmdb_ids:
            continue
        existing_tags = set(m.get("tags") or [])
        if any(tid not in existing_tags for tid in tag_ids):
            if m.get("id") is not None:
                target_movie_ids.append(m["id"])

    if not target_movie_ids:
        return 0

    # Single atomic batch call via Radarr's native movie editor API
    radarr_api_request(
        cfg,
        "/api/v3/movie/editor",
        method="PUT",
        payload={
            "movieIds": target_movie_ids,
            "tags": tag_ids,
            "applyTags": "add"
        },
        timeout=15
    )
    return len(target_movie_ids)


def _get_directory_size_bytes(path):
    """Safely computes total bytes of all files currently inside path."""
    total = 0
    lp = win_long_path(path)
    if not os.path.isdir(lp):
        return 0
    for root, _, files in os.walk(lp):
        for fname in files:
            try:
                total += os.path.getsize(os.path.join(root, fname))
            except OSError:
                pass
    return total


def _move_directory_with_external_command(src_dir, dst_dir, overall_index=None, overall_total=None):
    """
    Moves a movie folder using Windows' external `robocopy.exe /MOVE /E` command
    (with pre-move lock & free-space safety checks).
    Measures smooth, monotonic byte progress on dst_dir so status-bar percentages never jump erratically.
    Falls back to Python _move_directory_safely only if robocopy.exe is unavailable.
    """
    src_abs = os.path.abspath(src_dir)
    dst_abs = os.path.abspath(dst_dir)
    if os.path.normcase(src_abs) == os.path.normcase(dst_abs):
        return

    # Pre-check locks and exact total size across source folder
    total_size = 0
    largest_file_name = os.path.basename(src_abs)
    largest_file_size = -1
    for root, _, files in os.walk(win_long_path(src_abs)):
        rel = os.path.relpath(root, win_long_path(src_abs))
        for fname in files:
            src_f = os.path.join(src_abs, fname) if rel == "." else os.path.join(src_abs, rel, fname)
            if is_file_locked(src_f):
                raise OSError(f"File is locked by another process: {fname}")
            fsize = os.path.getsize(win_long_path(src_f))
            total_size += fsize
            if fsize > largest_file_size:
                largest_file_size = fsize
                largest_file_name = fname

    same_drv = is_same_drive(src_abs, dst_abs)
    if not same_drv:
        dst_parent = os.path.dirname(dst_abs)
        os.makedirs(win_long_path(dst_parent), exist_ok=True)
        usage = shutil.disk_usage(dst_parent)
        if usage.free < (total_size + CROSS_DRIVE_FREE_BUFFER_BYTES):
            need_gb = round((total_size + CROSS_DRIVE_FREE_BUFFER_BYTES) / (1024 ** 3), 2)
            have_gb = round(usage.free / (1024 ** 3), 2)
            raise OSError(f"Insufficient free space on {get_drive_label(dst_abs)} (Need {need_gb} GB, Free {have_gb} GB).")

    robocopy_exe = shutil.which("robocopy.exe") or os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "robocopy.exe"
    )
    if os.name == "nt" and robocopy_exe and os.path.isfile(robocopy_exe):
        os.makedirs(win_long_path(dst_abs), exist_ok=True)
        initial_dst_bytes = _get_directory_size_bytes(dst_abs)
        total_mb = round(total_size / (1024 ** 2), 1)
        update_job_progress(
            current_file=largest_file_name,
            file_percent=0,
            file_copied_mb=0.0,
            file_total_mb=total_mb
        )

        stop_monitor = threading.Event()

        def _monitor_bytes():
            last_pct = 0
            while not stop_monitor.is_set():
                if total_size > 0:
                    cur_dst = _get_directory_size_bytes(dst_abs)
                    copied_bytes = max(0, min(total_size, cur_dst - initial_dst_bytes))
                    pct = int((copied_bytes / total_size) * 100)
                    # Strictly monotonic (0..99 while running) so percentage never bounces backward
                    pct = max(last_pct, min(99, pct))
                    last_pct = pct
                    kwargs = {
                        "current_file": largest_file_name,
                        "file_percent": pct,
                        "file_copied_mb": round(copied_bytes / (1024 ** 2), 1),
                        "file_total_mb": total_mb
                    }
                    if overall_index is not None and overall_total and overall_total > 0:
                        blended = int(((overall_index + (pct / 100.0)) / overall_total) * 100)
                        kwargs["overall_percent"] = max(0, min(99, blended))
                    update_job_progress(**kwargs)
                stop_monitor.wait(0.35)

        mon_thread = threading.Thread(target=_monitor_bytes, daemon=True)
        mon_thread.start()

        cmd = [
            robocopy_exe,
            src_abs,
            dst_abs,
            "/E",        # Copy subdirectories including empty ones
            "/MOVE",     # Move files and directories (delete from source after copy)
            "/R:2",      # 2 retries on transient errors
            "/W:2",      # 2 second wait between retries
            "/NFL",      # No file list noise
            "/NDL",      # No directory list noise
            "/NJH",      # No job header
            "/NJS",      # No job summary
            "/NP"        # No erratic stdout percentage spam
        ]
        try:
            with track_active_transfer(src_abs, dst_abs):
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=7200,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
        finally:
            stop_monitor.set()
            mon_thread.join(timeout=1.5)

        # Robocopy exit codes 0..7 indicate success; >=8 is failure
        if proc.returncode >= 8:
            err_lines = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
            brief = err_lines[-1] if err_lines else f"robocopy exit code {proc.returncode}"
            raise OSError(f"External robocopy move failed: {brief}")
        update_job_progress(file_percent=100, file_copied_mb=total_mb, file_total_mb=total_mb)
        # Clean up emptied source directory if robocopy left the top-level shell behind
        if os.path.isdir(win_long_path(src_abs)):
            try:
                if not os.listdir(win_long_path(src_abs)):
                    os.rmdir(win_long_path(src_abs))
            except OSError:
                pass
        return

    with track_active_transfer(src_abs, dst_abs):
        _move_directory_safely(src_abs, dst_abs)


def _move_directory_safely(src_dir, dst_dir):
    """
    Moves a movie folder safely to dst_dir.
    Same-drive uses fast rename/move; cross-drive copies every file with size verification
    before removing the source folder.
    """
    src_abs = os.path.abspath(src_dir)
    dst_abs = os.path.abspath(dst_dir)
    if os.path.normcase(src_abs) == os.path.normcase(dst_abs):
        return

    # Pre-check locks on all files inside src_dir
    total_size = 0
    file_pairs = []
    for root, _, files in os.walk(win_long_path(src_abs)):
        rel = os.path.relpath(root, win_long_path(src_abs))
        for fname in files:
            src_f = os.path.join(src_abs, fname) if rel == "." else os.path.join(src_abs, rel, fname)
            if is_file_locked(src_f):
                raise OSError(f"File is locked by another process: {fname}")
            fsize = os.path.getsize(win_long_path(src_f))
            total_size += fsize
            dst_f = os.path.join(dst_abs, fname) if rel == "." else os.path.join(dst_abs, rel, fname)
            file_pairs.append((src_f, dst_f, fsize))

    same_drv = is_same_drive(src_abs, dst_abs)
    if same_drv and not os.path.exists(win_long_path(dst_abs)):
        os.makedirs(win_long_path(os.path.dirname(dst_abs)), exist_ok=True)
        shutil.move(win_long_path(src_abs), win_long_path(dst_abs))
        return

    if not same_drv:
        dst_parent = os.path.dirname(dst_abs)
        usage = shutil.disk_usage(dst_parent)
        if usage.free < (total_size + CROSS_DRIVE_FREE_BUFFER_BYTES):
            need_gb = round((total_size + CROSS_DRIVE_FREE_BUFFER_BYTES) / (1024 ** 3), 2)
            have_gb = round(usage.free / (1024 ** 3), 2)
            raise OSError(f"Insufficient free space on {get_drive_label(dst_abs)} (Need {need_gb} GB, Free {have_gb} GB).")

    os.makedirs(win_long_path(dst_abs), exist_ok=True)
    for src_f, dst_f, expected_size in file_pairs:
        os.makedirs(win_long_path(os.path.dirname(dst_f)), exist_ok=True)
        if same_drv:
            shutil.move(win_long_path(src_f), win_long_path(dst_f))
        else:
            tmp_f = dst_f + ".rc_tmp"
            shutil.copy2(win_long_path(src_f), win_long_path(tmp_f))
            if os.path.getsize(win_long_path(tmp_f)) != expected_size:
                os.remove(win_long_path(tmp_f))
                raise OSError(f"Cross-drive size verification failed for {os.path.basename(src_f)}.")
            os.replace(win_long_path(tmp_f), win_long_path(dst_f))

    if not same_drv:
        shutil.rmtree(win_long_path(src_abs))
    else:
        # Remove emptied source folder if it still exists
        try:
            shutil.rmtree(win_long_path(src_abs))
        except OSError:
            pass


def bulk_move_collection(payload, cfg):
    """
    Moves every movie in a native or custom collection to a single target root folder:
    1) Verifies destination root is a configured library/Radarr root; if missing on an online drive,
       prompts the UI to confirm creation (unless create_if_missing=True),
    2) Safely moves movie folders on disk (same-drive move or cross-drive size-verified copy),
    3) Updates each movie's path/rootFolderPath in Radarr v3 API and triggers RescanMovie.
    """
    collection_name = str(payload.get("collection_name") or "Collection").strip()
    collection_id = payload.get("collection_id")
    movie_ids = [int(x) for x in (payload.get("movie_ids") or []) if str(x).isdigit()]
    target_root = os.path.normpath(str(payload.get("target_root_folder") or "").strip())
    create_if_missing = bool(payload.get("create_if_missing", False))

    if not movie_ids:
        return False, "No movies selected to move.", {}
    if not target_root:
        return False, "Target root folder path is required.", {}

    # Allow destination if it is in local library_folders or registered as a Radarr root folder
    allowed_roots = {os.path.normcase(os.path.abspath(r)) for r in cfg.get("library_folders", [])}
    try:
        _, radarr_roots = radarr_api_request(cfg, "/api/v3/rootfolder", method="GET", timeout=8)
        for rf in radarr_roots:
            rp = str(rf.get("path") or "").strip()
            if rp:
                allowed_roots.add(os.path.normcase(os.path.abspath(rp)))
    except Exception as exc:
        return False, f"Could not query Radarr root folders: {exc}", {}

    if os.path.normcase(os.path.abspath(target_root)) not in allowed_roots:
        return False, f"Blocked: Target folder is not a configured Library or Radarr Root Folder ({target_root}).", {}

    if not os.path.isdir(win_long_path(target_root)):
        drive_root, _ = os.path.splitdrive(os.path.abspath(target_root))
        drive_anchor = (drive_root + os.sep) if drive_root else os.path.dirname(os.path.abspath(target_root))
        drive_online = os.path.isdir(drive_anchor)

        if not drive_online:
            return (
                False,
                f"Target drive ({get_drive_label(target_root)}) is offline or disconnected: {target_root}",
                {"needs_create": False, "drive_offline": True, "target_root_folder": target_root}
            )

        if not create_if_missing:
            return (
                False,
                f"Target root folder does not exist on {get_drive_label(target_root)}: {target_root}",
                {"needs_create": True, "target_root_folder": target_root}
            )

        try:
            os.makedirs(win_long_path(target_root), exist_ok=True)
            log_event(f"Created missing target root folder: {target_root}", "INFO")
        except OSError as exc:
            return False, f"Could not create target root folder ({target_root}): {exc}", {}

    _, raw_movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=15)
    movies_by_id = {m.get("id"): m for m in raw_movies}

    moved = 0
    already_there = 0
    failed = []
    moved_records = []  # Collect successfully moved movies; update Radarr API strictly AFTER external disk moves finish

    total_movies = len(movie_ids)
    jid = begin_job(
        f"Bulk Move: {collection_name}",
        f"Starting external robocopy move for {total_movies} movie(s)...",
        total=total_movies
    )
    log_event(f"Bulk Move started for '{collection_name}' via external robocopy: {total_movies} movie(s) -> {target_root}", "INFO")

    # PHASE 1: Execute external file moves on disk (no Radarr DB writes during disk I/O)
    for idx_m, mid in enumerate(movie_ids):
        m = movies_by_id.get(mid)
        if not m:
            failed.append(f"Movie ID {mid} not found in Radarr")
            update_job_progress(overall_current=idx_m + 1)
            continue

        title = m.get("title") or f"ID {mid}"
        src_path = os.path.normpath(m.get("path") or "")
        folder_name = os.path.basename(src_path.rstrip("\\/")) if src_path else sanitise_windows_filename(f"{title} ({m.get('year') or ''})")
        dst_path = os.path.normpath(os.path.join(target_root, folder_name))

        if src_path and os.path.normcase(os.path.abspath(src_path)) == os.path.normcase(os.path.abspath(dst_path)):
            already_there += 1
            update_job_progress(overall_current=idx_m + 1, status=f"Already in destination: {title}")
            continue

        try:
            update_job_progress(
                status=f"Moving ({idx_m + 1}/{total_movies}): {title} -> {target_root}",
                overall_current=idx_m,
                current_file=title,
                file_percent=0
            )
            if src_path and os.path.isdir(win_long_path(src_path)):
                _move_directory_with_external_command(
                    src_path,
                    dst_path,
                    overall_index=idx_m,
                    overall_total=total_movies
                )
            moved_records.append((mid, title, m, dst_path))
            update_job_progress(overall_current=idx_m + 1, file_percent=100)
            log_event(f"Bulk Move [{collection_name}]: Disk move complete via robocopy '{title}' -> {dst_path}", "INFO")
        except Exception as exc:
            failed.append(f"{title} (disk move): {exc}")
            update_job_progress(overall_current=idx_m + 1)
            log_event(f"Bulk Move [{collection_name}] disk move failed for '{title}': {exc}", "ERROR")

    # PHASE 2: Update Radarr v3 API AFTER all external disk moves have completed
    if moved_records:
        update_job_progress(status=f"Updating Radarr API paths for {len(moved_records)} moved movie(s)...")
    for mid, title, m, dst_path in moved_records:
        try:
            m_updated = dict(m)
            m_updated["rootFolderPath"] = target_root
            m_updated["path"] = dst_path
            radarr_api_request(cfg, f"/api/v3/movie/{mid}?moveFiles=false", method="PUT", payload=m_updated, timeout=12)
            moved += 1
            log_event(f"Bulk Move [{collection_name}]: Updated Radarr API path for '{title}' -> {dst_path}", "INFO")
        except Exception as exc:
            failed.append(f"{title} (Radarr API update): {exc}")
            log_event(f"Bulk Move [{collection_name}] Radarr API update failed for '{title}': {exc}", "ERROR")

    # Trigger a single consolidated Refresh/Rescan pass after all API updates complete
    if moved > 0:
        if moved == 1:
            trigger_radarr_rescan(cfg, radarr_movie_id=moved_records[0][0])
        else:
            trigger_radarr_rescan(cfg, radarr_movie_id=None)

    # Also update the native Radarr collection's rootFolderPath if a native collection ID was provided
    if collection_id is not None:
        try:
            _, coll_obj = radarr_api_request(cfg, f"/api/v3/collection/{int(collection_id)}", method="GET", timeout=8)
            if isinstance(coll_obj, dict) and coll_obj.get("id") is not None:
                coll_obj["rootFolderPath"] = target_root
                radarr_api_request(cfg, f"/api/v3/collection/{int(collection_id)}", method="PUT", payload=coll_obj, timeout=8)
        except Exception as exc:
            log_event(f"Could not update native collection rootFolderPath in Radarr: {exc}", "WARN")

    summary = f"Bulk move complete for '{collection_name}': {moved} moved, {already_there} already in destination" + (f", {len(failed)} failed." if failed else ".")
    finish_job(jid, summary, state="completed" if not failed else "warning")
    log_event(summary, "INFO" if not failed else "WARN")
    return (len(failed) == 0), summary, {"moved": moved, "already_there": already_there, "failed": failed}


def bulk_delete_collection(payload, cfg):
    """
    Bulk deletes an unwanted collection:
    - Optionally quarantines/recycles movie files on disk (delete_files=True),
    - Optionally adds every movie to Radarr's Import Exclusion list (add_import_exclusion=True),
    - Unmonitors native Radarr collection so Radarr doesn't re-add missing entries,
    - Removes custom collection entry from config.json if it is a custom collection.
    """
    collection_name = str(payload.get("collection_name") or "Collection").strip()
    collection_type = str(payload.get("collection_type") or "native").strip().lower()
    collection_id = payload.get("collection_id")
    custom_id = str(payload.get("custom_id") or "").strip()
    movie_ids = [int(x) for x in (payload.get("movie_ids") or []) if str(x).isdigit()]
    delete_files = bool(payload.get("delete_files", False))
    add_exclusion = bool(payload.get("add_import_exclusion", True))

    deleted_radarr = 0
    quarantined_folders = 0
    failed = []

    raw_movies_by_id = {}
    if movie_ids and cfg.get("radarr_url") and cfg.get("radarr_api_key"):
        try:
            _, raw_movies = radarr_api_request(cfg, "/api/v3/movie", method="GET", timeout=15)
            raw_movies_by_id = {m.get("id"): m for m in raw_movies}
        except Exception as exc:
            return False, f"Could not load Radarr movies: {exc}", {}

    jid = begin_job(f"Bulk Delete: {collection_name}", f"Deleting {len(movie_ids)} movie(s) from '{collection_name}'...", total=max(1, len(movie_ids)))
    log_event(
        f"Bulk Delete started for '{collection_name}' ({len(movie_ids)} movie(s) | "
        f"Delete/Quarantine Files: {delete_files} | Add Import Exclusion: {add_exclusion}).",
        "WARN"
    )

    for mid in movie_ids:
        m = raw_movies_by_id.get(mid)
        title = (m.get("title") if m else None) or f"Movie ID {mid}"
        movie_folder = os.path.normpath(m.get("path") or "") if (m and m.get("path")) else ""

        try:
            # Quarantine disk files ourselves using user's safety mode (Recycle Bin or Drive Quarantine)
            if delete_files and movie_folder and os.path.isdir(win_long_path(movie_folder)):
                if is_path_within_configured_roots(movie_folder, cfg):
                    quarantine_file(movie_folder, cfg)
                    quarantined_folders += 1
                else:
                    log_event(f"Skipped disk quarantine for '{title}' (outside configured roots): {movie_folder}", "WARN")

            # Remove from Radarr DB (deleteFiles=false because we handled disk quarantine safely above)
            excl_str = "true" if add_exclusion else "false"
            radarr_api_request(
                cfg,
                f"/api/v3/movie/{mid}?deleteFiles=false&addImportExclusion={excl_str}",
                method="DELETE",
                timeout=12
            )
            deleted_radarr += 1
        except Exception as exc:
            failed.append(f"{title}: {exc}")
            log_event(f"Bulk Delete [{collection_name}] failed for '{title}': {exc}", "ERROR")

    # Unmonitor native collection in Radarr so deleted/excluded movies aren't re-added
    if collection_type == "native" and collection_id is not None and cfg.get("radarr_url") and cfg.get("radarr_api_key"):
        try:
            _, coll_obj = radarr_api_request(cfg, f"/api/v3/collection/{int(collection_id)}", method="GET", timeout=8)
            if isinstance(coll_obj, dict) and coll_obj.get("id") is not None:
                coll_obj["monitored"] = False
                coll_obj["searchOnAdd"] = False
                radarr_api_request(cfg, f"/api/v3/collection/{int(collection_id)}", method="PUT", payload=coll_obj, timeout=8)
                log_event(f"Unmonitored native Radarr collection '{collection_name}' (ID {collection_id}).", "INFO")
        except Exception as exc:
            log_event(f"Could not unmonitor native Radarr collection '{collection_name}': {exc}", "WARN")

    # Remove custom collection definition from config.json if requested
    if collection_type == "custom" and custom_id:
        current = load_config()
        current["custom_collections"] = [c for c in current.get("custom_collections", []) if c.get("id") != custom_id]
        save_config(current)
        log_event(f"Removed custom collection '{collection_name}' from config.json.", "INFO")

    summary = (
        f"Bulk delete complete for '{collection_name}': {deleted_radarr} removed from Radarr"
        + (f" ({quarantined_folders} folder(s) quarantined)" if delete_files else " (disk files kept)")
        + (" [Import Exclusion added]" if add_exclusion else "")
        + (f", {len(failed)} failed." if failed else ".")
    )
    finish_job(jid, summary, state="completed" if not failed else "warning")
    log_event(summary, "INFO" if not failed else "WARN")
    return (len(failed) == 0), summary, {
        "deleted_radarr": deleted_radarr,
        "quarantined_folders": quarantined_folders,
        "failed": failed
    }


def save_custom_collection(payload, cfg):
    """Creates or updates a local Custom Collection in config.json and optionally syncs its Radarr tag(s)."""
    name = str(payload.get("name") or "").strip()
    if not name:
        return False, "Collection name is required.", None

    cid = str(payload.get("id") or "").strip().lower()
    tmdb_ids = [str(x).strip() for x in (payload.get("tmdb_ids") or []) if str(x).strip().isdigit()]
    raw_single_tag = str(payload.get("radarr_tag") or "").strip()
    raw_tags_list = payload.get("radarr_tags") if isinstance(payload.get("radarr_tags"), list) else []
    combined_tags = list(raw_tags_list)
    if raw_single_tag:
        for part in re.split(r"[\s,;]+", raw_single_tag):
            if part.strip():
                combined_tags.append(part.strip())
    sync_tag = bool(payload.get("sync_tag", False))
    explicit_root = str(payload.get("root_folder") or "").strip()

    existing = list(cfg.get("custom_collections", []))
    updated = False
    existing_root = ""
    if cid:
        for item in existing:
            if item.get("id") == cid:
                existing_root = str(item.get("root_folder") or "").strip()
                break
    final_root = os.path.normpath(explicit_root) if explicit_root else existing_root
    new_entry = {
        "id": cid,
        "name": name,
        "tmdb_ids": tmdb_ids,
        "radarr_tags": combined_tags,
        "root_folder": final_root
    }

    if cid:
        for i, item in enumerate(existing):
            if item.get("id") == cid:
                existing[i] = new_entry
                updated = True
                break
    if not updated:
        existing.append(new_entry)

    cfg["custom_collections"] = normalise_custom_collections(existing)
    saved = save_config(cfg)
    saved_entry = next((c for c in saved["custom_collections"] if c["name"] == name), saved["custom_collections"][-1])

    tagged_count = 0
    if sync_tag and saved_entry.get("radarr_tags"):
        try:
            tagged_count = _sync_custom_collection_tag_to_radarr(saved, saved_entry)
        except Exception as exc:
            log_event(f"Custom collection saved, but Radarr tag sync failed: {exc}", "WARN")

    msg = f"Saved custom collection '{saved_entry['name']}' ({len(saved_entry['tmdb_ids'])} movie(s)"
    if saved_entry.get("radarr_tags"):
        msg += f", tags: {', '.join(saved_entry['radarr_tags'])}"
    msg += ")"
    if sync_tag and saved_entry.get("radarr_tags"):
        msg += f" | Synced tag(s) to {tagged_count} Radarr movie(s)."
    log_event(msg, "INFO")
    return True, msg, saved_entry


def toggle_collection_location_ignore(payload, cfg):
    """Persists or removes a collection:tmdb_id ignore rule in config.json."""
    ignore_key = str(payload.get("ignore_key") or "").strip().lower()
    ignore_flag = bool(payload.get("ignore", True))
    movie_title = str(payload.get("title") or ignore_key).strip()
    if not ignore_key:
        return False, "Missing ignore key."

    ignores = normalise_collection_ignores(cfg.get("collection_location_ignores", []))
    if ignore_flag:
        if ignore_key not in ignores:
            ignores.append(ignore_key)
        action_txt = f"Ignored location warning for '{movie_title}' (remembered in config.json)."
    else:
        ignores = [k for k in ignores if k != ignore_key]
        action_txt = f"Restored location check for '{movie_title}'."

    cfg["collection_location_ignores"] = ignores
    save_config(cfg)
    log_event(f"Collections: {action_txt}", "INFO")
    return True, action_txt


def set_collection_root_folder(payload, cfg):
    """
    Updates a native or custom collection's root folder and immediately scans that root folder
    to count existing folders and re-evaluate misplaced movies for that collection.
    """
    coll_type = str(payload.get("collection_type") or "native").strip().lower()
    coll_name = str(payload.get("collection_name") or "Collection").strip()
    coll_id = payload.get("collection_id")
    custom_id = str(payload.get("custom_id") or "").strip().lower()
    new_root = os.path.normpath(str(payload.get("root_folder") or "").strip())

    if not new_root:
        return False, "Please select a valid root folder.", {}

    if coll_type == "native":
        if coll_id is None:
            return False, "This native collection has no Radarr collection ID to update.", {}
        try:
            _, coll_obj = radarr_api_request(cfg, f"/api/v3/collection/{int(coll_id)}", method="GET", timeout=8)
            if not isinstance(coll_obj, dict) or coll_obj.get("id") is None:
                return False, f"Collection ID {coll_id} not found in Radarr.", {}
            coll_obj["rootFolderPath"] = new_root
            radarr_api_request(cfg, f"/api/v3/collection/{int(coll_id)}", method="PUT", payload=coll_obj, timeout=10)
        except Exception as exc:
            return False, f"Could not update collection root in Radarr: {exc}", {}
    else:
        found = False
        customs = normalise_custom_collections(cfg.get("custom_collections", []))
        for c in customs:
            if c["id"] == custom_id:
                c["root_folder"] = new_root
                found = True
                break
        if not found:
            return False, f"Custom collection '{custom_id}' not found.", {}
        cfg["custom_collections"] = customs
        save_config(cfg)

    # Immediate lightweight scan of the newly selected root folder
    root_online = os.path.isdir(win_long_path(new_root))
    folder_count = 0
    if root_online:
        try:
            folder_count = sum(
                1 for e in os.listdir(win_long_path(new_root))
                if os.path.isdir(win_long_path(os.path.join(new_root, e)))
                and not e.lower().startswith("_radarrcompanion_quarantine")
            )
        except OSError:
            pass

    scan_note = f"scanned {folder_count} movie folder(s) in {new_root}" if root_online else f"folder is currently offline/missing on disk ({new_root})"
    msg = f"Updated root folder for '{coll_name}' to {new_root} — {scan_note}."
    log_event(f"Collections: {msg}", "INFO" if root_online else "WARN")
    return True, msg, {"root_folder": new_root, "online": root_online, "folder_count": folder_count}


def create_custom_collection_root_folder(payload, cfg):
    """
    Creates a dedicated root folder on disk for any Native or Custom Collection, registers it
    in Radarr (/api/v3/rootfolder) if connected, adds it to local library_folders in config.json,
    optionally saves the parent folder as the preset collections_root_parent, and assigns the
    new folder as the collection's root folder.
    """
    raw_full = str(payload.get("folder_path") or "").strip()
    parent_folder = str(payload.get("parent_folder") or "").strip()
    folder_name = sanitise_windows_filename(str(payload.get("folder_name") or "").strip())
    coll_type = str(payload.get("collection_type") or "custom").strip().lower()
    coll_id = payload.get("collection_id")
    custom_id = str(payload.get("custom_id") or "").strip().lower()
    collection_name = str(payload.get("collection_name") or folder_name or "Collection").strip()
    save_as_preset = bool(payload.get("save_as_preset", False))

    if raw_full:
        target_path = os.path.normpath(raw_full)
    elif parent_folder and folder_name:
        target_path = os.path.normpath(os.path.join(parent_folder, folder_name))
    else:
        return False, "Please provide a parent folder and folder name (or full folder path).", {}

    if not is_drive_mounted(target_path):
        return False, f"Cannot create folder: drive {get_drive_label(target_path)} is offline or disconnected.", {}

    jid = begin_job(f"Create Collection Root: {collection_name}", f"Creating root folder {target_path}...", total=1)
    try:
        os.makedirs(win_long_path(target_path), exist_ok=True)
    except OSError as exc:
        finish_job(jid, f"Failed to create folder: {exc}", state="failed")
        return False, f"Could not create folder on disk ({target_path}): {exc}", {}

    # Register in Radarr /api/v3/rootfolder if connected and not already present
    radarr_note = ""
    radarr_registered = False
    if cfg.get("radarr_url") and cfg.get("radarr_api_key"):
        try:
            _, existing_roots = radarr_api_request(cfg, "/api/v3/rootfolder", method="GET", timeout=8)
            norm_target = os.path.normcase(os.path.abspath(target_path))
            already_in_radarr = any(
                os.path.normcase(os.path.abspath(str(rf.get("path") or ""))) == norm_target
                for rf in (existing_roots or [])
                if rf.get("path")
            )
            if already_in_radarr:
                radarr_registered = True
                radarr_note = "already registered in Radarr"
            else:
                radarr_api_request(cfg, "/api/v3/rootfolder", method="POST", payload={"path": target_path}, timeout=10)
                radarr_registered = True
                radarr_note = "registered as Radarr root folder"
        except Exception as exc:
            radarr_note = f"Radarr root registration skipped ({exc})"
            log_event(f"Collection root created on disk, but Radarr root registration failed: {exc}", "WARN")

    # Add to local library_folders in config.json if not already present
    norm_target = os.path.normcase(os.path.abspath(target_path))
    lib_folders = list(cfg.get("library_folders", []))
    library_added = False
    if not any(os.path.normcase(os.path.abspath(p)) == norm_target for p in lib_folders if str(p).strip()):
        lib_folders.append(target_path)
        cfg["library_folders"] = sort_paths_by_drive_and_path(lib_folders)
        library_added = True

    # Optionally save parent folder as the preset location for future collections
    preset_saved = False
    if save_as_preset and parent_folder:
        cfg["collections_root_parent"] = os.path.normpath(parent_folder)
        preset_saved = True

    # Assign directly to the collection (native in Radarr OR custom in config.json)
    assigned_collection = False
    assign_err = ""
    if coll_type == "native" and coll_id is not None and str(coll_id).isdigit():
        if cfg.get("radarr_url") and cfg.get("radarr_api_key"):
            try:
                _, coll_obj = radarr_api_request(cfg, f"/api/v3/collection/{int(coll_id)}", method="GET", timeout=8)
                if isinstance(coll_obj, dict) and coll_obj.get("id") is not None:
                    coll_obj["rootFolderPath"] = target_path
                    radarr_api_request(cfg, f"/api/v3/collection/{int(coll_id)}", method="PUT", payload=coll_obj, timeout=10)
                    assigned_collection = True
                    log_event(f"Updated Radarr native collection '{collection_name}' (ID {coll_id}) rootFolderPath -> {target_path}", "INFO")
                else:
                    assign_err = f"Collection ID {coll_id} not found in Radarr"
            except Exception as exc:
                assign_err = str(exc)
                log_event(f"Could not update native Radarr collection root for '{collection_name}': {exc}", "WARN")
    elif custom_id:
        customs = normalise_custom_collections(cfg.get("custom_collections", []))
        for c in customs:
            if c["id"] == custom_id:
                c["root_folder"] = target_path
                assigned_collection = True
                break
        cfg["custom_collections"] = customs

    save_config(cfg)
    details = [f"Created folder: {target_path}"]
    if radarr_note:
        details.append(radarr_note)
    if library_added:
        details.append("added to Library folders")
    if assigned_collection:
        details.append(f"assigned to '{collection_name}'")
    elif assign_err:
        details.append(f"Radarr collection update warning: {assign_err}")
    if preset_saved:
        details.append("saved preset parent")
    msg = " — ".join(details) + "."
    finish_job(jid, msg, state="completed" if not assign_err else "warning")
    log_event(f"Collections: {msg}", "INFO" if not assign_err else "WARN")
    return True, msg, {
        "path": target_path,
        "radarr_registered": radarr_registered,
        "library_added": library_added,
        "assigned_collection": assigned_collection,
        "assign_error": assign_err,
        "preset_saved": preset_saved
    }


def delete_custom_collection_only(custom_id, cfg):
    """Deletes a custom collection definition from config.json without deleting movies."""
    before = len(cfg.get("custom_collections", []))
    cfg["custom_collections"] = [c for c in cfg.get("custom_collections", []) if c.get("id") != custom_id]
    if len(cfg["custom_collections"]) == before:
        return False, "Custom collection not found."
    save_config(cfg)
    log_event(f"Deleted custom collection definition '{custom_id}' (movies untouched).", "INFO")
    return True, "Custom collection removed (movie files & Radarr entries untouched)."


# --- LIBRARY MAINTENANCE MODULE (SPINNING HDD MOVIE DEFRAG VIA SYSINTERNALS CONTIG) ---
def detect_windows_drive_media_types(force_refresh=False):
    """
    Maps Windows drive letters (e.g. 'D:') to physical media types ('HDD', 'SSD', 'Unknown')
    using PowerShell Get-Partition -> Get-Disk -> Get-PhysicalDisk.
    Strictly skips non-HDD drives (fail-safe: SSD and Unknown are never defragged).
    """
    global DRIVE_MEDIA_CACHE, DRIVE_MEDIA_CACHE_TS
    now_ts = time.time()
    if not force_refresh and DRIVE_MEDIA_CACHE and (now_ts - DRIVE_MEDIA_CACHE_TS) < 120:
        return dict(DRIVE_MEDIA_CACHE)

    media_map = {}
    if os.name != "nt":
        return media_map

    ps_script = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "$out = @{}; "
        "$physByNum = @{}; "
        "foreach ($pd in (Get-PhysicalDisk)) { "
        "  if ($null -ne $pd.DeviceId) { $physByNum[[string]$pd.DeviceId] = $pd } "
        "}; "
        "foreach ($p in (Get-Partition | Where-Object { $_.DriveLetter })) { "
        "  $dl = ($p.DriveLetter.ToString().ToUpper() + ':'); "
        "  $pd = $physByNum[[string]$p.DiskNumber]; "
        "  $mt = 'Unknown'; "
        "  if ($pd) { "
        "    if ($pd.MediaType -eq 'SSD' -or $pd.MediaType -eq 4) { $mt = 'SSD' } "
        "    elseif ($pd.MediaType -eq 'HDD' -or $pd.MediaType -eq 3) { $mt = 'HDD' } "
        "    elseif ($pd.SpindleSpeed -gt 0) { $mt = 'HDD' } "
        "  }; "
        "  $out[$dl] = $mt "
        "}; "
        "$out | ConvertTo-Json -Compress"
    )

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        raw_out = (proc.stdout or "").strip()
        if raw_out:
            parsed = json.loads(raw_out)
            if isinstance(parsed, dict):
                for k, v in parsed.items():
                    media_map[str(k).upper()] = str(v)
    except Exception as exc:
        log_event(f"Drive media type detection fallback (treating unverified drives as Unknown): {exc}", "WARN")

    DRIVE_MEDIA_CACHE = dict(media_map)
    DRIVE_MEDIA_CACHE_TS = now_ts
    return media_map


def resolve_contig_executable(cfg):
    """Locates Sysinternals contig64.exe / contig.exe from config, script directory, or PATH."""
    candidates = []
    configured = str(cfg.get("contig_path") or "").strip().strip('"')
    if configured:
        candidates.append(configured)
    for name in ("contig64.exe", "contig.exe"):
        candidates.append(os.path.join(BASE_DIR, name))
        candidates.append(os.path.join(BASE_DIR, "tools", name))
        found_in_path = shutil.which(name)
        if found_in_path:
            candidates.append(found_in_path)

    for cand in candidates:
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def resolve_windows_defrag_executable():
    """Locates the built-in Windows defrag.exe utility."""
    if os.name != "nt":
        return None
    sys_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidates = [
        os.path.join(sys_root, "System32", "defrag.exe"),
        os.path.join(sys_root, "Sysnative", "defrag.exe"),
        shutil.which("defrag.exe")
    ]
    for cand in candidates:
        if cand and os.path.isfile(cand):
            return os.path.abspath(cand)
    return None


def resolve_defrag_engine(cfg):
    """
    Selects the active defragmentation engine:
    1) Sysinternals contig64.exe / contig.exe (file-level movie defrag) if available,
    2) Built-in Windows defrag.exe (drive-level HDD defrag) as automatic fallback.
    """
    contig_exe = resolve_contig_executable(cfg)
    if contig_exe:
        return "contig", contig_exe
    win_defrag = resolve_windows_defrag_executable()
    if win_defrag:
        return "windows_defrag", win_defrag
    return None, None


def is_windows_admin():
    """Returns True if the current process is running with Windows Administrator privileges."""
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def sync_maintenance_snapshot_to_config(cfg, force_refresh_drives=True):
    """
    Detects active defrag engine + physical drive media types (HDD/SSD) and persists
    the full maintenance snapshot (schedule, paths, engine, drive types) to config.json.
    """
    engine_type, engine_exe = resolve_defrag_engine(cfg)
    drives = get_maintenance_drive_report(cfg, force_refresh=force_refresh_drives)

    persisted_media = dict(cfg.get("maintenance_drive_media_types") or {})
    for d in drives:
        persisted_media[d["drive"].upper()] = d["media_type"]

    cfg["maintenance_engine"] = engine_type or "none"
    cfg["maintenance_engine_path"] = engine_exe or ""
    cfg["maintenance_drive_media_types"] = persisted_media
    saved = save_config(cfg)
    return saved, drives


def get_maintenance_drive_report(cfg, force_refresh=False):
    """Returns per-drive HDD/SSD classification and defrag eligibility for configured library folders."""
    media_map = detect_windows_drive_media_types(force_refresh=force_refresh)
    cached_cfg_media = cfg.get("maintenance_drive_media_types") or {}
    overrides = cfg.get("maintenance_drive_overrides") or {}
    by_drive = {}

    for folder in cfg.get("library_folders", []):
        drive = get_drive_label(folder)
        drive_key = drive.upper()
        online = os.path.isdir(folder)
        if drive.startswith("\\\\"):
            detected_type = "Network"
        else:
            detected_type = media_map.get(drive_key) or "Unknown"

        override_type = overrides.get(drive_key)
        # Auto-detected SSD is never overridden for hardware safety; Unknown drives can be manually set to HDD or SSD
        if detected_type == "SSD":
            media_type = "SSD"
            is_manual = False
            can_toggle = False
        elif detected_type == "HDD":
            media_type = "HDD"
            is_manual = False
            can_toggle = False
        elif detected_type == "Network":
            media_type = "Network"
            is_manual = False
            can_toggle = False
        else:
            # Detected as Unknown (e.g. USB enclosure / RAID / unqueried)
            if override_type in ("HDD", "SSD"):
                media_type = override_type
                is_manual = True
            else:
                # Fall back to cached auto-detected type if it wasn't Unknown
                cached_val = cached_cfg_media.get(drive_key)
                media_type = cached_val if cached_val in ("HDD", "SSD") else "Unknown"
                is_manual = False
            can_toggle = True

        eligible = (media_type == "HDD") and online
        if media_type == "SSD":
            reason = "Protected: SSD (Manual override — strictly skipped)" if is_manual else "Protected: SSD detected (strictly skipped)"
        elif media_type == "Network":
            reason = "Skipped: Network / UNC share"
        elif media_type != "HDD":
            reason = "Unknown drive type — click the badge to set as HDD or SSD"
        elif not online:
            reason = "Offline: Drive currently disconnected"
        else:
            reason = "Eligible: Spinning HDD (Manual override — Online)" if is_manual else "Eligible: Spinning HDD (Online)"

        if drive not in by_drive:
            by_drive[drive] = {
                "drive": drive,
                "media_type": media_type,
                "detected_type": detected_type,
                "manual_override": is_manual,
                "can_toggle": can_toggle,
                "online": online,
                "eligible": eligible,
                "reason": reason,
                "folders": []
            }
        else:
            by_drive[drive]["online"] = by_drive[drive]["online"] or online
            by_drive[drive]["eligible"] = (by_drive[drive]["media_type"] == "HDD") and by_drive[drive]["online"]

        by_drive[drive]["folders"].append(folder)

    return sorted(by_drive.values(), key=lambda d: d["drive"])


def compute_next_maintenance_run(cfg, now_dt=None):
    """Calculates the next scheduled maintenance datetime based on user settings."""
    if not cfg.get("maintenance_enabled"):
        return None

    now = now_dt or datetime.now()
    maint = normalise_maintenance_config(cfg)
    hour, minute = (int(x) for x in maint["maintenance_time"].split(":"))
    sched = maint["maintenance_schedule"]

    if sched == "daily":
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=1)
        return candidate

    if sched in ("weekly", "biweekly"):
        target_wd = VALID_WEEKDAYS.index(maint["maintenance_day_of_week"])
        days_ahead = (target_wd - now.weekday()) % 7
        candidate = (now + timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= now:
            candidate += timedelta(days=7)

        if sched == "biweekly" and maint["maintenance_last_run"]:
            try:
                last_dt = datetime.strptime(maint["maintenance_last_run"], "%Y-%m-%d %H:%M:%S")
                while (candidate - last_dt).days < 13:
                    candidate += timedelta(days=7)
            except ValueError:
                pass
        return candidate

    # Monthly (day 1..28)
    dom = maint["maintenance_day_of_month"]
    candidate = now.replace(day=dom, hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= now:
        year = now.year + (1 if now.month == 12 else 0)
        month = 1 if now.month == 12 else now.month + 1
        candidate = candidate.replace(year=year, month=month, day=dom)
    return candidate


def is_maintenance_due(cfg, now_dt=None):
    """Returns True if scheduled maintenance is enabled and due at now_dt."""
    if not cfg.get("maintenance_enabled"):
        return False

    now = now_dt or datetime.now()
    maint = normalise_maintenance_config(cfg)
    hour, minute = (int(x) for x in maint["maintenance_time"].split(":"))
    sched = maint["maintenance_schedule"]

    last_dt = None
    if maint["maintenance_last_run"]:
        try:
            last_dt = datetime.strptime(maint["maintenance_last_run"], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            last_dt = None

    scheduled_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now < scheduled_today:
        return False

    if sched == "daily":
        return last_dt is None or last_dt < scheduled_today

    if sched == "weekly":
        target_wd = VALID_WEEKDAYS.index(maint["maintenance_day_of_week"])
        if now.weekday() != target_wd:
            return False
        return last_dt is None or last_dt < scheduled_today

    if sched == "biweekly":
        target_wd = VALID_WEEKDAYS.index(maint["maintenance_day_of_week"])
        if now.weekday() != target_wd:
            return False
        if last_dt is None:
            return True
        return last_dt < scheduled_today and (now - last_dt).days >= 13

    if sched == "monthly":
        if now.day != maint["maintenance_day_of_month"]:
            return False
        return last_dt is None or last_dt < scheduled_today

    return False


def _run_defrag_worker(cfg, trigger_source="manual"):
    """
    Background worker that defragments confirmed spinning HDDs.
    Uses Sysinternals Contig (file-level) when available, or falls back to
    Windows built-in defrag.exe (drive-level, spinning HDDs only).
    """
    engine_type, engine_exe = resolve_defrag_engine(cfg)
    if not engine_type or not engine_exe:
        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["running"] = False
            MAINTENANCE_STATE["last_result"] = "Aborted: Neither Sysinternals contig64.exe nor Windows defrag.exe was found."
        log_event("Library Maintenance aborted: No defragmentation engine available.", "ERROR")
        return

    drives = get_maintenance_drive_report(cfg, force_refresh=True)
    eligible_drives = []
    eligible_folders = []
    skipped_ssds = []

    for d in drives:
        if d["eligible"]:
            eligible_drives.append(d["drive"])
            eligible_folders.extend([f for f in d["folders"] if os.path.isdir(f)])
        elif d["media_type"] == "SSD":
            skipped_ssds.append(d["drive"])

    with MAINTENANCE_LOCK:
        MAINTENANCE_STATE["skipped_ssd_drives"] = skipped_ssds

    if skipped_ssds:
        log_event(f"Library Maintenance SSD Protection: strictly skipping SSD drive(s) {', '.join(skipped_ssds)}.", "INFO")

    # --- FALLBACK ENGINE: WINDOWS BUILT-IN DEFRAG.EXE (DRIVE-LEVEL HDD ONLY) ---
    if engine_type == "windows_defrag":
        if not eligible_drives:
            now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with MAINTENANCE_LOCK:
                MAINTENANCE_STATE["running"] = False
                MAINTENANCE_STATE["last_run_at"] = now_str
                MAINTENANCE_STATE["last_result"] = "Completed: No online spinning HDDs eligible for defragmentation."
            cfg_latest = load_config()
            cfg_latest["maintenance_last_run"] = now_str
            save_config(cfg_latest)
            log_event(f"Library Maintenance ({trigger_source}): No online spinning HDDs found.", "INFO")
            return

        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["total_files"] = len(eligible_drives)

        log_event(
            f"Library Maintenance ({trigger_source}) started via Windows built-in defrag.exe fallback "
            f"(contig64.exe not found): {len(eligible_drives)} spinning HDD(s) queued ({', '.join(eligible_drives)}).",
            "INFO"
        )

        processed = 0
        defragged = 0
        failed_drives = 0
        cancelled = False

        for drv in eligible_drives:
            if MAINTENANCE_STOP_EVENT.is_set():
                cancelled = True
                break

            with MAINTENANCE_LOCK:
                MAINTENANCE_STATE["current_drive"] = drv
                MAINTENANCE_STATE["current_file"] = f"Windows defrag.exe {drv} /D /U"

            try:
                proc = subprocess.run(
                    [engine_exe, drv, "/D", "/U"],
                    capture_output=True,
                    text=True,
                    timeout=7200,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
                )
                if proc.returncode == 0:
                    defragged += 1
                    log_event(f"Windows defrag.exe completed successfully on spinning HDD {drv}.", "INFO")
                else:
                    failed_drives += 1
                    lines = [ln.strip() for ln in (proc.stderr or proc.stdout or "").splitlines() if ln.strip()]
                    brief = lines[-1] if lines else f"exit code {proc.returncode}"
                    if "0x89000018" in brief.lower() or "privilege" in brief.lower() or "administrator" in brief.lower() or not is_windows_admin():
                        brief = f"{brief} (Run Radarr Companion as Administrator for built-in Windows defrag.exe)"
                    log_event(f"Windows defrag.exe failed on {drv}: {brief}", "WARN")
            except subprocess.TimeoutExpired:
                failed_drives += 1
                log_event(f"Windows defrag.exe timed out on {drv} after 2 hours.", "WARN")
            except Exception as exc:
                failed_drives += 1
                log_event(f"Windows defrag.exe error on {drv}: {exc}", "WARN")

            processed += 1
            with MAINTENANCE_LOCK:
                MAINTENANCE_STATE["processed_files"] = processed
                MAINTENANCE_STATE["defragged_files"] = defragged

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if cancelled:
            summary = f"Stopped by user after {processed}/{len(eligible_drives)} HDD(s) via Windows defrag.exe."
            log_event(f"Library Maintenance stopped: {summary}", "WARN")
        else:
            if failed_drives and not defragged and not is_windows_admin():
                summary = f"Windows defrag.exe requires Administrator privileges ({failed_drives} HDD(s) failed). Run as Administrator or install contig64.exe."
            else:
                summary = f"Completed via Windows defrag.exe: {defragged}/{len(eligible_drives)} spinning HDD(s) defragged" + (f" ({failed_drives} failed)." if failed_drives else ".")
            log_event(f"Library Maintenance ({trigger_source}) finished: {summary}", "INFO" if not failed_drives else "WARN")
            if defragged > 0 or failed_drives == 0:
                cfg_latest = load_config()
                cfg_latest["maintenance_last_run"] = now_str
                save_config(cfg_latest)

        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["running"] = False
            MAINTENANCE_STATE["current_drive"] = ""
            MAINTENANCE_STATE["current_file"] = ""
            MAINTENANCE_STATE["last_run_at"] = now_str if (not cancelled and (defragged > 0 or failed_drives == 0)) else MAINTENANCE_STATE["last_run_at"]
            MAINTENANCE_STATE["last_result"] = summary
        return

    # --- PRIMARY ENGINE: SYSINTERNALS CONTIG (FILE-LEVEL MOVIE DEFRAG) ---
    movie_files = []
    seen = set()
    for folder in eligible_folders:
        for dirpath, dirnames, filenames in os.walk(folder):
            dirnames[:] = [
                dn for dn in dirnames
                if not dn.startswith("_RadarrCompanion_Quarantine")
                and dn not in ("$RECYCLE.BIN", "System Volume Information")
            ]
            for fname in filenames:
                ext = os.path.splitext(fname)[1].lower()
                if ext not in VIDEO_EXTENSIONS:
                    continue
                full_path = os.path.abspath(os.path.join(dirpath, fname))
                norm_key = os.path.normcase(full_path)
                if norm_key in seen:
                    continue
                seen.add(norm_key)
                try:
                    if os.path.getsize(win_long_path(full_path)) < MIN_MOVIE_SIZE_BYTES:
                        continue
                except OSError:
                    continue
                movie_files.append(full_path)

    with MAINTENANCE_LOCK:
        MAINTENANCE_STATE["total_files"] = len(movie_files)

    if not movie_files:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["running"] = False
            MAINTENANCE_STATE["last_run_at"] = now_str
            MAINTENANCE_STATE["last_result"] = "Completed: No eligible movie files found on online spinning HDDs."
        cfg_latest = load_config()
        cfg_latest["maintenance_last_run"] = now_str
        save_config(cfg_latest)
        log_event(f"Library Maintenance ({trigger_source}): No eligible movie files found on online spinning HDDs.", "INFO")
        return

    log_event(
        f"Library Maintenance ({trigger_source}) started via {os.path.basename(engine_exe)}: "
        f"{len(movie_files)} movie file(s) queued across spinning HDDs.",
        "INFO"
    )

    processed = 0
    defragged = 0
    skipped_locked = 0
    cancelled = False

    for fpath in movie_files:
        if MAINTENANCE_STOP_EVENT.is_set():
            cancelled = True
            break

        drv = get_drive_label(fpath)
        fname = os.path.basename(fpath)
        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["current_drive"] = drv
            MAINTENANCE_STATE["current_file"] = fname

        if not is_path_within_configured_roots(fpath, cfg):
            processed += 1
            continue

        if is_file_locked(fpath):
            skipped_locked += 1
            processed += 1
            with MAINTENANCE_LOCK:
                MAINTENANCE_STATE["processed_files"] = processed
                MAINTENANCE_STATE["skipped_locked"] = skipped_locked
            continue

        try:
            proc = subprocess.run(
                [engine_exe, "-nobanner", "-accepteula", fpath],
                capture_output=True,
                text=True,
                timeout=900,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            if proc.returncode == 0:
                defragged += 1
            else:
                err_txt = (proc.stderr or proc.stdout or "").strip().splitlines()
                brief = err_txt[-1] if err_txt else f"exit code {proc.returncode}"
                log_event(f"Contig warning for {fname}: {brief}", "WARN")
        except subprocess.TimeoutExpired:
            log_event(f"Contig timed out after 15 min on {fname}; moving to next file.", "WARN")
        except Exception as exc:
            log_event(f"Contig error on {fname}: {exc}", "WARN")

        processed += 1
        with MAINTENANCE_LOCK:
            MAINTENANCE_STATE["processed_files"] = processed
            MAINTENANCE_STATE["defragged_files"] = defragged

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if cancelled:
        summary = f"Stopped by user after {processed}/{len(movie_files)} file(s) ({defragged} processed, {skipped_locked} locked skipped)."
        log_event(f"Library Maintenance stopped: {summary}", "WARN")
    else:
        summary = f"Completed: {defragged}/{len(movie_files)} movie file(s) defragged on HDDs ({skipped_locked} locked skipped)."
        log_event(f"Library Maintenance ({trigger_source}) finished: {summary}", "INFO")
        cfg_latest = load_config()
        cfg_latest["maintenance_last_run"] = now_str
        save_config(cfg_latest)

    with MAINTENANCE_LOCK:
        MAINTENANCE_STATE["running"] = False
        MAINTENANCE_STATE["current_drive"] = ""
        MAINTENANCE_STATE["current_file"] = ""
        MAINTENANCE_STATE["last_run_at"] = now_str if not cancelled else MAINTENANCE_STATE["last_run_at"]
        MAINTENANCE_STATE["last_result"] = summary


def start_maintenance_run(cfg, trigger_source="manual"):
    """Starts a background spinning-HDD defragmentation run if one is not already active."""
    engine_type, engine_exe = resolve_defrag_engine(cfg)
    if not engine_type or not engine_exe:
        return False, "Neither Sysinternals contig64.exe nor Windows built-in defrag.exe was found."

    engine_label = "Sysinternals Contig" if engine_type == "contig" else "Windows built-in defrag.exe"
    with MAINTENANCE_LOCK:
        if MAINTENANCE_STATE["running"]:
            return False, "Library Maintenance defragmentation is already running."
        MAINTENANCE_STOP_EVENT.clear()
        MAINTENANCE_STATE.update({
            "running": True,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "current_drive": "",
            "current_file": f"Initialising {engine_label}...",
            "processed_files": 0,
            "total_files": 0,
            "defragged_files": 0,
            "skipped_locked": 0,
            "skipped_ssd_drives": [],
            "last_result": f"Running via {engine_label} ({trigger_source})..."
        })

    worker = threading.Thread(target=_run_defrag_worker, args=(dict(cfg), trigger_source), daemon=True)
    worker.start()
    return True, f"Library Maintenance started via {engine_label} on eligible spinning HDDs."


def stop_maintenance_run():
    """Signals the active defragmentation worker to stop cleanly after the current file/drive."""
    with MAINTENANCE_LOCK:
        if not MAINTENANCE_STATE["running"]:
            return False, "No Library Maintenance job is currently running."
        MAINTENANCE_STOP_EVENT.set()
    log_event("Stop requested for Library Maintenance (will halt cleanly after current item).", "WARN")
    return True, "Stop requested. Will finish the current item and halt cleanly."


def get_maintenance_status_payload(cfg, force_refresh_drives=False):
    """Builds the JSON status payload for the Library Maintenance UI."""
    engine_type, engine_exe = resolve_defrag_engine(cfg)
    contig_exe = resolve_contig_executable(cfg)
    next_dt = compute_next_maintenance_run(cfg)
    with MAINTENANCE_LOCK:
        state_copy = dict(MAINTENANCE_STATE)
        if not state_copy["last_run_at"] and cfg.get("maintenance_last_run"):
            state_copy["last_run_at"] = cfg.get("maintenance_last_run")

    return {
        "enabled": bool(cfg.get("maintenance_enabled", False)),
        "schedule": cfg.get("maintenance_schedule", "weekly"),
        "day_of_week": cfg.get("maintenance_day_of_week", "Sunday"),
        "day_of_month": cfg.get("maintenance_day_of_month", 1),
        "time": cfg.get("maintenance_time", "03:00"),
        "contig_path": cfg.get("contig_path", ""),
        "contig_detected": bool(contig_exe),
        "contig_resolved_path": contig_exe or "",
        "active_engine": engine_type or "none",
        "active_engine_path": engine_exe or "",
        "is_admin": is_windows_admin(),
        "next_run": next_dt.strftime("%Y-%m-%d %H:%M") if next_dt else "Disabled",
        "drives": get_maintenance_drive_report(cfg, force_refresh=force_refresh_drives),
        "state": state_copy
    }


def maintenance_scheduler_loop():
    """In-app background scheduler loop checking every 45 seconds if HDD defrag is due."""
    while True:
        time.sleep(45)
        try:
            cfg = load_config()
            if not cfg.get("maintenance_enabled"):
                continue
            with MAINTENANCE_LOCK:
                already_running = MAINTENANCE_STATE["running"]
            if already_running:
                continue
            if is_maintenance_due(cfg):
                ok, msg = start_maintenance_run(cfg, trigger_source="scheduled")
                if not ok:
                    log_event(f"Scheduled Library Maintenance skipped: {msg}", "WARN")
        except Exception as exc:
            log_event(f"Library Maintenance scheduler error: {exc}", "WARN")


def open_native_windows_dialog(mode="folder", title="Select Path"):
    """
    Opens a native Windows Explorer picker dialog (folder or executable) via PowerShell
    System.Windows.Forms so the local web UI receives the true filesystem path.
    """
    if os.name != "nt":
        return False, "", "Native browse dialogs are only supported on Windows."

    safe_title = str(title).replace("'", "''")
    if mode == "contig":
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; $f.MinimizeBox = $false; "
            "$dlg = New-Object System.Windows.Forms.OpenFileDialog; "
            f"$dlg.Title = '{safe_title}'; "
            "$dlg.Filter = 'Sysinternals Contig (contig64.exe;contig.exe)|contig64.exe;contig.exe|Executables (*.exe)|*.exe|All Files (*.*)|*.*'; "
            "$dlg.CheckFileExists = $true; "
            "if ($dlg.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.FileName }; "
            "$f.Dispose()"
        )
    else:
        ps_script = (
            "Add-Type -AssemblyName System.Windows.Forms; "
            "$f = New-Object System.Windows.Forms.Form; $f.TopMost = $true; $f.MinimizeBox = $false; "
            "$dlg = New-Object System.Windows.Forms.FolderBrowserDialog; "
            f"$dlg.Description = '{safe_title}'; "
            "$dlg.ShowNewFolderButton = $true; "
            "if ($dlg.ShowDialog($f) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $dlg.SelectedPath }; "
            "$f.Dispose()"
        )

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
            capture_output=True,
            text=True,
            timeout=180,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        selected = (proc.stdout or "").strip()
        if selected:
            return True, os.path.normpath(selected), ""
        return True, "", "Cancelled"
    except subprocess.TimeoutExpired:
        return False, "", "Browse dialog timed out."
    except Exception as exc:
        return False, "", f"Could not open Windows browse dialog: {exc}"


# --- HTTP API SERVER ---
class CompanionHandler(BaseHTTPRequestHandler):
    def log_message(self, format_str, *args):
        pass

    def _send_no_cache_headers(self):
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._send_no_cache_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            if os.path.exists(HTML_FILE):
                with open(HTML_FILE, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(content)))
                self._send_no_cache_headers()
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b"dashboard.html not found in script folder.")
            return

        if self.path == "/api/config":
            cfg = load_config()
            safe_cfg = dict(cfg)
            safe_cfg["has_api_key"] = bool(cfg.get("radarr_api_key"))
            safe_cfg["radarr_api_key"] = ""
            safe_cfg["log_file_path"] = LOG_FILE
            safe_cfg["folder_status"] = get_folder_statuses(cfg)
            safe_cfg["server_instance_id"] = SERVER_INSTANCE_ID
            safe_cfg["server_pid"] = os.getpid()
            safe_cfg["capabilities"] = {
                "pymediainfo": HAS_MEDIAINFO,
                "send2trash": HAS_SEND2TRASH
            }
            self._send_json(safe_cfg)
            return

        if self.path == "/api/logs":
            with LOG_LOCK:
                logs_copy = list(ACTIVITY_LOG)
            self._send_json({"logs": logs_copy, "log_file_path": LOG_FILE})
            return

        if self.path == "/api/maintenance/status" or self.path.startswith("/api/maintenance/status?"):
            cfg = load_config()
            force_refresh = "refresh=1" in self.path
            if force_refresh:
                cfg, _ = sync_maintenance_snapshot_to_config(cfg, force_refresh_drives=True)
            self._send_json(get_maintenance_status_payload(cfg, force_refresh_drives=False))
            return

        if self.path == "/api/collections":
            cfg = load_config()
            self._send_json(fetch_collections_overview(cfg))
            return

        if self.path == "/api/quarantine":
            cfg = load_config()
            self._send_json(list_quarantined_items(cfg))
            return

        if self.path == "/api/job_status":
            with JOB_LOCK:
                job_copy = dict(JOB_STATE)
                history_copy = list(JOB_HISTORY[:50])
            with MAINTENANCE_LOCK:
                maint_running = bool(MAINTENANCE_STATE.get("running"))
                if maint_running and not job_copy.get("active"):
                    tot = int(MAINTENANCE_STATE.get("total_files") or 0)
                    proc = int(MAINTENANCE_STATE.get("processed_files") or 0)
                    job_copy.update({
                        "active": True,
                        "job_name": "HDD Maintenance",
                        "status": f"Defragmenting [{MAINTENANCE_STATE.get('current_drive') or '...'}] {MAINTENANCE_STATE.get('current_file') or ''}",
                        "overall_current": proc,
                        "overall_total": tot,
                        "overall_percent": int(round((proc / tot) * 100)) if tot > 0 else 0,
                        "current_file": MAINTENANCE_STATE.get("current_file") or "",
                        "file_percent": 0
                    })
            job_copy["history"] = history_copy
            job_copy["active_count"] = 1 if job_copy.get("active") else 0
            job_copy["server_instance_id"] = SERVER_INSTANCE_ID
            self._send_json(job_copy)
            return

        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        if self.path == "/api/config":
            try:
                incoming = self._read_json()
                current = load_config()
                clear_key = bool(incoming.pop("clear_api_key", False))
                if clear_key:
                    incoming["radarr_api_key"] = ""
                elif not str(incoming.get("radarr_api_key") or "").strip():
                    incoming["radarr_api_key"] = current.get("radarr_api_key", "")
                if "staging_folder" not in incoming:
                    incoming["staging_folder"] = current.get("staging_folder", "")
                if "cached_naming_format" not in incoming:
                    incoming["cached_naming_format"] = current.get("cached_naming_format", "")
                for maint_key in (
                    "maintenance_enabled",
                    "maintenance_schedule",
                    "maintenance_day_of_week",
                    "maintenance_day_of_month",
                    "maintenance_time",
                    "contig_path",
                    "maintenance_engine",
                    "maintenance_engine_path",
                    "maintenance_drive_media_types",
                    "maintenance_drive_overrides",
                    "maintenance_last_run",
                    "custom_collections",
                    "collection_location_ignores",
                    "ignored_missing_folders",
                    "ignored_folders"
                ):
                    if maint_key not in incoming:
                        incoming[maint_key] = current.get(maint_key, DEFAULT_CONFIG[maint_key])
                saved = save_config(incoming)
                removed, _ = trim_log_file(saved["log_retention_days"])
                key_status_msg = "API key cleared" if clear_key else "API key DPAPI-encrypted"
                log_event(
                    f"Settings saved to {CONFIG_FILE} ({key_status_msg} | Log retention: {saved['log_retention_days']}d, trimmed {removed} old line(s)).",
                    "INFO"
                )
                self._send_json({
                    "ok": True,
                    "has_api_key": bool(saved.get("radarr_api_key")),
                    "log_retention_days": saved["log_retention_days"],
                    "folder_status": get_folder_statuses(saved)
                })
            except Exception as exc:
                log_event(f"Failed to save settings: {exc}", "ERROR")
                self._send_json({"ok": False, "message": f"Save failed: {exc}"}, status=500)
            return

        if self.path == "/api/clear_api_key":
            try:
                saved = clear_saved_api_key()
                self._send_json({
                    "ok": True,
                    "has_api_key": False,
                    "message": "Saved Radarr API key cleared from config.json.",
                    "folder_status": get_folder_statuses(saved)
                })
            except Exception as exc:
                log_event(f"Failed to clear Radarr API key: {exc}", "ERROR")
                self._send_json({"ok": False, "message": f"Clear failed: {exc}"}, status=500)
            return

        if self.path == "/api/trim_logs":
            cfg = load_config()
            removed, kept = trim_log_file(cfg.get("log_retention_days", 30))
            msg = f"Manual log trim complete ({cfg.get('log_retention_days', 30)}d window): {removed} removed, {kept} kept."
            jid = begin_job("Log Trim", msg, total=1)
            finish_job(jid, msg, state="completed")
            log_event(msg, "INFO")
            self._send_json({"ok": True, "removed": removed, "kept": kept})
            return

        if self.path == "/api/test_radarr":
            cfg = load_config()
            try:
                _, info = radarr_api_request(cfg, "/api/v3/system/status", method="GET", timeout=6)
                ver = info.get("version", "unknown")
                naming = fetch_radarr_naming_config(cfg)
                log_event(f"Connected to Radarr v{ver}.", "INFO")
                self._send_json({
                    "ok": True,
                    "message": f"Connected to Radarr v{ver}",
                    "naming_format": naming.get("standard_movie_format", "")
                })
            except Exception as exc:
                self._send_json({"ok": False, "message": f"Connection failed: {exc}"})
            return

        if self.path == "/api/sync_root_folders":
            cfg = load_config()
            try:
                result = sync_root_folders_from_radarr(cfg)
                naming = fetch_radarr_naming_config(cfg)
                result["naming_format"] = naming.get("standard_movie_format", "")
                self._send_json(result)
            except Exception as exc:
                log_event(f"Root folder sync failed: {exc}", "ERROR")
                self._send_json({"ok": False, "message": f"Sync failed: {exc}"}, status=400)
            return

        if self.path == "/api/scan":
            cfg = load_config()
            report = scan_all_drives(cfg)
            self._send_json(report)
            return

        if self.path == "/api/execute":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = execute_duplicate_resolution(payload, cfg)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/intake/import":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = execute_intake_import(payload, cfg)
            self._send_json({"ok": ok, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/maintenance/config":
            try:
                incoming = self._read_json()
                current = load_config()
                for maint_key in (
                    "maintenance_enabled",
                    "maintenance_schedule",
                    "maintenance_day_of_week",
                    "maintenance_day_of_month",
                    "maintenance_time",
                    "contig_path"
                ):
                    if maint_key in incoming:
                        current[maint_key] = incoming[maint_key]
                saved, _ = sync_maintenance_snapshot_to_config(current, force_refresh_drives=True)
                status_payload = get_maintenance_status_payload(saved, force_refresh_drives=False)
                log_event(
                    f"Library Maintenance saved to {CONFIG_FILE} (Enabled: {saved['maintenance_enabled']} | "
                    f"Engine: {saved['maintenance_engine']} | Schedule: {saved['maintenance_schedule']} at {saved['maintenance_time']} | "
                    f"Drives: {saved['maintenance_drive_media_types']} | Next: {status_payload['next_run']}).",
                    "INFO"
                )
                self._send_json({"ok": True, "message": "Library Maintenance settings & drive types saved to config.json.", "maintenance": status_payload})
            except Exception as exc:
                log_event(f"Failed to save Library Maintenance settings: {exc}", "ERROR")
                self._send_json({"ok": False, "message": f"Save failed: {exc}"}, status=500)
            return

        if self.path == "/api/maintenance/run_now":
            cfg = load_config()
            ok, msg = start_maintenance_run(cfg, trigger_source="manual")
            self._send_json({
                "ok": ok,
                "message": msg,
                "maintenance": get_maintenance_status_payload(cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/maintenance/stop":
            cfg = load_config()
            ok, msg = stop_maintenance_run()
            self._send_json({
                "ok": ok,
                "message": msg,
                "maintenance": get_maintenance_status_payload(cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/maintenance/drive_type":
            try:
                payload = self._read_json()
                drive = str(payload.get("drive") or "").strip().upper()
                media_type = str(payload.get("media_type") or "").strip().upper()
                if not drive or media_type not in ("HDD", "SSD"):
                    self._send_json({"ok": False, "message": "Invalid drive or media type (must be HDD or SSD)."}, status=400)
                    return
                current = load_config()
                overrides = dict(current.get("maintenance_drive_overrides") or {})
                overrides[drive] = media_type
                current["maintenance_drive_overrides"] = overrides
                saved, _ = sync_maintenance_snapshot_to_config(current, force_refresh_drives=False)
                log_event(f"Library Maintenance: Drive {drive} manually set to {media_type} in config.json.", "INFO")
                self._send_json({
                    "ok": True,
                    "message": f"Drive {drive} set to {media_type} and saved to config.json.",
                    "maintenance": get_maintenance_status_payload(saved, force_refresh_drives=False)
                })
            except Exception as exc:
                log_event(f"Failed to update drive media type: {exc}", "ERROR")
                self._send_json({"ok": False, "message": f"Update failed: {exc}"}, status=500)
            return

        if self.path == "/api/browse_dir":
            payload = self._read_json()
            ok, listing, err = list_directory_for_browser(payload)
            resp = {"ok": ok, "message": err}
            resp.update(listing)
            self._send_json(resp, status=200 if ok else 400)
            return

        if self.path == "/api/browse_mkdir":
            payload = self._read_json()
            ok, new_path, msg = create_subfolder_for_browser(payload)
            self._send_json({"ok": ok, "path": new_path, "message": msg}, status=200 if ok else 400)
            return

        if self.path == "/api/browse":
            payload = self._read_json()
            mode = str(payload.get("mode") or "folder").strip().lower()
            if mode not in ("folder", "contig"):
                self._send_json({"ok": False, "message": "Invalid browse mode."}, status=400)
                return
            title = "Select Sysinternals contig64.exe" if mode == "contig" else "Select Folder"
            ok, path_val, err = open_native_windows_dialog(mode=mode, title=title)
            if ok:
                self._send_json({"ok": True, "path": path_val, "cancelled": not bool(path_val)})
            else:
                self._send_json({"ok": False, "path": "", "message": err}, status=500)
            return

        if self.path == "/api/collections/custom/save":
            cfg = load_config()
            payload = self._read_json()
            ok, msg, entry = save_custom_collection(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "collection": entry,
                "overview": fetch_collections_overview(load_config()) if ok else None
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/custom/remove":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = delete_custom_collection_only(str(payload.get("id") or "").strip(), cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "overview": fetch_collections_overview(load_config()) if ok else None
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/bulk_move":
            cfg = load_config()
            payload = self._read_json()
            ok, msg, detail = bulk_move_collection(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "detail": detail,
                "overview": fetch_collections_overview(cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/bulk_delete":
            cfg = load_config()
            payload = self._read_json()
            ok, msg, detail = bulk_delete_collection(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "detail": detail,
                "overview": fetch_collections_overview(load_config())
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/ignore_location":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = toggle_collection_location_ignore(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "overview": fetch_collections_overview(load_config()) if ok else None
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/set_root":
            cfg = load_config()
            payload = self._read_json()
            ok, msg, detail = set_collection_root_folder(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "detail": detail,
                "overview": fetch_collections_overview(load_config()) if ok else None
            }, status=200 if ok else 400)
            return

        if self.path in ("/api/collections/custom/create_root", "/api/collections/create_root"):
            cfg = load_config()
            payload = self._read_json()
            ok, msg, detail = create_custom_collection_root_folder(payload, cfg)
            updated_cfg = load_config()
            self._send_json({
                "ok": ok,
                "message": msg,
                "detail": detail,
                "overview": fetch_collections_overview(updated_cfg) if ok else None,
                "folder_status": get_folder_statuses(updated_cfg) if ok else None
            }, status=200 if ok else 400)
            return

        if self.path == "/api/collections/set_preset_parent":
            cfg = load_config()
            payload = self._read_json()
            raw_parent = str(payload.get("parent_folder") or "").strip()
            cfg["collections_root_parent"] = os.path.normpath(raw_parent) if raw_parent else ""
            saved = save_config(cfg)
            msg = (
                f"Saved preset Collections parent folder: {saved['collections_root_parent']}"
                if saved["collections_root_parent"]
                else "Cleared preset Collections parent folder (will prompt each time)."
            )
            log_event(f"Collections: {msg}", "INFO")
            self._send_json({
                "ok": True,
                "message": msg,
                "collections_root_parent": saved["collections_root_parent"]
            })
            return

        if self.path == "/api/quarantine/purge":
            cfg = load_config()
            payload = self._read_json()
            ok, msg, detail = purge_quarantined_items(payload, cfg)
            self._send_json({
                "ok": ok,
                "message": msg,
                "detail": detail,
                "quarantine": list_quarantined_items(cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/folder/resolve_missing":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = resolve_missing_folder(payload, cfg)
            if ok:
                jid = begin_job("Folder Resolve", msg, total=1)
                finish_job(jid, msg, state="completed")
            updated_cfg = load_config()
            self._send_json({
                "ok": ok,
                "message": msg,
                "folder_status": get_folder_statuses(updated_cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/folder/toggle_ignore":
            cfg = load_config()
            payload = self._read_json()
            ok, msg = toggle_folder_ignored(payload, cfg)
            updated_cfg = load_config()
            self._send_json({
                "ok": ok,
                "message": msg,
                "folder_status": get_folder_statuses(updated_cfg)
            }, status=200 if ok else 400)
            return

        if self.path == "/api/jobs/clear":
            with JOB_LOCK:
                JOB_HISTORY[:] = [j for j in JOB_HISTORY if j.get("state") == "running"]
            self._send_json({"ok": True, "message": "Cleared completed job history."})
            return

        if self.path == "/api/shutdown":
            payload = self._read_json()
            force_close = bool(payload.get("force", False))
            with JOB_LOCK:
                snap = dict(JOB_STATE)
                hist_copy = list(JOB_HISTORY)
            maint_running = bool(MAINTENANCE_STATE.get("running"))
            has_running_job = bool(snap.get("active")) or any(
                j.get("state") == "running" for j in hist_copy
            ) or maint_running

            if has_running_job and not force_close:
                active_name = snap.get("job_name") or ("Library Maintenance (HDD Defrag)" if maint_running else "Active Job")
                active_status = snap.get("status") or ("Running scheduled HDD defrag..." if maint_running else "Job in progress...")
                active_pct = int(snap.get("overall_percent") or 0)
                log_event(
                    f"Shutdown guarded: refused graceful shutdown while '{active_name}' ({active_pct}%) is running.",
                    "WARN"
                )
                self._send_json({
                    "ok": False,
                    "busy": True,
                    "pid": os.getpid(),
                    "job_name": active_name,
                    "status": active_status,
                    "overall_percent": active_pct,
                    "message": f"Shutdown refused: '{active_name}' ({active_pct}%) is currently running."
                }, status=409)
                return

            mode_note = "FORCE shutdown" if force_close else "Graceful shutdown"
            log_event(f"{mode_note} requested by a newer Radarr Companion session.", "WARN")
            self._send_json({"ok": True, "pid": os.getpid(), "message": f"{mode_note} accepted."})
            if ACTIVE_HTTP_SERVER is not None:
                threading.Thread(target=ACTIVE_HTTP_SERVER.shutdown, daemon=True).start()
            return

        self.send_response(404)
        self.end_headers()


def list_windows_drives_for_browser():
    """Returns a sorted list of accessible mounted drives on Windows with free/total GB."""
    drives = []
    if os.name == "nt":
        try:
            bitmask = ctypes.windll.kernel32.GetLogicalDrives()
            letters = [chr(65 + i) for i in range(26) if bitmask & (1 << i)]
        except Exception:
            letters = [chr(c) for c in range(ord("A"), ord("Z") + 1)]
        for letter in letters:
            root = f"{letter}:\\"
            if os.path.isdir(root):
                free_gb = total_gb = None
                try:
                    usage = shutil.disk_usage(root)
                    free_gb = round(usage.free / (1024 ** 3), 1)
                    total_gb = round(usage.total / (1024 ** 3), 1)
                except OSError:
                    pass
                drives.append({
                    "name": root,
                    "path": root,
                    "free_gb": free_gb,
                    "total_gb": total_gb
                })
    else:
        drives.append({"name": "/", "path": "/", "free_gb": None, "total_gb": None})
    return drives


def list_directory_for_browser(payload):
    """
    Lists drives, subfolders, and (in file/contig mode) matching files for the in-app
    Radarr-style folder/file browser so the UI never depends on background Windows dialogs.
    """
    raw_path = str(payload.get("path") or "").strip()
    mode = str(payload.get("mode") or "folder").strip().lower()
    file_ext = str(payload.get("file_ext") or (".exe" if mode == "contig" else "")).strip().lower()
    drives = list_windows_drives_for_browser()

    if not raw_path or raw_path == "__DRIVES__":
        return True, {
            "current_path": "",
            "parent_path": "",
            "is_drives_view": True,
            "drives": drives,
            "folders": [],
            "files": []
        }, ""

    norm_path = os.path.normpath(raw_path)
    if re.match(r"^[A-Za-z]:$", norm_path):
        norm_path += "\\"
    if os.path.isfile(win_long_path(norm_path)):
        norm_path = os.path.dirname(norm_path)
    if not os.path.isdir(win_long_path(norm_path)):
        return False, {
            "current_path": "",
            "parent_path": "",
            "is_drives_view": True,
            "drives": drives,
            "folders": [],
            "files": []
        }, f"Folder not found or drive offline: {norm_path}"

    # Determine parent path (if at drive root like D:\, parent_path is "" -> Drives list)
    parent = os.path.dirname(norm_path.rstrip("\\/"))
    if not parent or os.path.normcase(parent) == os.path.normcase(norm_path.rstrip("\\/")):
        parent_path = ""
    else:
        if re.match(r"^[A-Za-z]:$", parent):
            parent += "\\"
        parent_path = parent

    skip_names = {"$recycle.bin", "system volume information", "recovery", "perflogs"}
    folders = []
    files = []
    try:
        with os.scandir(win_long_path(norm_path)) as it:
            for entry in it:
                name_low = entry.name.lower()
                if name_low in skip_names:
                    continue
                full_entry = os.path.join(norm_path, entry.name)
                try:
                    if entry.is_dir(follow_symlinks=False):
                        folders.append({"name": entry.name, "path": full_entry})
                    elif mode in ("file", "contig") and entry.is_file(follow_symlinks=False):
                        if not file_ext or name_low.endswith(file_ext):
                            files.append({"name": entry.name, "path": full_entry})
                except OSError:
                    continue
    except OSError as exc:
        return False, {
            "current_path": norm_path,
            "parent_path": parent_path,
            "is_drives_view": False,
            "drives": drives,
            "folders": [],
            "files": []
        }, f"Cannot read folder ({norm_path}): {exc}"

    folders.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())
    return True, {
        "current_path": norm_path,
        "parent_path": parent_path,
        "is_drives_view": False,
        "drives": drives,
        "folders": folders,
        "files": files
    }, ""


def create_subfolder_for_browser(payload):
    """Creates a new subfolder inside current_path from the in-app folder browser."""
    parent_path = str(payload.get("parent_path") or "").strip()
    folder_name = sanitise_windows_filename(str(payload.get("folder_name") or "").strip())
    if not parent_path or not folder_name:
        return False, "", "Both parent path and new folder name are required."
    if re.match(r"^[A-Za-z]:$", parent_path):
        parent_path += "\\"
    if not os.path.isdir(win_long_path(parent_path)):
        return False, "", f"Parent folder does not exist or drive is offline: {parent_path}"
    target = os.path.normpath(os.path.join(parent_path, folder_name))
    try:
        os.makedirs(win_long_path(target), exist_ok=True)
        log_event(f"Created folder via in-app browser: {target}", "INFO")
        return True, target, f"Created folder: {target}"
    except OSError as exc:
        return False, "", f"Could not create folder: {exc}"


def clear_saved_api_key():
    """Clears the saved Radarr API key from config.json and returns the updated config."""
    current = load_config()
    current["radarr_api_key"] = ""
    saved = save_config(current)
    log_event(f"Cleared saved Radarr API key from {CONFIG_FILE}.", "INFO")
    return saved


def is_port_in_use(host, port):
    """Checks whether a TCP listener is already active or bound on host:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.8)
        if sock.connect_ex((host, port)) == 0:
            return True
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as test_bind:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                test_bind.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            test_bind.bind((host, port))
            return False
    except OSError:
        return True


def is_existing_companion_instance(base_url):
    """Probes an active listener on base_url to verify if it is a running Radarr Companion instance."""
    try:
        req = urllib.request.Request(f"{base_url}/api/config", method="GET")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.getcode() == 200:
                data = json.loads(resp.read().decode("utf-8"))
                return isinstance(data, dict) and "library_folders" in data and "quarantine_mode" in data
    except Exception:
        pass
    return False


def find_existing_companion_pids():
    """
    Finds PIDs of any other running Radarr Companion processes (RadarrCompanion.exe or
    radarr_companion.py), strictly excluding this process and its parent bootloader PID.
    """
    if os.name != "nt":
        return []
    my_pid = os.getpid()
    parent_pid = os.getppid()
    ps_cmd = (
        "$ErrorActionPreference='SilentlyContinue'; "
        "Get-CimInstance Win32_Process | Where-Object { "
        "  ($_.Name -ieq 'RadarrCompanion.exe') -or "
        "  ($_.Name -imatch '^python(w)?(\\.exe)?$' -and $_.CommandLine -imatch 'radarr_companion\\.py') "
        "} | Select-Object -ExpandProperty ProcessId"
    )
    pids = []
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
        )
        for line in (proc.stdout or "").splitlines():
            s = line.strip()
            if s.isdigit():
                pid = int(s)
                if pid not in (my_pid, parent_pid):
                    pids.append(pid)
    except Exception as exc:
        log_event(f"Could not enumerate existing Radarr Companion processes: {exc}", "WARN")
    return sorted(set(pids))


def probe_existing_instance_active_job(base_url):
    """
    Queries an existing Radarr Companion instance's /api/job_status to determine if any
    file move, import, scan, or maintenance job is currently running.
    Returns a dict with job details if busy, or None if idle/unreachable.
    """
    try:
        req = urllib.request.Request(f"{base_url}/api/job_status", method="GET")
        with urllib.request.urlopen(req, timeout=2.0) as resp:
            if resp.getcode() == 200:
                data = json.loads(resp.read().decode("utf-8") or "{}")
                running_hist = [j for j in (data.get("history") or []) if j.get("state") == "running"]
                if data.get("active") or running_hist:
                    return {
                        "job_name": data.get("job_name") or (running_hist[0].get("name") if running_hist else "Active Job"),
                        "status": data.get("status") or (running_hist[0].get("status") if running_hist else "Operation in progress"),
                        "overall_percent": int(data.get("overall_percent") or 0),
                        "current_file": data.get("current_file") or ""
                    }
    except Exception:
        pass
    return None


def prompt_running_jobs_action(job_info):
    """
    Warns the user that an existing Radarr Companion instance has active running job(s)
    and asks whether to 'wait' (recommended), 'force' close, or 'cancel' (keep existing).
    Uses a native Windows MessageBox when no interactive console TTY is attached.
    """
    job_name = job_info.get("job_name") or "Active Job"
    job_status = job_info.get("status") or "Operation in progress"
    pct = job_info.get("overall_percent", 0)
    cur_file = job_info.get("current_file") or ""

    detail_lines = [
        "An existing Radarr Companion instance is currently running a job:",
        f"  Job: {job_name} ({pct}%)",
        f"  Status: {job_status}",
    ]
    if cur_file:
        detail_lines.append(f"  Current File: {cur_file}")
    detail_lines.append("")
    detail_lines.append("Force-closing while a file move, import, or defrag is running can cause partial copies or file corruption.")

    # Use interactive console prompt if attached to a terminal
    if sys.stdin is not None and sys.stdin.isatty():
        print("\n" + "=" * 72)
        print("[!] ACTIVE JOB GUARD — EXISTING INSTANCE IS BUSY")
        for line in detail_lines:
            print(line)
        print("=" * 72)
        while True:
            try:
                ans = input("Choose action — [W]ait for completion (Recommended) / [F]orce close / [C]ancel: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return "cancel"
            if ans in ("", "w", "wait", "y", "yes"):
                return "wait"
            if ans in ("f", "force", "kill"):
                return "force"
            if ans in ("c", "cancel", "q", "quit", "n", "no"):
                return "cancel"
            print("Please enter W (Wait), F (Force close), or C (Cancel).")

    # Native Windows MessageBox (Yes = Wait, No = Force Close, Cancel = Keep Existing)
    if os.name == "nt":
        try:
            MB_YESNOCANCEL = 0x00000003
            MB_ICONWARNING = 0x00000030
            MB_DEFBUTTON1 = 0x00000000
            MB_TOPMOST = 0x00040000
            IDYES = 6
            IDNO = 7
            msg_text = (
                "\n".join(detail_lines)
                + "\n\nClick YES to Wait safely until the job finishes (Recommended)."
                + "\nClick NO to Force Close the running instance anyway."
                + "\nClick CANCEL to keep the existing instance running."
            )
            res = ctypes.windll.user32.MessageBoxW(
                None,
                msg_text,
                "Radarr Companion — Running Job Protection",
                MB_YESNOCANCEL | MB_ICONWARNING | MB_DEFBUTTON1 | MB_TOPMOST
            )
            if res == IDYES:
                return "wait"
            if res == IDNO:
                return "force"
            return "cancel"
        except Exception:
            pass

    return "cancel"


def wait_for_existing_jobs_to_finish(base_url, poll_interval=2.0):
    """Polls an existing instance until its running jobs finish cleanly."""
    log_event("Waiting for existing Radarr Companion instance to finish active job(s)...", "INFO")
    print("[*] Waiting for existing Radarr Companion job(s) to finish safely (press Ctrl+C to cancel)...")
    last_printed = ""
    try:
        while True:
            if not is_port_in_use(HTTP_HOST, HTTP_PORT):
                return True
            active = probe_existing_instance_active_job(base_url)
            if not active:
                print("[*] Existing job(s) finished cleanly. Switching to new session...")
                log_event("Existing Radarr Companion job(s) completed; proceeding with graceful handover.", "INFO")
                return True
            status_line = f"    Waiting: {active['job_name']} ({active['overall_percent']}%) — {active['status']}"
            if status_line != last_printed:
                print(status_line)
                last_printed = status_line
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        log_event("User cancelled waiting for existing Radarr Companion job(s).", "INFO")
        return False


def terminate_old_companion_sessions(dashboard_url, prompt_fn=None):
    """
    Checks for an existing Radarr Companion session (by port probe and Windows process list).
    If the existing session has active running jobs, guards against corruption by prompting
    the user to Wait (recommended), Force Close, or Cancel (keep existing).
    Returns (terminated_count, aborted_by_user).
    """
    terminated = 0
    old_pids = find_existing_companion_pids()
    port_busy = is_port_in_use(HTTP_HOST, HTTP_PORT)
    is_companion_on_port = is_existing_companion_instance(dashboard_url) if port_busy else False
    force_shutdown = False

    if is_companion_on_port:
        active_job = probe_existing_instance_active_job(dashboard_url)
        if active_job:
            chooser = prompt_fn or prompt_running_jobs_action
            choice = str(chooser(active_job) or "cancel").strip().lower()
            if choice == "wait":
                if not wait_for_existing_jobs_to_finish(dashboard_url):
                    return 0, True
            elif choice == "force":
                force_shutdown = True
                log_event(
                    f"User chose FORCE CLOSE while '{active_job['job_name']}' ({active_job['overall_percent']}%) was running.",
                    "WARN"
                )
            else:
                log_event(
                    f"User chose to keep existing Radarr Companion session running ('{active_job['job_name']}' active).",
                    "INFO"
                )
                return 0, True

    if is_companion_on_port or old_pids:
        warn_msg = (
            f"WARNING: Detected existing Radarr Companion session(s) "
            f"(PIDs: {old_pids if old_pids else 'port ' + str(HTTP_PORT)}). Terminating old session before starting new instance..."
        )
        log_event(warn_msg, "WARN")
        print(f"[!] {warn_msg}")

    # 1. Try graceful HTTP shutdown first if a companion server is responding on our port
    if is_companion_on_port:
        try:
            payload_bytes = json.dumps({"force": force_shutdown}).encode("utf-8")
            req = urllib.request.Request(
                f"{dashboard_url}/api/shutdown",
                data=payload_bytes,
                headers={"Content-Type": "application/json"},
                method="POST"
            )
            with urllib.request.urlopen(req, timeout=2.5) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
                if body.get("pid") and body["pid"] not in old_pids and body["pid"] != os.getpid():
                    old_pids.append(int(body["pid"]))
            terminated += 1
        except urllib.error.HTTPError as exc:
            if exc.code == 409 and not force_shutdown:
                # A job started in the tiny race window between probe and shutdown — re-guard safely
                try:
                    busy_info = json.loads(exc.read().decode("utf-8") or "{}")
                except Exception:
                    busy_info = {"job_name": "Active Job", "status": "Operation in progress", "overall_percent": 0}
                chooser = prompt_fn or prompt_running_jobs_action
                choice = str(chooser(busy_info) or "cancel").strip().lower()
                if choice == "wait":
                    if not wait_for_existing_jobs_to_finish(dashboard_url):
                        return 0, True
                    return terminate_old_companion_sessions(dashboard_url, prompt_fn=prompt_fn)
                if choice == "force":
                    force_shutdown = True
                    try:
                        req_f = urllib.request.Request(
                            f"{dashboard_url}/api/shutdown",
                            data=b'{"force": true}',
                            headers={"Content-Type": "application/json"},
                            method="POST"
                        )
                        with urllib.request.urlopen(req_f, timeout=2.5):
                            terminated += 1
                    except Exception:
                        pass
                else:
                    return 0, True
        except Exception:
            pass
        # Wait briefly for graceful port release
        for _ in range(15):
            if not is_port_in_use(HTTP_HOST, HTTP_PORT):
                break
            time.sleep(0.2)

    # 2. Force-kill any remaining old companion PIDs
    remaining_pids = find_existing_companion_pids()
    for pid in sorted(set(old_pids) | set(remaining_pids)):
        if pid in (os.getpid(), os.getppid()):
            continue
        try:
            res = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            if res.returncode == 0:
                terminated += 1
                log_event(f"Terminated old Radarr Companion process (PID {pid}).", "WARN")
        except Exception as exc:
            log_event(f"Failed to terminate old PID {pid}: {exc}", "WARN")

    # Wait up to 3s for port 8085 to clear after taskkill
    if terminated > 0:
        for _ in range(15):
            if not is_port_in_use(HTTP_HOST, HTTP_PORT):
                break
            time.sleep(0.2)
    return terminated, False


if __name__ == "__main__":
    if not os.path.exists(CONFIG_FILE):
        startup_cfg = save_config(DEFAULT_CONFIG)
    else:
        startup_cfg = load_config()

    removed_old, _ = trim_log_file(startup_cfg.get("log_retention_days", 30))
    dashboard_url = f"http://{HTTP_HOST}:{HTTP_PORT}"

    # Detect, guard active jobs (Wait / Force Close / Cancel), and replace idle old sessions before binding port 8085
    replaced_count, aborted_by_user = terminate_old_companion_sessions(dashboard_url)
    if aborted_by_user:
        print(f"[*] Keeping existing Radarr Companion session running at {dashboard_url}.")
        if "--no-browser" not in sys.argv:
            open_dashboard_browser(dashboard_url)
        sys.exit(0)

    if is_port_in_use(HTTP_HOST, HTTP_PORT):
        log_event(
            f"Startup aborted: Port {HTTP_PORT} on {HTTP_HOST} is currently occupied by another application.",
            "ERROR"
        )
        print(f"[!] Cannot start server: {HTTP_HOST}:{HTTP_PORT} is already in use by another application.")
        sys.exit(1)

    try:
        server = ThreadingHTTPServer((HTTP_HOST, HTTP_PORT), CompanionHandler)
        ACTIVE_HTTP_SERVER = server
    except OSError as exc:
        log_event(f"Startup failed: Could not bind to {HTTP_HOST}:{HTTP_PORT} ({exc}).", "ERROR")
        print(f"[!] Could not bind to {HTTP_HOST}:{HTTP_PORT}: {exc}")
        sys.exit(1)

    log_event(
        f"Radarr Companion started at {dashboard_url} (PID {os.getpid()}"
        + (f" | Replaced {replaced_count} previous session(s)" if replaced_count else "")
        + ")",
        "INFO"
    )
    log_event(
        f"Config loaded from {CONFIG_FILE} | Library folders: {len(startup_cfg['library_folders'])} | "
        f"Download folders: {len(startup_cfg['download_folders'])} | "
        f"Radarr Key: {'Loaded' if startup_cfg['radarr_api_key'] else 'Not Set'}",
        "INFO"
    )
    log_event(
        f"DPAPI Encryption: Active | Log File: {LOG_FILE} ({startup_cfg.get('log_retention_days', 30)}d retention, {removed_old} expired line(s) trimmed)",
        "INFO"
    )
    log_event(
        f"MediaInfo: {'Active' if HAS_MEDIAINFO else 'Fallback'} | Recycle Bin: {'Active' if HAS_SEND2TRASH else 'Drive Quarantine'}",
        "INFO"
    )

    scheduler_thread = threading.Thread(target=maintenance_scheduler_loop, daemon=True)
    scheduler_thread.start()

    if "--no-browser" not in sys.argv:
        fresh_url = f"{dashboard_url}/?_t={int(time.time())}"
        browser_timer = threading.Timer(0.6, open_dashboard_browser, args=(fresh_url,))
        browser_timer.daemon = True
        browser_timer.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log_event("Shutting down Radarr Companion.", "INFO")
    finally:
        server.server_close()