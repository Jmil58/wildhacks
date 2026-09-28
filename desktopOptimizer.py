import tkinter as tk
from tkinter import ttk, messagebox
import psutil
import platform
import socket
import os
import sys
import stat as stat_module
import glob
import json
import tempfile
import threading
import time
import shutil
import uuid
import queue
from collections import deque
from datetime import datetime

try:
    import winreg
except ImportError:
    winreg = None


# =====================================================================
# Colors / theme constants
# =====================================================================

COLOR_BG = "#111827"
COLOR_SIDEBAR = "#0f172a"
COLOR_CARD = "#1e293b"
COLOR_BORDER = "#334155"
COLOR_TEXT = "white"
COLOR_MUTED = "#94a3b8"
COLOR_ACCENT = "#2563eb"
COLOR_ACCENT_HOVER = "#1d4ed8"
COLOR_GREEN = "#16a34a"
COLOR_GREEN_HOVER = "#15803d"
COLOR_SUCCESS = "#22c55e"
COLOR_WARNING = "#f59e0b"
COLOR_DANGER = "#ef4444"
COLOR_DANGER_HOVER = "#dc2626"
COLOR_NAV_HOVER = "#1e293b"
COLOR_GRAPH_CPU = "#38bdf8"
COLOR_GRAPH_MEM = "#a855f7"


# =====================================================================
# Config / History persistence
# =====================================================================

CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".pc_optimizer")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.json")
RECOVERY_DIR = os.path.join(CONFIG_DIR, "recovery")
RECOVERY_INDEX_FILE = os.path.join(CONFIG_DIR, "recovery.json")
RECOVERY_LOCK = threading.RLock()
HISTORY_LOCK = threading.RLock()

# Paths that have been moved into Recovery but whose index record hasn't been
# written yet. reconcile_recovery_index() ignores these so it never creates
# "Unknown" duplicates while a cleanup is still running.
_PENDING_RECOVERY_PATHS = set()

DEFAULT_CONFIG = {
    "min_age_minutes": 60,
    "refresh_interval_sec": 5,
    "large_file_min_mb": 100,
}


def ensure_config_dir():
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        pass


def atomic_write_json(path, data, indent=2):
    """Write to a temp file then swap it in, so a crash can't leave a
    half-written (corrupt) JSON file behind."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
    os.replace(tmp, path)


def is_inside(path, parent):
    try:
        path = os.path.normcase(os.path.abspath(path))
        parent = os.path.normcase(os.path.abspath(parent))
        return path == parent or path.startswith(parent + os.sep)
    except (ValueError, OSError):
        return False


def load_config():
    ensure_config_dir()
    config = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                config.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config):
    ensure_config_dir()
    try:
        atomic_write_json(CONFIG_FILE, config)
        return True
    except OSError:
        return False


def load_history():
    ensure_config_dir()
    if os.path.isfile(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_history(history):
    ensure_config_dir()
    try:
        with HISTORY_LOCK:
            atomic_write_json(HISTORY_FILE, history)
        return True
    except OSError:
        return False


def append_history_record(record, max_records=200):
    with HISTORY_LOCK:
        history = load_history()
        history.insert(0, record)
        save_history(history[:max_records])


# =====================================================================
# Recovery system
# =====================================================================

def ensure_recovery_dir():
    ensure_config_dir()
    try:
        os.makedirs(RECOVERY_DIR, exist_ok=True)
        return True
    except OSError:
        return False


def load_recovery_index():
    ensure_config_dir()
    if not os.path.isfile(RECOVERY_INDEX_FILE):
        return []
    try:
        with open(RECOVERY_INDEX_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        # Keep the damaged file for inspection instead of silently
        # overwriting it with an empty list on the next save.
        try:
            os.replace(RECOVERY_INDEX_FILE, RECOVERY_INDEX_FILE + ".corrupt")
        except OSError:
            pass
        return []
    except OSError:
        return []


def save_recovery_index(items):
    ensure_config_dir()
    try:
        with RECOVERY_LOCK:
            atomic_write_json(RECOVERY_INDEX_FILE, items, indent=None)
        return True
    except OSError:
        return False


def has_valid_original(record):
    """A record can only be restored if we know a real, absolute original path."""
    p = record.get("original_path")
    return bool(p) and isinstance(p, str) and os.path.isabs(p)


def _move_to_recovery_raw(path):
    """Move ONE file into Recovery without touching the index.
    Returns (ok, record, error). The caller MUST later call
    add_recovery_records([...]) (or rollback_moves) for every ok record."""
    if not os.path.isfile(path):
        return False, None, "File does not exist."
    if not ensure_recovery_dir():
        return False, None, "Could not create the Recovery folder."

    original_path = os.path.abspath(path)
    recovery_id = str(uuid.uuid4())
    recovery_path = os.path.join(RECOVERY_DIR, recovery_id + "_" + os.path.basename(original_path))
    abs_recovery = os.path.abspath(recovery_path)

    with RECOVERY_LOCK:
        _PENDING_RECOVERY_PATHS.add(abs_recovery)
    try:
        size = os.path.getsize(original_path)
        shutil.move(original_path, recovery_path)
    except PermissionError as e:
        with RECOVERY_LOCK:
            _PENDING_RECOVERY_PATHS.discard(abs_recovery)
        return False, None, f"Permission denied (or file in use): {e}"
    except OSError as e:
        with RECOVERY_LOCK:
            _PENDING_RECOVERY_PATHS.discard(abs_recovery)
        return False, None, str(e)

    record = {
        "id": recovery_id, "name": os.path.basename(original_path),
        "original_path": original_path, "recovery_path": recovery_path,
        "size": size, "moved_at": datetime.now().isoformat(timespec="seconds"),
    }
    return True, record, None


def add_recovery_records(records):
    """Write many records to the index in ONE load/save (under the lock)."""
    if not records:
        return True
    with RECOVERY_LOCK:
        items = load_recovery_index()
        items = list(reversed(records)) + items
        ok = save_recovery_index(items)
        if ok:
            for r in records:
                _PENDING_RECOVERY_PATHS.discard(os.path.abspath(r["recovery_path"]))
        return ok


def rollback_moves(records):
    """Put files back where they came from if the index couldn't be saved."""
    for r in records:
        try:
            parent = os.path.dirname(r["original_path"])
            if parent:
                os.makedirs(parent, exist_ok=True)
            shutil.move(r["recovery_path"], r["original_path"])
        except OSError:
            pass
        with RECOVERY_LOCK:
            _PENDING_RECOVERY_PATHS.discard(os.path.abspath(r["recovery_path"]))


def move_file_to_recovery(path):
    ok, record, error = _move_to_recovery_raw(path)
    if not ok:
        return False, None, error
    if not add_recovery_records([record]):
        rollback_moves([record])
        return False, None, "Could not save the Recovery index."
    return True, record, None


def reconcile_recovery_index(items):
    """Make sure every physical file in Recovery is represented in the index.
    Caller should hold RECOVERY_LOCK."""
    try:
        os.makedirs(RECOVERY_DIR, exist_ok=True)
        known_paths = {os.path.abspath(x.get("recovery_path")) for x in items if x.get("recovery_path")}
        changed = False
        with os.scandir(RECOVERY_DIR) as it:
            for entry in it:
                if not entry.is_file():
                    continue
                path = os.path.abspath(entry.path)
                if path in known_paths or path in _PENDING_RECOVERY_PATHS:
                    continue
                try:
                    st = entry.stat()
                    size = st.st_size
                    moved_at = datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds")
                except OSError:
                    continue
                filename = entry.name
                display_name = filename.split("_", 1)[1] if "_" in filename else filename
                items.insert(0, {
                    "id": str(uuid.uuid4()),
                    "name": display_name,
                    "original_path": "Unknown (Recovery record was missing)",
                    "recovery_path": path,
                    "size": size,
                    "moved_at": moved_at,
                })
                changed = True
        if changed:
            save_recovery_index(items)
    except OSError:
        pass
    return items


def load_reconciled_recovery():
    """Load + reconcile + prune, all under the lock so it can't race a cleanup."""
    with RECOVERY_LOCK:
        items = reconcile_recovery_index(load_recovery_index())
        valid = [x for x in items
                 if x.get("recovery_path") and os.path.isfile(x.get("recovery_path"))
                 or os.path.abspath(x.get("recovery_path", "")) in _PENDING_RECOVERY_PATHS]
        if len(valid) != len(items):
            save_recovery_index(valid)
        return valid


def remove_recovery_records(record_ids):
    ids = set(record_ids)
    if not ids:
        return True
    with RECOVERY_LOCK:
        items = [x for x in load_recovery_index() if x.get("id") not in ids]
        return save_recovery_index(items)


def remove_recovery_record(record_id):
    return remove_recovery_records([record_id])


def restore_recovery_item(record, remove_record=True):
    src = record.get("recovery_path")
    dst = record.get("original_path")
    if not src or not os.path.isfile(src):
        return False, "The recovered file could not be found."
    if not has_valid_original(record):
        return False, "The original location for this file is unknown, so it can't be restored."
    if os.path.exists(dst):
        return False, "A file already exists at the original location:\n\n" + dst
    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
        if remove_record:
            remove_recovery_record(record.get("id"))
        return True, None
    except OSError as e:
        return False, str(e)


def restore_recovery_items(records):
    """Restore many files, updating the index once at the end."""
    restored = 0
    failed = 0
    done_ids = []
    for r in records:
        ok, _ = restore_recovery_item(r, remove_record=False)
        if ok:
            restored += 1
            done_ids.append(r.get("id"))
        else:
            failed += 1
    remove_recovery_records(done_ids)
    return restored, failed


def permanently_delete_recovery_item(record, remove_record=True):
    path = record.get("recovery_path")
    try:
        if path and os.path.isfile(path):
            os.remove(path)
        if remove_record:
            remove_recovery_record(record.get("id"))
        return True, None
    except OSError as e:
        return False, str(e)


def permanently_delete_recovery_items(records):
    deleted = 0
    failed = 0
    done_ids = []
    for r in records:
        ok, _ = permanently_delete_recovery_item(r, remove_record=False)
        if ok:
            deleted += 1
            done_ids.append(r.get("id"))
        else:
            failed += 1
    remove_recovery_records(done_ids)
    return deleted, failed


# =====================================================================
# System metrics
# =====================================================================

def get_root_path():
    return os.path.abspath(os.sep)


def get_root_label():
    root = get_root_path()
    stripped = root.rstrip("\\/")
    return stripped or "/"


def get_temp_dir():
    return tempfile.gettempdir()


def get_cpu_usage():
    return psutil.cpu_percent(interval=None)


def get_memory_usage():
    return psutil.virtual_memory().percent


def get_disk_usage():
    return psutil.disk_usage(get_root_path()).percent


def get_folder_size(folder):
    total_size = 0
    if not folder or not os.path.isdir(folder):
        return 0
    for root, dirs, files in os.walk(folder):
        for file in files:
            try:
                path = os.path.join(root, file)
                total_size += os.path.getsize(path)
            except (PermissionError, FileNotFoundError, OSError):
                pass
    return total_size


def get_temp_size():
    return get_folder_size(get_temp_dir())


def format_bytes(size):
    if size < 1024:
        return f"{size} B"
    if size < 1024 ** 2:
        return f"{size / 1024:.1f} KB"
    if size < 1024 ** 3:
        return f"{size / (1024 ** 2):.1f} MB"
    if size < 1024 ** 4:
        return f"{size / (1024 ** 3):.1f} GB"
    return f"{size / (1024 ** 4):.1f} TB"


def get_health_score(cpu, memory, disk):
    score = 100
    if cpu > 80:
        score -= 20
    elif cpu > 60:
        score -= 10
    if memory > 85:
        score -= 20
    elif memory > 70:
        score -= 10
    if disk > 90:
        score -= 25
    elif disk > 80:
        score -= 10
    return max(score, 0)


def get_recommendations(cpu, memory, disk):
    recommendations = []
    if cpu > 80:
        recommendations.append("CPU usage is currently high.")
    if memory > 80:
        recommendations.append("Memory usage is high.")
    if disk > 85:
        recommendations.append("Your system drive is getting full.")
    if not recommendations:
        recommendations.append("No major optimization issues detected.")
    return recommendations


# =====================================================================
# Protected paths (never offered for deletion in Large File Finder)
# =====================================================================

def get_protected_roots():
    roots = []
    if platform.system() == "Windows":
        for var in ("SystemRoot", "windir", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
            v = os.environ.get(var)
            if v:
                roots.append(v)
    else:
        roots = ["/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/boot",
                 "/System", "/Library", "/Applications", "/var/lib"]
    return roots


def is_protected_path(path):
    return any(is_inside(path, r) for r in get_protected_roots())


# =====================================================================
# Drives
# =====================================================================

def get_all_drives():
    """Usage info for every mounted/mapped drive on the system."""
    drives = []
    try:
        partitions = psutil.disk_partitions(all=False)
    except OSError:
        return drives

    for part in partitions:
        if platform.system() != "Windows" and part.fstype in (
            "proc", "sysfs", "devtmpfs", "tmpfs", "devpts",
            "cgroup", "cgroup2", "overlay", "squashfs", "autofs"
        ):
            continue

        try:
            usage = psutil.disk_usage(part.mountpoint)
        except (PermissionError, FileNotFoundError, OSError):
            drives.append({
                "device": part.device, "mountpoint": part.mountpoint,
                "fstype": part.fstype, "total": 0, "used": 0, "free": 0,
                "percent": None, "error": True,
            })
            continue

        drives.append({
            "device": part.device, "mountpoint": part.mountpoint,
            "fstype": part.fstype, "total": usage.total, "used": usage.used,
            "free": usage.free, "percent": usage.percent, "error": False,
        })

    return drives


def get_junk_folders_for_drive(mountpoint):
    """Only well-known junk locations. Generic root folders named Temp/tmp are
    deliberately NOT included, since on secondary drives those are often
    user folders."""
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.join(mountpoint, "Windows", "Temp"),
            os.path.join(mountpoint, "$RECYCLE.BIN"),
        ]
    else:
        candidates = [
            os.path.join(mountpoint, ".Trash-1000"),
            os.path.join(mountpoint, ".Trashes"),
        ]
    return [c for c in candidates if os.path.isdir(c)]


def get_drive_junk_paths_all():
    paths = []
    system_temp = os.path.normcase(os.path.abspath(get_temp_dir()))
    for d in get_all_drives():
        if d.get("error"):
            continue
        for p in get_junk_folders_for_drive(d["mountpoint"]):
            if os.path.basename(p).upper() in ("$RECYCLE.BIN", "RECYCLER"):
                continue  # handled by the Recycle Bin category
            if os.path.normcase(os.path.abspath(p)) == system_temp:
                continue  # already covered by System Temp Files
            paths.append(p)
    return paths


def get_recycle_bin_paths():
    system = platform.system()
    paths = []
    if system == "Windows":
        for d in get_all_drives():
            if d.get("error"):
                continue
            p = os.path.join(d["mountpoint"], "$RECYCLE.BIN")
            if os.path.isdir(p):
                paths.append(p)
    elif system == "Darwin":
        p = os.path.join(os.path.expanduser("~"), ".Trash")
        if os.path.isdir(p):
            paths.append(p)
    else:
        p = os.path.join(os.path.expanduser("~"), ".local", "share", "Trash", "files")
        if os.path.isdir(p):
            paths.append(p)
    return paths


def get_chrome_cache_paths():
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        p = os.path.join(local, "Google", "Chrome", "User Data", "Default", "Cache")
    elif system == "Darwin":
        p = os.path.join(home, "Library", "Caches", "Google", "Chrome", "Default", "Cache")
    else:
        p = os.path.join(home, ".cache", "google-chrome", "Default", "Cache")
    return [p] if os.path.isdir(p) else []


def get_firefox_cache_paths():
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local"))
        pattern = os.path.join(local, "Mozilla", "Firefox", "Profiles", "*", "cache2")
    elif system == "Darwin":
        pattern = os.path.join(home, "Library", "Caches", "Firefox", "Profiles", "*", "cache2")
    else:
        pattern = os.path.join(home, ".cache", "mozilla", "firefox", "*", "cache2")
    return [p for p in glob.glob(pattern) if os.path.isdir(p)]


def get_thumbnail_cache_paths():
    if platform.system() != "Windows":
        return []
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return []
    p = os.path.join(local, "Microsoft", "Windows", "Explorer")
    return [p] if os.path.isdir(p) else []


# =====================================================================
# Cleanup categories
# =====================================================================

CLEANUP_CATEGORIES = [
    {
        "id": "system_temp", "label": "System Temp Files",
        "description": "Temporary files created by the OS and apps.",
        "paths_func": lambda: [get_temp_dir()],
    },
    {
        "id": "drive_junk", "label": "Other Drives' Temp Folders",
        "description": "Known temp/trash folders found on other local/mapped drives.",
        "paths_func": get_drive_junk_paths_all,
    },
    {
        "id": "recycle_bin", "label": "Recycle Bin / Trash",
        "description": "Files sitting in the Recycle Bin or Trash.",
        "paths_func": get_recycle_bin_paths,
    },
    {
        "id": "chrome_cache", "label": "Chrome Browser Cache",
        "description": "Cached web content from Google Chrome. Safe to clear.",
        "paths_func": get_chrome_cache_paths,
    },
    {
        "id": "firefox_cache", "label": "Firefox Browser Cache",
        "description": "Cached web content from Firefox. Safe to clear.",
        "paths_func": get_firefox_cache_paths,
    },
    {
        "id": "thumbnail_cache", "label": "Thumbnail Cache (Windows)",
        "description": "Cached thumbnail images; Windows regenerates these.",
        "paths_func": get_thumbnail_cache_paths,
    },
]


# =====================================================================
# Cleanup / scanning primitives
# =====================================================================

def new_clean_result():
    return {
        "deleted_bytes": 0,      # bytes moved into Recovery
        "skipped_recent": 0,     # too recent / in use / other failure
        "skipped_permission": 0, # permission denied (may also mean file in use)
        "deleted_paths": [],     # list of (path, size) moved to Recovery
        "permission_paths": [],  # list of paths that were denied
    }


def merge_clean_result(into, other):
    into["deleted_bytes"] += other["deleted_bytes"]
    into["skipped_recent"] += other["skipped_recent"]
    into["skipped_permission"] += other["skipped_permission"]
    into["deleted_paths"].extend(other["deleted_paths"])
    into["permission_paths"].extend(other["permission_paths"])
    return into


FLUSH_EVERY = 200


def clean_folder(folder, min_age_seconds=3600):
    """Move cleanable files into PC Optimizer Recovery (not a real delete).
    The Recovery index is written in batches instead of once per file."""
    result = new_clean_result()
    if not folder or not os.path.isdir(folder):
        return result
    if is_inside(folder, CONFIG_DIR):
        return result

    now = time.time()
    pending = []

    def flush():
        nonlocal pending
        if not pending:
            return
        batch = pending
        pending = []
        if add_recovery_records(batch):
            for rec in batch:
                result["deleted_bytes"] += rec["size"]
                result["deleted_paths"].append((rec["original_path"], rec["size"]))
        else:
            rollback_moves(batch)
            result["skipped_recent"] += len(batch)

    for root, dirs, files in os.walk(folder, topdown=False):
        if is_inside(root, CONFIG_DIR):
            continue
        for file in files:
            path = os.path.join(root, file)
            try:
                st = os.stat(path)
                if now - st.st_mtime < min_age_seconds:
                    result["skipped_recent"] += 1
                    continue
                success, record, error = _move_to_recovery_raw(path)
                if success:
                    pending.append(record)
                    if len(pending) >= FLUSH_EVERY:
                        flush()
                elif error and ("permission" in error.lower() or "access" in error.lower()):
                    result["skipped_permission"] += 1
                    result["permission_paths"].append(path)
                else:
                    result["skipped_recent"] += 1
            except PermissionError:
                result["skipped_permission"] += 1
                result["permission_paths"].append(path)
            except (FileNotFoundError, OSError):
                result["skipped_recent"] += 1
    flush()
    return result


def scan_folder_cleanable(folder, min_age_seconds):
    if not folder or not os.path.isdir(folder):
        return 0, 0
    if is_inside(folder, CONFIG_DIR):
        return 0, 0

    total = 0
    count = 0
    now = time.time()

    for root, dirs, files in os.walk(folder):
        if is_inside(root, CONFIG_DIR):
            continue
        for f in files:
            path = os.path.join(root, f)
            try:
                st = os.stat(path)
                if now - st.st_mtime < min_age_seconds:
                    continue
                total += st.st_size
                count += 1
            except OSError:
                pass

    return total, count


def scan_category(category, min_age_seconds):
    total = 0
    count = 0
    for p in category["paths_func"]():
        s, c = scan_folder_cleanable(p, min_age_seconds)
        total += s
        count += c
    return total, count


def clean_category(category, min_age_seconds):
    result = new_clean_result()
    for p in category["paths_func"]():
        merge_clean_result(result, clean_folder(p, min_age_seconds))
    return result


def clean_drive_junk(mountpoint, min_age_seconds=3600):
    folders = get_junk_folders_for_drive(mountpoint)
    result = new_clean_result()
    for folder in folders:
        merge_clean_result(result, clean_folder(folder, min_age_seconds))
    result["folders"] = folders
    return result


# =====================================================================
# Cleanup reports
# =====================================================================

REPORTS_DIR = os.path.join(CONFIG_DIR, "reports")


def write_cleanup_report(deleted_paths, permission_paths, skipped_recent, freed_bytes, categories):
    try:
        os.makedirs(REPORTS_DIR, exist_ok=True)
    except OSError:
        return None

    timestamp = datetime.now()
    filename = f"cleanup_{timestamp.strftime('%Y-%m-%d_%H-%M-%S')}.txt"
    report_path = os.path.join(REPORTS_DIR, filename)

    lines = [
        "PC Optimizer -- Cleanup Report",
        f"Date: {timestamp.strftime('%b %d, %Y at %I:%M %p')}",
        f"Categories cleaned: {', '.join(categories) if categories else 'N/A'}",
        f"Total moved to Recovery: {format_bytes(freed_bytes)}",
        "(Disk space is only reclaimed once Recovery is emptied.)",
        f"Files moved to Recovery: {len(deleted_paths)}",
        f"Files skipped (too recent / other): {skipped_recent}",
        f"Files skipped (permission denied or in use): {len(permission_paths)}",
        "",
        "=" * 70,
        f"MOVED TO RECOVERY ({len(deleted_paths)})",
        "=" * 70,
    ]
    for path, size in deleted_paths:
        lines.append(f"{format_bytes(size):>10}  {path}")

    if permission_paths:
        lines.append("")
        lines.append("=" * 70)
        lines.append(f"SKIPPED -- PERMISSION DENIED OR IN USE ({len(permission_paths)})")
        lines.append("=" * 70)
        lines.extend(permission_paths)

    try:
        with open(report_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
        return report_path
    except OSError:
        return None


# =====================================================================
# Storage analyzer
# =====================================================================

def analyze_top_level(path, max_items=25):
    entries = []
    try:
        with os.scandir(path) as it:
            for entry in it:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        size = get_folder_size(entry.path)
                        entries.append((entry.name, size, True))
                    elif entry.is_file(follow_symlinks=False):
                        size = entry.stat().st_size
                        entries.append((entry.name, size, False))
                except OSError:
                    continue
    except (PermissionError, FileNotFoundError, OSError):
        pass

    entries.sort(key=lambda x: x[1], reverse=True)
    return entries[:max_items]


# =====================================================================
# Large file finder
# =====================================================================

POSIX_SKIP_DIRS = ("/proc", "/sys", "/dev", "/run")


def find_large_files(root_path, min_size_bytes, max_results=100, cancel_event=None):
    results = []
    is_windows = platform.system() == "Windows"

    for dirpath, dirnames, filenames in os.walk(root_path, onerror=lambda e: None):
        if cancel_event is not None and cancel_event.is_set():
            break

        # Prune virtual filesystems, symlinked dirs, and our own data folder.
        kept = []
        for d in dirnames:
            full = os.path.join(dirpath, d)
            if not is_windows and full in POSIX_SKIP_DIRS:
                continue
            if os.path.islink(full):
                continue
            if is_inside(full, CONFIG_DIR):
                continue
            kept.append(d)
        dirnames[:] = kept

        for f in filenames:
            path = os.path.join(dirpath, f)
            try:
                st = os.lstat(path)
                if not stat_module.S_ISREG(st.st_mode):
                    continue
                if st.st_size >= min_size_bytes:
                    results.append((path, st.st_size))
            except OSError:
                continue

    results.sort(key=lambda x: x[1], reverse=True)
    return results[:max_results]


# =====================================================================
# Startup apps (Windows only)
# =====================================================================

STARTUP_REG_LOCATIONS = [
    (getattr(winreg, "HKEY_CURRENT_USER", None), r"Software\Microsoft\Windows\CurrentVersion\Run", "HKCU"),
    (getattr(winreg, "HKEY_LOCAL_MACHINE", None), r"Software\Microsoft\Windows\CurrentVersion\Run", "HKLM"),
]


def get_startup_items():
    items = []
    if winreg is None:
        return items

    for hive, path, label in STARTUP_REG_LOCATIONS:
        try:
            with winreg.OpenKey(hive, path) as key:
                i = 0
                while True:
                    try:
                        name, value, _ = winreg.EnumValue(key, i)
                        items.append({
                            "name": name, "command": value, "location": label,
                            "reg_path": path, "hive": hive,
                        })
                        i += 1
                    except OSError:
                        break
        except (FileNotFoundError, OSError):
            continue

    appdata = os.environ.get("APPDATA")
    if appdata:
        startup_dir = os.path.join(appdata, "Microsoft", "Windows", "Start Menu", "Programs", "Startup")
        if os.path.isdir(startup_dir):
            for f in os.listdir(startup_dir):
                items.append({
                    "name": f, "command": os.path.join(startup_dir, f),
                    "location": "Startup Folder", "reg_path": None, "hive": None,
                })

    return items


def remove_startup_item(item):
    if item["location"] == "Startup Folder":
        try:
            os.remove(item["command"])
            return True, None
        except OSError as e:
            return False, str(e)

    try:
        with winreg.OpenKey(item["hive"], item["reg_path"], 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, item["name"])
        return True, None
    except OSError as e:
        return False, str(e)


# =====================================================================
# Process list helper
# =====================================================================

def refresh_process_rows(proc_cache, limit=60):
    current_pids = set()

    for p in psutil.process_iter(["pid"]):
        pid = p.info["pid"]
        current_pids.add(pid)
        if pid not in proc_cache:
            try:
                p.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            proc_cache[pid] = p

    for pid in list(proc_cache.keys()):
        if pid not in current_pids:
            del proc_cache[pid]

    rows = []
    for pid, proc in list(proc_cache.items()):
        try:
            rows.append((pid, proc.name(), proc.cpu_percent(None), proc.memory_percent()))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            proc_cache.pop(pid, None)

    rows.sort(key=lambda r: r[2], reverse=True)
    return rows[:limit]


# =====================================================================
# GUI
# =====================================================================

class PCOptimizer:

    def __init__(self, root):
        self.root = root
        self.root.title("PC Optimizer")
        self.root.geometry("1150x720")
        self.root.configure(bg=COLOR_BG)

        self.config = load_config()

        psutil.cpu_percent(interval=None)  # prime overall CPU sampling

        self._active_page = None
        self._page_generation = 0

        self.nav_buttons = {}
        self._proc_cache = {}
        self._after_ids = {}

        self._drives_request_id = 0
        self._loading_active = False
        self._loading_dots = 0

        # Busy flags (reset whenever the page changes or a job fails)
        self._dashboard_tick_pending = False
        self._processes_tick_pending = False
        self._cleanup_running = False
        self._storage_running = False
        self._largefile_running = False
        self._drive_clean_running = False
        self._recovery_busy = False
        self._largefile_cancel = None

        # Thread-safe queue that hands finished background work back to Tk.
        self._gui_queue = queue.Queue()
        self.root.after(50, self.process_gui_queue)

        self._setup_ttk_style()

        self.root.report_callback_exception = self.handle_tk_exception

        self.create_sidebar()
        self.create_main_area()

        self.navigate_dashboard()

    def handle_tk_exception(self, exc_type, exc_value, exc_tb):
        import traceback
        traceback.print_exception(exc_type, exc_value, exc_tb)
        try:
            messagebox.showerror(
                "Unexpected Error",
                f"Something went wrong on this page:\n\n{exc_value}\n\n"
                "You can keep using the app -- try refreshing this page."
            )
        except tk.TclError:
            pass

    def _setup_ttk_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass

        style.configure(
            "Treeview",
            background=COLOR_CARD, fieldbackground=COLOR_CARD,
            foreground=COLOR_TEXT, rowheight=26, borderwidth=0
        )
        style.map("Treeview", background=[("selected", COLOR_ACCENT)])
        style.configure(
            "Treeview.Heading",
            background=COLOR_SIDEBAR, foreground=COLOR_TEXT,
            font=("Segoe UI", 10, "bold"), borderwidth=0
        )
        style.configure("TCombobox", fieldbackground=COLOR_CARD, background=COLOR_CARD)

    # -------------------------
    # Scheduling helpers (prevent stacked refresh loops)
    # -------------------------

    def schedule(self, name, ms, fn):
        self.cancel_schedule(name)
        try:
            self._after_ids[name] = self.root.after(ms, fn)
        except tk.TclError:
            pass

    def cancel_schedule(self, name):
        after_id = self._after_ids.pop(name, None)
        if after_id is not None:
            try:
                self.root.after_cancel(after_id)
            except (tk.TclError, ValueError):
                pass

    def cancel_all_schedules(self):
        for name in list(self._after_ids.keys()):
            self.cancel_schedule(name)

    # -------------------------
    # Small UI helpers
    # -------------------------

    def add_hover(self, widget, normal_bg, hover_bg):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg) if str(widget.cget("state")) != "disabled" else None)
        widget.bind("<Leave>", lambda e: widget.config(bg=normal_bg))

    def make_button(self, parent, text, command, bg=COLOR_ACCENT, hover=COLOR_ACCENT_HOVER,
                    font=("Segoe UI", 10, "bold"), padx=18, pady=10):
        btn = tk.Button(
            parent, text=text, command=command, font=font, fg="white",
            bg=bg, activebackground=hover, bd=0, padx=padx, pady=pady,
            cursor="hand2", disabledforeground="#cbd5e1"
        )
        self.add_hover(btn, bg, hover)
        return btn

    def start_loading(self, label_widget, base_text="Scanning"):
        self._loading_active = True
        self._loading_dots = 0

        def step():
            if not self._loading_active:
                return
            try:
                dots = "." * (self._loading_dots % 4)
                label_widget.config(text=f"{base_text}{dots}")
            except tk.TclError:
                return
            self._loading_dots += 1
            self.root.after(400, step)

        step()

    def stop_loading(self):
        self._loading_active = False

    def show_text_viewer(self, title, content):
        try:
            win = tk.Toplevel(self.root)
        except tk.TclError:
            return

        win.title(title)
        win.geometry("750x550")
        win.configure(bg=COLOR_BG)

        frame = tk.Frame(win, bg=COLOR_BG)
        frame.pack(fill="both", expand=True, padx=10, pady=(10, 5))

        text_widget = tk.Text(
            frame, bg=COLOR_CARD, fg="white", insertbackground="white",
            wrap="none", font=("Consolas", 10), bd=0
        )
        text_widget.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(frame, orient="vertical", command=text_widget.yview)
        scrollbar.pack(side="right", fill="y")
        text_widget.configure(yscrollcommand=scrollbar.set)

        text_widget.insert("1.0", content)
        text_widget.configure(state="disabled")

        self.make_button(win, "Close", win.destroy, bg="#334155", hover="#475569").pack(pady=(0, 10))

    def view_report_file(self, report_path):
        if not report_path or not os.path.isfile(report_path):
            messagebox.showerror(
                "Report Not Found",
                "That report file couldn't be found -- it may have been moved or deleted."
            )
            return
        try:
            with open(report_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            messagebox.showerror("Could Not Open Report", str(e))
            return
        self.show_text_viewer(f"Cleanup Report -- {os.path.basename(report_path)}", content)

    # -------------------------
    # Background work
    # -------------------------

    def process_gui_queue(self):
        """Deliver a small batch of finished jobs to the Tk main thread."""
        max_callbacks = 10
        processed = 0

        while processed < max_callbacks:
            try:
                callback, payload = self._gui_queue.get_nowait()
            except queue.Empty:
                break

            try:
                callback(payload)
            except tk.TclError:
                pass
            except Exception:
                self.handle_tk_exception(*sys.exc_info())
            processed += 1

        try:
            self.root.after(50, self.process_gui_queue)
        except tk.TclError:
            pass

    def make_error_handler(self, reset=None, title="Background Task Failed"):
        """Build an on_error callback that resets busy flags/buttons and
        shows the error, so a failed job can never leave the UI stuck."""
        def handler(exc):
            self.stop_loading()
            try:
                if reset:
                    reset()
            except tk.TclError:
                pass
            try:
                messagebox.showerror(title, f"{exc}")
            except tk.TclError:
                pass
        return handler

    def run_background(self, work_fn, on_done, on_error=None):
        """
        Run work_fn() in a worker thread. On success on_done(result) is called
        on the Tk thread; if work_fn raises, on_error(exception) is called
        instead. work_fn must never touch Tk widgets.
        """
        if on_error is None:
            on_error = self.make_error_handler()

        def worker():
            try:
                result = work_fn()
            except Exception as e:
                print("Background worker error:", e)
                self._gui_queue.put((on_error, e))
                return
            self._gui_queue.put((on_done, result))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_drives_async(self, callback):
        self.run_background(get_all_drives, callback, on_error=lambda e: callback([]))

    # -------------------------
    # Sidebar
    # -------------------------

    def create_sidebar(self):
        self.sidebar = tk.Frame(self.root, bg=COLOR_SIDEBAR, width=220)
        self.sidebar.pack(side="left", fill="y")

        tk.Label(
            self.sidebar, text="PC OPTIMIZER", font=("Segoe UI", 16, "bold"),
            fg="white", bg=COLOR_SIDEBAR
        ).pack(pady=(25, 20))

        nav_items = [
            ("dashboard", "Dashboard", self.navigate_dashboard),
            ("diagnostics", "Diagnostics", self.navigate_diagnostics),
            ("drives", "Drives", self.navigate_drives),
            ("storage", "Storage Analyzer", self.navigate_storage_analyzer),
            ("cleanup", "Cleanup", self.navigate_cleanup),
            ("largefiles", "Large File Finder", self.navigate_large_files),
            ("processes", "Process Viewer", self.navigate_processes),
            ("startup", "Startup Apps", self.navigate_startup),
            ("history", "Cleanup History", self.navigate_history),
            ("recovery", "Recovery", self.navigate_recovery),
            ("settings", "Settings", self.navigate_settings),
        ]

        for key, text, command in nav_items:
            self.create_nav_button(key, text, command)

    def create_nav_button(self, key, text, command):
        button = tk.Button(
            self.sidebar, text=text, command=command, font=("Segoe UI", 10),
            fg="#d1d5db", bg=COLOR_SIDEBAR, activebackground=COLOR_NAV_HOVER,
            activeforeground="white", bd=0, relief="flat", anchor="w",
            padx=25, pady=9, cursor="hand2"
        )
        button.pack(fill="x")
        button.bind("<Enter>", lambda e, b=button: b.config(bg=COLOR_NAV_HOVER) if self._active_page != key else None)
        button.bind("<Leave>", lambda e, b=button: self.update_nav_highlight())
        self.nav_buttons[key] = button

    def update_nav_highlight(self):
        for key, button in self.nav_buttons.items():
            if key == self._active_page:
                button.config(bg=COLOR_ACCENT, fg="white")
            else:
                button.config(bg=COLOR_SIDEBAR, fg="#d1d5db")

    # -------------------------
    # Main area / shared widgets
    # -------------------------

    def create_main_area(self):
        self.main = tk.Frame(self.root, bg=COLOR_BG)
        self.main.pack(side="left", fill="both", expand=True)

    def clear_main(self):
        self.stop_loading()
        for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
            try:
                self.root.unbind_all(seq)
            except tk.TclError:
                pass
        for widget in self.main.winfo_children():
            widget.destroy()

    def set_page(self, key):
        # New page generation: any in-flight refresh belonging to the old
        # page will see the mismatch and drop its result.
        self._page_generation += 1
        self._active_page = key

        # Kill old refresh loops so they can't stack, and clear busy flags
        # that belonged to the old page's refresh loops.
        self.cancel_all_schedules()
        self._dashboard_tick_pending = False
        self._processes_tick_pending = False

        self.update_nav_highlight()
        self.clear_main()

    def create_header(self, title):
        header = tk.Frame(self.main, bg=COLOR_BG)
        header.pack(fill="x", padx=35, pady=(30, 20))
        tk.Label(
            header, text=title, font=("Segoe UI", 24, "bold"),
            fg="white", bg=COLOR_BG
        ).pack(side="left")
        return header

    def make_scrollable(self, parent):
        """Returns the inner frame to fill. The inner frame always matches the
        canvas width, and the mouse wheel scrolls it."""
        container = tk.Frame(parent, bg=COLOR_BG)
        container.pack(fill="both", expand=True, padx=35, pady=(0, 20))

        canvas = tk.Canvas(container, bg=COLOR_BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=COLOR_BG)

        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(window_id, width=e.width))
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        def on_wheel(event):
            try:
                if event.num == 4:
                    canvas.yview_scroll(-3, "units")
                elif event.num == 5:
                    canvas.yview_scroll(3, "units")
                elif platform.system() == "Darwin":
                    canvas.yview_scroll(int(-event.delta), "units")
                else:
                    canvas.yview_scroll(int(-event.delta / 120) * 3, "units")
            except tk.TclError:
                pass

        # Removed again in clear_main() when the page changes.
        self.root.bind_all("<MouseWheel>", on_wheel)
        self.root.bind_all("<Button-4>", on_wheel)
        self.root.bind_all("<Button-5>", on_wheel)

        return inner

    # =====================================================================
    # 1) Dashboard
    # =====================================================================

    def navigate_dashboard(self):
        self.set_page("dashboard")
        self.create_header("System Dashboard")

        cards = tk.Frame(self.main, bg=COLOR_BG)
        cards.pack(fill="x", padx=35)

        self.dash_labels = {}
        self.dash_labels["cpu"] = self.create_metric_card(cards, "CPU", "…", 0)
        self.dash_labels["memory"] = self.create_metric_card(cards, "Memory", "…", 1)
        self.dash_labels["disk"] = self.create_metric_card(cards, f"Disk ({get_root_label()})", "…", 2)
        self.dash_labels["health"] = self.create_metric_card(cards, "Health", "…", 3)

        graphs = tk.Frame(self.main, bg=COLOR_BG)
        graphs.pack(fill="x", padx=35, pady=(20, 0))
        graphs.columnconfigure(0, weight=1)
        graphs.columnconfigure(1, weight=1)

        self.cpu_canvas = self.create_graph_card(graphs, "CPU Usage", 0)
        self.mem_canvas = self.create_graph_card(graphs, "Memory Usage", 1)

        self.cpu_history = deque(maxlen=60)
        self.mem_history = deque(maxlen=60)

        self.rec_frame = tk.Frame(self.main, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        self.rec_frame.pack(fill="both", expand=True, padx=35, pady=(20, 30))

        tk.Label(
            self.rec_frame, text="Optimization Recommendations", font=("Segoe UI", 14, "bold"),
            fg="white", bg=COLOR_CARD
        ).pack(anchor="w", padx=25, pady=(18, 10))

        self.rec_list_frame = tk.Frame(self.rec_frame, bg=COLOR_CARD)
        self.rec_list_frame.pack(fill="x", padx=25, pady=(0, 15))
        self.render_recommendations(["Checking system health..."])

        self.tick_dashboard()

    def create_metric_card(self, parent, title, value, column):
        card = tk.Frame(parent, bg=COLOR_CARD, width=150, height=110,
                        highlightbackground=COLOR_BORDER, highlightthickness=1)
        card.grid(row=0, column=column, padx=8, sticky="nsew")
        card.grid_propagate(False)
        parent.columnconfigure(column, weight=1)

        tk.Label(card, text=title, font=("Segoe UI", 10), fg=COLOR_MUTED, bg=COLOR_CARD).pack(pady=(18, 4))
        value_label = tk.Label(card, text=value, font=("Segoe UI", 22, "bold"), fg="white", bg=COLOR_CARD)
        value_label.pack()
        return value_label

    def create_graph_card(self, parent, title, column):
        card = tk.Frame(parent, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        card.grid(row=0, column=column, padx=8, sticky="nsew")

        tk.Label(card, text=title, font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_CARD).pack(
            anchor="w", padx=15, pady=(12, 5)
        )
        canvas = tk.Canvas(card, bg=COLOR_CARD, height=90, highlightthickness=0)
        canvas.pack(fill="x", padx=15, pady=(0, 15))
        return canvas

    def draw_graph(self, canvas, history, color):
        try:
            w = canvas.winfo_width()
            h = canvas.winfo_height()
        except tk.TclError:
            return
        if w <= 1 or h <= 1:
            w, h = 400, 90

        canvas.delete("all")
        n = len(history)
        if n < 2:
            return

        step = w / (n - 1)
        points = []
        for i, v in enumerate(history):
            x = i * step
            y = h - (max(0, min(v, 100)) / 100) * (h - 4) - 2
            points.extend([x, y])

        canvas.create_line(0, h - 1, w, h - 1, fill=COLOR_BORDER)
        canvas.create_line(*points, fill=color, width=2, smooth=True)
        canvas.create_text(
            w - 6, 8, text=f"{history[-1]:.0f}%", fill="white",
            anchor="ne", font=("Segoe UI", 10, "bold")
        )

    def render_recommendations(self, recommendations):
        for widget in self.rec_list_frame.winfo_children():
            widget.destroy()
        for rec in recommendations:
            tk.Label(
                self.rec_list_frame, text="•  " + rec, font=("Segoe UI", 11),
                fg="#d1d5db", bg=COLOR_CARD
            ).pack(anchor="w", pady=4)

    def tick_dashboard(self):
        if self._active_page != "dashboard":
            return

        generation = self._page_generation
        interval = int(self.config["refresh_interval_sec"] * 1000)

        if self._dashboard_tick_pending:
            self.schedule("dashboard", interval, self.tick_dashboard)
            return
        self._dashboard_tick_pending = True

        def collect_metrics():
            return (get_cpu_usage(), get_memory_usage(), get_disk_usage())

        def apply_metrics(result):
            if generation != self._page_generation:
                return  # this result belongs to a page that no longer exists
            self._dashboard_tick_pending = False

            cpu, memory, disk = result
            try:
                self.dash_labels["cpu"].config(text=f"{cpu:.0f}%")
                self.dash_labels["memory"].config(text=f"{memory:.0f}%")
                self.dash_labels["disk"].config(text=f"{disk:.0f}%")
                self.dash_labels["health"].config(text=f"{get_health_score(cpu, memory, disk)}/100")

                self.cpu_history.append(cpu)
                self.mem_history.append(memory)
                self.draw_graph(self.cpu_canvas, self.cpu_history, COLOR_GRAPH_CPU)
                self.draw_graph(self.mem_canvas, self.mem_history, COLOR_GRAPH_MEM)

                self.render_recommendations(get_recommendations(cpu, memory, disk))
            except tk.TclError:
                pass

            self.schedule("dashboard", interval, self.tick_dashboard)

        def failed(exc):
            # Keep the refresh loop alive even if one reading fails.
            if generation != self._page_generation:
                return
            self._dashboard_tick_pending = False
            self.schedule("dashboard", interval, self.tick_dashboard)

        self.run_background(collect_metrics, apply_metrics, on_error=failed)

    # =====================================================================
    # 2) Diagnostics
    # =====================================================================

    def navigate_diagnostics(self):
        self.set_page("diagnostics")
        generation = self._page_generation
        self.create_header("System Diagnostics")

        frame = tk.Frame(self.main, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=35, pady=10)

        row_names = ["CPU Usage", "Memory Usage", "Disk Space", "Internet Connection"]
        rows = [self.create_diagnostic_row(frame, name, None, checking=True) for name in row_names]

        def collect_checks():
            return [
                get_cpu_usage() < 80,
                get_memory_usage() < 85,
                get_disk_usage() < 90,
                self.check_internet(),
            ]

        def finished(results):
            if generation != self._page_generation:
                return
            try:
                for (symbol_label, text_label), name, ok in zip(rows, row_names, results):
                    symbol_label.config(text="✓" if ok else "⚠", fg=COLOR_SUCCESS if ok else COLOR_WARNING)
                    text_label.config(text=name)
            except tk.TclError:
                pass

        def failed(exc):
            if generation != self._page_generation:
                return
            try:
                for symbol_label, text_label in rows:
                    symbol_label.config(text="⚠", fg=COLOR_WARNING)
                    text_label.config(text=text_label.cget("text").replace(" (checking...)", " (check failed)"))
            except tk.TclError:
                pass

        self.run_background(collect_checks, finished, on_error=failed)

    def create_diagnostic_row(self, parent, name, status, checking=False):
        if checking:
            symbol, color, label_text = "…", COLOR_MUTED, f"{name} (checking...)"
        else:
            symbol = "✓" if status else "⚠"
            color = COLOR_SUCCESS if status else COLOR_WARNING
            label_text = name

        row = tk.Frame(parent, bg=COLOR_CARD)
        row.pack(fill="x", padx=25, pady=15)
        symbol_label = tk.Label(row, text=symbol, font=("Segoe UI", 18, "bold"), fg=color, bg=COLOR_CARD)
        symbol_label.pack(side="left")
        text_label = tk.Label(row, text=label_text, font=("Segoe UI", 12), fg="white", bg=COLOR_CARD)
        text_label.pack(side="left", padx=15)
        return symbol_label, text_label

    def check_internet(self):
        try:
            with socket.create_connection(("8.8.8.8", 53), timeout=2):
                return True
        except OSError:
            return False

    # =====================================================================
    # 3) Drives
    # =====================================================================

    def navigate_drives(self):
        self.set_page("drives")
        header = self.create_header("Drives")
        refresh_btn = self.make_button(header, "Refresh", self.navigate_drives, bg="#334155", hover="#475569")
        refresh_btn.pack(side="right")

        list_frame = self.make_scrollable(self.main)
        loading_label = tk.Label(list_frame, text="", font=("Segoe UI", 11, "italic"),
                                 fg=COLOR_MUTED, bg=COLOR_BG)
        loading_label.pack(anchor="w", pady=20)
        self.start_loading(loading_label, "Loading drives")

        self._drives_request_id += 1
        my_request_id = self._drives_request_id
        try:
            refresh_btn.config(state="disabled")
        except tk.TclError:
            pass

        def on_drives(drives):
            if self._active_page != "drives" or my_request_id != self._drives_request_id:
                return
            self.stop_loading()

            try:
                refresh_btn.config(state="normal")
            except tk.TclError:
                pass

            try:
                for widget in list_frame.winfo_children():
                    widget.destroy()
            except tk.TclError:
                return

            if not drives:
                tk.Label(list_frame, text="No drives could be detected.", font=("Segoe UI", 12),
                         fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
                return

            for drive in drives:
                try:
                    self.create_drive_card(list_frame, drive)
                except Exception as e:
                    error_row = tk.Frame(list_frame, bg=COLOR_CARD,
                                         highlightbackground=COLOR_DANGER, highlightthickness=1)
                    error_row.pack(fill="x", pady=4, ipady=8)
                    tk.Label(
                        error_row, text=f"Couldn't display {drive.get('mountpoint', 'a drive')}: {e}",
                        font=("Segoe UI", 10), fg=COLOR_WARNING, bg=COLOR_CARD, wraplength=800, justify="left"
                    ).pack(anchor="w", padx=15)

        self.fetch_drives_async(on_drives)

    def create_drive_card(self, parent, drive):
        card = tk.Frame(parent, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        card.pack(fill="x", pady=8, ipady=10)

        top_row = tk.Frame(card, bg=COLOR_CARD)
        top_row.pack(fill="x", padx=20, pady=(15, 5))

        label_text = drive["mountpoint"]
        if drive["device"] and drive["device"] != drive["mountpoint"]:
            label_text += f"   ({drive['device']})"

        tk.Label(top_row, text=label_text, font=("Segoe UI", 14, "bold"), fg="white", bg=COLOR_CARD).pack(side="left")

        if drive.get("fstype"):
            tk.Label(top_row, text=drive["fstype"], font=("Segoe UI", 10), fg=COLOR_MUTED, bg=COLOR_CARD).pack(
                side="left", padx=10
            )

        if drive.get("error") or drive["percent"] is None:
            tk.Label(
                card, text="Unavailable (drive may be disconnected or offline).",
                font=("Segoe UI", 11), fg=COLOR_WARNING, bg=COLOR_CARD
            ).pack(anchor="w", padx=20, pady=(0, 10))
            return

        percent = drive["percent"]

        bar_bg = tk.Frame(card, bg="#334155", height=14)
        bar_bg.pack(fill="x", padx=20, pady=(5, 10))
        bar_bg.pack_propagate(False)

        bar_color = COLOR_DANGER if percent > 90 else (COLOR_WARNING if percent > 75 else COLOR_SUCCESS)
        tk.Frame(bar_bg, bg=bar_color).place(relx=0, rely=0, relwidth=max(percent / 100, 0.01), relheight=1)

        stats_text = (
            f"{percent:.0f}% used   •   {format_bytes(drive['used'])} used of "
            f"{format_bytes(drive['total'])}   •   {format_bytes(drive['free'])} free"
        )
        tk.Label(card, text=stats_text, font=("Segoe UI", 10), fg="#d1d5db", bg=COLOR_CARD).pack(
            anchor="w", padx=20
        )

        action_row = tk.Frame(card, bg=COLOR_CARD)
        action_row.pack(fill="x", padx=20, pady=(10, 0))

        analyze_btn = self.make_button(
            action_row, "Analyze Storage",
            lambda m=drive["mountpoint"]: self.navigate_storage_analyzer(preselect=m),
            bg="#334155", hover="#475569", font=("Segoe UI", 9, "bold"), padx=12, pady=6
        )
        analyze_btn.pack(side="left", padx=(0, 10))

        junk_folders = get_junk_folders_for_drive(drive["mountpoint"])
        if junk_folders:
            clean_btn = self.make_button(
                action_row, "Clean Junk On This Drive", None,
                font=("Segoe UI", 9, "bold"), padx=12, pady=6
            )
            clean_btn.config(command=lambda m=drive["mountpoint"], b=clean_btn: self.clean_drive(m, b))
            clean_btn.pack(side="left")
        else:
            tk.Label(
                action_row, text="No known temp/junk folders found on this drive.",
                font=("Segoe UI", 9, "italic"), fg="#64748b", bg=COLOR_CARD
            ).pack(side="left")

    def clean_drive(self, mountpoint, button):
        if self._drive_clean_running:
            return

        junk_folders = get_junk_folders_for_drive(mountpoint)
        folder_list = "\n".join(junk_folders) if junk_folders else "(none found)"
        confirmed = messagebox.askyesno(
            "Confirm Cleanup",
            f"This will move files older than 1 hour into PC Optimizer Recovery from:\n\n{folder_list}\n\n"
            "Disk space is only reclaimed once you empty Recovery.\n\nContinue?"
        )
        if not confirmed:
            return

        self._drive_clean_running = True
        if button is not None:
            button.config(state="disabled")

        def do_clean():
            result = clean_drive_junk(mountpoint)
            category_label = f"Drive junk ({mountpoint})"
            report_path = write_cleanup_report(
                result["deleted_paths"], result["permission_paths"],
                result["skipped_recent"], result["deleted_bytes"], [category_label]
            )
            append_history_record({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "freed_bytes": result["deleted_bytes"], "skipped_files": result["skipped_recent"],
                "skipped_permission": result["skipped_permission"],
                "categories": [category_label],
                "report_path": report_path,
            })
            return result, report_path

        def finished(payload):
            result, report_path = payload
            self._drive_clean_running = False
            permission_line = (
                f"\nSkipped (permission denied or in use): {result['skipped_permission']}"
                if result["skipped_permission"] else ""
            )
            messagebox.showinfo(
                "Cleanup Complete",
                f"Cleanup finished for {mountpoint}.\n\n"
                f"Moved to Recovery: {format_bytes(result['deleted_bytes'])}\n"
                f"Files skipped (recent/other): {result['skipped_recent']}"
                f"{permission_line}\n"
                f"Folders scanned: {len(result['folders'])}\n\n"
                "Space is reclaimed when you empty Recovery."
            )
            if report_path and messagebox.askyesno(
                "View Report?", "View the full list of moved files now?"
            ):
                self.view_report_file(report_path)
            if self._active_page == "drives":
                self.navigate_drives()

        def reset():
            self._drive_clean_running = False
            if button is not None:
                button.config(state="normal")

        self.run_background(do_clean, finished, on_error=self.make_error_handler(reset, "Cleanup Failed"))

    # =====================================================================
    # 4) Storage Analyzer
    # =====================================================================

    def navigate_storage_analyzer(self, preselect=None):
        self.set_page("storage")
        self.create_header("Storage Analyzer")

        controls = tk.Frame(self.main, bg=COLOR_BG)
        controls.pack(fill="x", padx=35, pady=(0, 10))

        self.storage_drive_var = tk.StringVar()
        combo = ttk.Combobox(controls, textvariable=self.storage_drive_var, state="disabled", width=30)
        combo.pack(side="left")

        self.storage_analyze_btn = self.make_button(
            controls, "Analyze Storage", self.run_storage_analysis,
            font=("Segoe UI", 9, "bold"), padx=12, pady=6
        )
        self.storage_analyze_btn.config(state="disabled")
        self.storage_analyze_btn.pack(side="left", padx=10)

        self.storage_status_label = tk.Label(
            controls, text="Loading drives...", font=("Segoe UI", 9),
            fg=COLOR_MUTED, bg=COLOR_BG
        )
        self.storage_status_label.pack(side="left")

        self.storage_results_frame = tk.Frame(self.main, bg=COLOR_BG)
        self.storage_results_frame.pack(fill="both", expand=True, padx=35, pady=10)

        self._storage_request_id = getattr(self, "_storage_request_id", 0) + 1
        request_id = self._storage_request_id

        def on_drives(drives):
            if self._active_page != "storage" or request_id != self._storage_request_id:
                return
            try:
                mountpoints = [d["mountpoint"] for d in drives if not d.get("error")] or [get_root_path()]
                combo.config(values=mountpoints, state="readonly")
                combo.set(preselect if preselect in mountpoints else mountpoints[0])
                self.storage_analyze_btn.config(state="normal")
                self.storage_status_label.config(text="")
            except tk.TclError:
                pass

        self.fetch_drives_async(on_drives)

    def run_storage_analysis(self):
        if self._storage_running:
            return

        mount = self.storage_drive_var.get()
        if not mount:
            return

        self._storage_running = True
        page_generation = self._page_generation

        try:
            self.storage_analyze_btn.config(state="disabled")
            self.start_loading(self.storage_status_label, "Scanning")
        except tk.TclError:
            self._storage_running = False
            return

        def do_analysis():
            return analyze_top_level(mount, max_items=25)

        def finished(entries):
            self._storage_running = False
            if self._active_page != "storage" or page_generation != self._page_generation:
                return
            self.stop_loading()
            try:
                self.storage_analyze_btn.config(state="normal")
                self.storage_status_label.config(text="")
                self.render_storage_results(entries)
            except tk.TclError:
                pass

        def reset():
            self._storage_running = False
            if page_generation == self._page_generation:
                self.storage_analyze_btn.config(state="normal")
                self.storage_status_label.config(text="Scan failed.")

        self.run_background(do_analysis, finished, on_error=self.make_error_handler(reset, "Scan Failed"))

    def render_storage_results(self, entries):
        for widget in self.storage_results_frame.winfo_children():
            widget.destroy()

        if not entries:
            tk.Label(self.storage_results_frame, text="No accessible items found (or the folder is empty).",
                     font=("Segoe UI", 11), fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
            return

        max_size = max(size for _, size, _ in entries) or 1

        for name, size, is_dir in entries:
            row = tk.Frame(self.storage_results_frame, bg=COLOR_CARD,
                           highlightbackground=COLOR_BORDER, highlightthickness=1)
            row.pack(fill="x", pady=4, ipady=6)

            top = tk.Frame(row, bg=COLOR_CARD)
            top.pack(fill="x", padx=15, pady=(8, 4))

            icon = "📁" if is_dir else "📄"
            tk.Label(top, text=f"{icon}  {name}", font=("Segoe UI", 11), fg="white", bg=COLOR_CARD).pack(side="left")
            tk.Label(top, text=format_bytes(size), font=("Segoe UI", 11, "bold"), fg=COLOR_MUTED,
                     bg=COLOR_CARD).pack(side="right")

            bar_bg = tk.Frame(row, bg="#334155", height=8)
            bar_bg.pack(fill="x", padx=15, pady=(0, 8))
            bar_bg.pack_propagate(False)
            tk.Frame(bar_bg, bg=COLOR_ACCENT).place(relx=0, rely=0, relwidth=max(size / max_size, 0.01), relheight=1)

    # =====================================================================
    # 5) Cleanup
    # =====================================================================

    def navigate_cleanup(self):
        self.set_page("cleanup")
        self.create_header("Cleanup")

        intro = tk.Frame(self.main, bg=COLOR_BG)
        intro.pack(fill="x", padx=35, pady=(0, 10))
        tk.Label(
            intro,
            text=("Scan first to see what's cleanable, then pick which categories to clean. "
                  f"Files modified in the last {self.config['min_age_minutes']} minutes are always skipped. "
                  "Cleaned files are MOVED to the Recovery page, so disk space is only reclaimed "
                  "once you empty Recovery."),
            font=("Segoe UI", 10), fg="#d1d5db", bg=COLOR_BG, wraplength=800, justify="left"
        ).pack(anchor="w")

        controls = tk.Frame(self.main, bg=COLOR_BG)
        controls.pack(fill="x", padx=35, pady=(5, 10))

        self.cleanup_scan_btn = self.make_button(controls, "Scan for Cleanable Files", self.run_cleanup_scan)
        self.cleanup_scan_btn.pack(side="left")

        self.cleanup_status_label = tk.Label(controls, text="", font=("Segoe UI", 10, "italic"),
                                             fg=COLOR_MUTED, bg=COLOR_BG)
        self.cleanup_status_label.pack(side="left", padx=15)

        self.cleanup_results_frame = tk.Frame(self.main, bg=COLOR_BG)
        self.cleanup_results_frame.pack(fill="both", expand=True, padx=35, pady=(10, 20))

        tk.Label(
            self.cleanup_results_frame, text="Click \"Scan for Cleanable Files\" to get started.",
            font=("Segoe UI", 11), fg="#d1d5db", bg=COLOR_BG
        ).pack(anchor="w", pady=20)

        self.category_vars = {}
        self.category_sizes = {}

    def run_cleanup_scan(self):
        if self._cleanup_running:
            return
        self._cleanup_running = True
        self.cleanup_scan_btn.config(state="disabled")
        self.start_loading(self.cleanup_status_label, "Scanning")

        def do_scan():
            min_age = self.config["min_age_minutes"] * 60
            return [(cat, *scan_category(cat, min_age)) for cat in CLEANUP_CATEGORIES]

        def finished(results):
            self._cleanup_running = False
            self.stop_loading()
            try:
                self.cleanup_scan_btn.config(state="normal")
                self.cleanup_status_label.config(text="")
            except tk.TclError:
                pass
            if self._active_page == "cleanup":
                self.render_cleanup_checkboxes(results)

        def reset():
            self._cleanup_running = False
            self.cleanup_scan_btn.config(state="normal")
            self.cleanup_status_label.config(text="")

        self.run_background(do_scan, finished, on_error=self.make_error_handler(reset, "Scan Failed"))

    def render_cleanup_checkboxes(self, results):
        for widget in self.cleanup_results_frame.winfo_children():
            widget.destroy()

        self.category_vars = {}
        self.category_sizes = {}
        total = 0

        list_container = tk.Frame(self.cleanup_results_frame, bg=COLOR_CARD,
                                  highlightbackground=COLOR_BORDER, highlightthickness=1)
        list_container.pack(fill="both", expand=True)

        header_row = tk.Frame(list_container, bg=COLOR_CARD)
        header_row.pack(fill="x", padx=20, pady=(15, 5))

        self.make_button(header_row, "Select All", lambda: self.set_all_categories(True),
                         bg="#334155", hover="#475569", font=("Segoe UI", 9), padx=10, pady=5).pack(side="left")
        self.make_button(header_row, "Select None", lambda: self.set_all_categories(False),
                         bg="#334155", hover="#475569", font=("Segoe UI", 9), padx=10, pady=5).pack(
            side="left", padx=8
        )

        for cat, size, count in results:
            self.category_sizes[cat["id"]] = size
            var = tk.BooleanVar(value=(size > 0))
            self.category_vars[cat["id"]] = var

            total += size
            row = tk.Frame(list_container, bg=COLOR_CARD)
            row.pack(fill="x", padx=20, pady=8)

            cb = tk.Checkbutton(
                row, variable=var, bg=COLOR_CARD, activebackground=COLOR_CARD,
                selectcolor="#0f172a", fg="white", disabledforeground=COLOR_MUTED,
                state="normal" if size > 0 else "disabled"
            )
            cb.pack(side="left")

            text_frame = tk.Frame(row, bg=COLOR_CARD)
            text_frame.pack(side="left", fill="x", expand=True, padx=(5, 0))
            tk.Label(text_frame, text=f"{cat['label']} — {format_bytes(size)} ({count} files)",
                     font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_CARD).pack(anchor="w")
            tk.Label(text_frame, text=cat["description"], font=("Segoe UI", 9),
                     fg=COLOR_MUTED, bg=COLOR_CARD).pack(anchor="w")

        footer = tk.Frame(list_container, bg=COLOR_CARD)
        footer.pack(fill="x", padx=20, pady=(10, 15))
        tk.Label(footer, text=f"Total cleanable: {format_bytes(total)}", font=("Segoe UI", 11, "bold"),
                 fg="white", bg=COLOR_CARD).pack(side="left")

        self.cleanup_clean_btn = self.make_button(
            footer, "Clean Selected", self.run_cleanup_clean, bg=COLOR_GREEN, hover=COLOR_GREEN_HOVER
        )
        self.cleanup_clean_btn.pack(side="right")
        if total == 0:
            self.cleanup_clean_btn.config(state="disabled")

    def set_all_categories(self, value):
        for cat_id, var in self.category_vars.items():
            if self.category_sizes.get(cat_id, 0) > 0:
                var.set(value)

    def run_cleanup_clean(self):
        if self._cleanup_running:
            return

        selected = [cat for cat in CLEANUP_CATEGORIES
                    if self.category_vars.get(cat["id"]) and self.category_vars[cat["id"]].get()]

        if not selected:
            messagebox.showinfo("Nothing Selected", "Select at least one category to clean.")
            return

        total_est = sum(self.category_sizes.get(c["id"], 0) for c in selected)
        labels = "\n".join(f"• {c['label']}" for c in selected)

        confirmed = messagebox.askyesno(
            "Confirm Cleanup",
            f"This will move files older than {self.config['min_age_minutes']} minutes into Recovery from:\n\n"
            f"{labels}\n\nEstimated size: {format_bytes(total_est)}\n"
            "(Space is only reclaimed once you empty Recovery.)\n\nContinue?"
        )
        if not confirmed:
            return

        self._cleanup_running = True
        self.cleanup_clean_btn.config(state="disabled")

        def do_clean():
            min_age = self.config["min_age_minutes"] * 60
            combined = new_clean_result()
            cleaned_labels = []
            for cat in selected:
                merge_clean_result(combined, clean_category(cat, min_age))
                cleaned_labels.append(cat["label"])

            report_path = write_cleanup_report(
                combined["deleted_paths"], combined["permission_paths"],
                combined["skipped_recent"], combined["deleted_bytes"], cleaned_labels
            )

            append_history_record({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "freed_bytes": combined["deleted_bytes"], "skipped_files": combined["skipped_recent"],
                "skipped_permission": combined["skipped_permission"],
                "categories": cleaned_labels,
                "report_path": report_path,
            })
            return (combined, cleaned_labels, report_path)

        def finished(result):
            combined, labels_done, report_path = result
            self._cleanup_running = False
            permission_line = (
                f"\nFiles skipped (permission denied or in use): {combined['skipped_permission']}"
                if combined["skipped_permission"] else ""
            )
            messagebox.showinfo(
                "Cleanup Complete",
                f"Cleanup finished.\n\nMoved to Recovery: {format_bytes(combined['deleted_bytes'])}\n"
                f"Files skipped (recent/other): {combined['skipped_recent']}"
                f"{permission_line}\n\nCategories cleaned:\n" +
                "\n".join(f"• {l}" for l in labels_done) +
                "\n\nSpace is reclaimed when you empty Recovery."
            )
            if report_path and messagebox.askyesno(
                "View Report?", "View the full list of moved files now?"
            ):
                self.view_report_file(report_path)
            if self._active_page == "cleanup":
                self.navigate_cleanup()

        def reset():
            self._cleanup_running = False
            self.cleanup_clean_btn.config(state="normal")

        self.run_background(do_clean, finished, on_error=self.make_error_handler(reset, "Cleanup Failed"))

    # =====================================================================
    # 6) Large File Finder
    # =====================================================================

    LARGE_FILE_SIZE_OPTIONS = [
        ("50 MB", 50), ("100 MB", 100), ("250 MB", 250),
        ("500 MB", 500), ("1 GB", 1024), ("2 GB", 2048),
    ]

    def navigate_large_files(self):
        self.set_page("largefiles")
        self.create_header("Large File Finder")

        controls = tk.Frame(self.main, bg=COLOR_BG)
        controls.pack(fill="x", padx=35, pady=(0, 15))

        tk.Label(controls, text="Drive:", font=("Segoe UI", 11), fg="white", bg=COLOR_BG).pack(side="left")
        self.largefile_drive_var = tk.StringVar(value="Loading...")
        drive_combo = ttk.Combobox(controls, textvariable=self.largefile_drive_var, values=["Loading..."],
                                   state="disabled", width=25)
        drive_combo.pack(side="left", padx=(8, 20))

        tk.Label(controls, text="Minimum size:", font=("Segoe UI", 11), fg="white", bg=COLOR_BG).pack(side="left")
        default_label = next(
            (lbl for lbl, mb in self.LARGE_FILE_SIZE_OPTIONS if mb == self.config["large_file_min_mb"]),
            "100 MB"
        )
        self.largefile_size_var = tk.StringVar(value=default_label)
        ttk.Combobox(controls, textvariable=self.largefile_size_var,
                     values=[lbl for lbl, _ in self.LARGE_FILE_SIZE_OPTIONS],
                     state="readonly", width=12).pack(side="left", padx=8)

        self.largefile_find_btn = self.make_button(controls, "Find Large Files", self.run_large_file_scan)
        self.largefile_find_btn.config(state="disabled")
        self.largefile_find_btn.pack(side="left", padx=(15, 5))

        self.largefile_cancel_btn = self.make_button(
            controls, "Cancel", self.cancel_large_file_scan, bg="#334155", hover="#475569"
        )
        self.largefile_cancel_btn.config(state="disabled")
        self.largefile_cancel_btn.pack(side="left", padx=5)

        self.largefile_status_label = tk.Label(controls, text="", font=("Segoe UI", 10, "italic"),
                                               fg=COLOR_MUTED, bg=COLOR_BG)
        self.largefile_status_label.pack(side="left", padx=10)
        self.start_loading(self.largefile_status_label, "Loading drives")

        self.largefile_results_frame = self.make_scrollable(self.main)
        tk.Label(
            self.largefile_results_frame,
            text="Pick a drive and size threshold, then click Find Large Files. "
                 "Scanning a full drive can take a while -- you can cancel at any time.",
            font=("Segoe UI", 11), fg="#d1d5db", bg=COLOR_BG, wraplength=700, justify="left"
        ).pack(anchor="w", pady=20)

        self._largefile_request_id = getattr(self, "_largefile_request_id", 0) + 1
        my_request_id = self._largefile_request_id

        def on_drives(drives):
            if self._active_page != "largefiles" or my_request_id != self._largefile_request_id:
                return

            self.stop_loading()
            try:
                self.largefile_status_label.config(text="")
                mountpoints = [d["mountpoint"] for d in drives if not d.get("error")] or [get_root_path()]
                drive_combo.config(values=mountpoints, state="readonly")
                self.largefile_drive_var.set(mountpoints[0])
                self.largefile_find_btn.config(state="normal")
            except tk.TclError:
                return

        self.fetch_drives_async(on_drives)

    def cancel_large_file_scan(self):
        if self._largefile_cancel is not None:
            self._largefile_cancel.set()
        try:
            self.largefile_cancel_btn.config(state="disabled")
        except tk.TclError:
            pass

    def run_large_file_scan(self):
        if self._largefile_running:
            return

        mount = self.largefile_drive_var.get()
        size_label = self.largefile_size_var.get()
        min_mb = dict(self.LARGE_FILE_SIZE_OPTIONS).get(size_label, 100)
        min_bytes = min_mb * 1024 * 1024

        cancel_event = threading.Event()
        self._largefile_cancel = cancel_event
        self._largefile_running = True
        page_generation = self._page_generation
        self.largefile_find_btn.config(state="disabled")
        self.largefile_cancel_btn.config(state="normal")
        self.start_loading(self.largefile_status_label, "Scanning")

        def finished(results):
            self._largefile_running = False
            self.stop_loading()
            if page_generation != self._page_generation:
                return
            try:
                self.largefile_find_btn.config(state="normal")
                self.largefile_cancel_btn.config(state="disabled")
                self.largefile_status_label.config(
                    text="Cancelled -- showing partial results." if cancel_event.is_set() else ""
                )
            except tk.TclError:
                pass
            if self._active_page == "largefiles":
                self.render_large_file_results(results)

        def reset():
            self._largefile_running = False
            if page_generation == self._page_generation:
                self.largefile_find_btn.config(state="normal")
                self.largefile_cancel_btn.config(state="disabled")
                self.largefile_status_label.config(text="")

        self.run_background(
            lambda: find_large_files(mount, min_bytes, max_results=100, cancel_event=cancel_event),
            finished,
            on_error=self.make_error_handler(reset, "Scan Failed")
        )

    def render_large_file_results(self, results):
        for widget in self.largefile_results_frame.winfo_children():
            widget.destroy()

        if not results:
            tk.Label(self.largefile_results_frame, text="No files found at or above that size.",
                     font=("Segoe UI", 11), fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
            return

        for path, size in results:
            row = tk.Frame(self.largefile_results_frame, bg=COLOR_CARD,
                           highlightbackground=COLOR_BORDER, highlightthickness=1)
            row.pack(fill="x", pady=3, ipady=4)

            tk.Label(row, text=path, font=("Segoe UI", 10), fg="white", bg=COLOR_CARD,
                     anchor="w").pack(side="left", padx=15, fill="x", expand=True)
            tk.Label(row, text=format_bytes(size), font=("Segoe UI", 10, "bold"), fg=COLOR_MUTED,
                     bg=COLOR_CARD).pack(side="left", padx=10)

            if is_protected_path(path):
                tk.Label(row, text="System file", font=("Segoe UI", 9, "italic"),
                         fg=COLOR_WARNING, bg=COLOR_CARD).pack(side="right", padx=15)
                continue

            del_btn = self.make_button(
                row, "Move to Recovery", None, bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER,
                font=("Segoe UI", 9, "bold"), padx=10, pady=4
            )
            del_btn.config(command=lambda p=path, r=row: self.delete_large_file(p, r))
            del_btn.pack(side="right", padx=15)

    def delete_large_file(self, path, row_widget):
        if is_protected_path(path):
            messagebox.showerror("Protected File", "This looks like a system or program file and won't be moved.")
            return
        confirmed = messagebox.askyesno(
            "Move To Recovery",
            f"Move this file to PC Optimizer Recovery?\n\n{path}\n\nYou can restore it later."
        )
        if not confirmed:
            return
        success, record, error = move_file_to_recovery(path)
        if not success:
            messagebox.showerror("Could Not Move File", error or "Unknown error.")
            return
        try:
            row_widget.destroy()
        except tk.TclError:
            pass
        append_history_record({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "freed_bytes": record.get("size", 0), "skipped_files": 0,
            "skipped_permission": 0, "categories": ["Large File Finder -> Recovery"],
            "report_path": None,
        })
        messagebox.showinfo(
            "Moved To Recovery",
            f"Moved to Recovery:\n\n{path}\n\nSize: {format_bytes(record.get('size', 0))}"
        )

    # =====================================================================
    # 7) Process Viewer
    # =====================================================================

    def navigate_processes(self):
        self.set_page("processes")
        header = self.create_header("Process Viewer")

        self.process_end_btn = self.make_button(
            header, "End Process", self.end_selected_process,
            bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER
        )
        self.process_end_btn.config(state="disabled")
        self.process_end_btn.pack(side="right")

        note = tk.Label(
            self.main, text="CPU % becomes accurate after the first refresh (~2s).",
            font=("Segoe UI", 9, "italic"), fg=COLOR_MUTED, bg=COLOR_BG
        )
        note.pack(anchor="w", padx=35)

        table_frame = tk.Frame(self.main, bg=COLOR_BG)
        table_frame.pack(fill="both", expand=True, padx=35, pady=15)

        columns = ("name", "pid", "cpu", "mem")
        self.process_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)
        self.process_tree.heading("name", text="Name")
        self.process_tree.heading("pid", text="PID")
        self.process_tree.heading("cpu", text="CPU %")
        self.process_tree.heading("mem", text="Memory %")
        self.process_tree.column("name", width=350, anchor="w")
        self.process_tree.column("pid", width=100, anchor="center")
        self.process_tree.column("cpu", width=100, anchor="center")
        self.process_tree.column("mem", width=100, anchor="center")
        self.process_tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.process_tree.yview)
        self.process_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self.process_tree.bind("<<TreeviewSelect>>", self.on_process_select)

        self.tick_processes()

    def on_process_select(self, event):
        try:
            selected = self.process_tree.selection()
            self.process_end_btn.config(state="normal" if selected else "disabled")
        except tk.TclError:
            pass

    def tick_processes(self):
        if self._active_page != "processes":
            return

        generation = self._page_generation

        if self._processes_tick_pending:
            self.schedule("processes", 2000, self.tick_processes)
            return
        self._processes_tick_pending = True

        def collect_rows():
            return refresh_process_rows(self._proc_cache)

        def apply_rows(rows):
            if generation != self._page_generation:
                return
            self._processes_tick_pending = False

            try:
                tree = self.process_tree
                existing = set(tree.get_children())
                wanted = {str(pid) for pid, _, _, _ in rows}

                # Update rows in place (keeps scroll position and selection
                # instead of rebuilding the whole table every tick).
                for iid in existing - wanted:
                    tree.delete(iid)

                for idx, (pid, name, cpu, mem) in enumerate(rows):
                    iid = str(pid)
                    values = (name, pid, f"{cpu:.1f}", f"{mem:.1f}")
                    if iid in existing:
                        tree.item(iid, values=values)
                    else:
                        tree.insert("", idx, iid=iid, values=values)
                    tree.move(iid, "", idx)
            except tk.TclError:
                pass

            self.schedule("processes", 2000, self.tick_processes)

        def failed(exc):
            if generation != self._page_generation:
                return
            self._processes_tick_pending = False
            self.schedule("processes", 2000, self.tick_processes)

        self.run_background(collect_rows, apply_rows, on_error=failed)

    def end_selected_process(self):
        selection = self.process_tree.selection()
        if not selection:
            return
        pid = int(selection[0])
        values = self.process_tree.item(selection[0], "values")
        name = values[0] if values else str(pid)

        confirmed = messagebox.askyesno("End Process", f"End process '{name}' (PID {pid})?\n\nUnsaved work may be lost.")
        if not confirmed:
            return

        try:
            psutil.Process(pid).terminate()
            messagebox.showinfo("Process Ended", f"'{name}' was asked to terminate.")
        except (psutil.NoSuchProcess, psutil.AccessDenied) as e:
            messagebox.showerror("Could Not End Process", str(e))

    # =====================================================================
    # 8) Startup Apps
    # =====================================================================

    def navigate_startup(self):
        self.set_page("startup")
        generation = self._page_generation
        header = self.create_header("Startup Apps")

        if winreg is None:
            tk.Label(
                self.main, text="Startup app management is only available on Windows.",
                font=("Segoe UI", 12), fg="#d1d5db", bg=COLOR_BG
            ).pack(anchor="w", padx=35, pady=20)
            return

        self.startup_remove_btn = self.make_button(
            header, "Remove From Startup", self.remove_selected_startup_item,
            bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER
        )
        self.startup_remove_btn.config(state="disabled")
        self.startup_remove_btn.pack(side="right")

        tk.Label(
            self.main,
            text="Removing an entry here is not easily reversible unless you know its original value. "
                 "Only remove apps you recognize.",
            font=("Segoe UI", 9, "italic"), fg=COLOR_WARNING, bg=COLOR_BG, wraplength=800, justify="left"
        ).pack(anchor="w", padx=35)

        table_frame = tk.Frame(self.main, bg=COLOR_BG)
        table_frame.pack(fill="both", expand=True, padx=35, pady=15)

        columns = ("name", "location", "command")
        self.startup_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=20)
        self.startup_tree.heading("name", text="Name")
        self.startup_tree.heading("location", text="Location")
        self.startup_tree.heading("command", text="Command / Path")
        self.startup_tree.column("name", width=200, anchor="w")
        self.startup_tree.column("location", width=130, anchor="w")
        self.startup_tree.column("command", width=500, anchor="w")
        self.startup_tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.startup_tree.yview)
        self.startup_tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self.startup_tree.bind("<<TreeviewSelect>>", self.on_startup_select)

        self._startup_items = []
        loading_row = self.startup_tree.insert("", "end", values=("Loading...", "", ""))

        def finished(items):
            if generation != self._page_generation:
                return
            try:
                self.startup_tree.delete(loading_row)
                self._startup_items = items
                for i, item in enumerate(items):
                    self.startup_tree.insert("", "end", iid=str(i), values=(item["name"], item["location"], item["command"]))
            except tk.TclError:
                pass

        def failed(exc):
            if generation != self._page_generation:
                return
            try:
                self.startup_tree.delete(loading_row)
                self.startup_tree.insert("", "end", values=("Could not read startup items", str(exc), ""))
            except tk.TclError:
                pass

        self.run_background(get_startup_items, finished, on_error=failed)

    def on_startup_select(self, event):
        try:
            selected = self.startup_tree.selection()
            self.startup_remove_btn.config(state="normal" if selected else "disabled")
        except tk.TclError:
            pass

    def remove_selected_startup_item(self):
        selection = self.startup_tree.selection()
        if not selection:
            return
        try:
            item = self._startup_items[int(selection[0])]
        except (ValueError, IndexError):
            return

        confirmed = messagebox.askyesno(
            "Remove Startup Item",
            f"Remove '{item['name']}' from startup?\n\nLocation: {item['location']}\nCommand: {item['command']}"
        )
        if not confirmed:
            return

        success, error = remove_startup_item(item)
        if success:
            messagebox.showinfo("Removed", f"'{item['name']}' was removed from startup.")
            self.navigate_startup()
        else:
            messagebox.showerror("Could Not Remove", error or "Unknown error.")

    # =====================================================================
    # 9) Cleanup History
    # =====================================================================

    def navigate_history(self):
        self.set_page("history")
        header = self.create_header("Cleanup History")
        self.make_button(header, "Clear History", self.clear_history_action,
                         bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER).pack(side="right")

        history = load_history()
        list_frame = self.make_scrollable(self.main)

        if not history:
            tk.Label(list_frame, text="No cleanup history yet.", font=("Segoe UI", 11),
                     fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
            return

        for record in history:
            row = tk.Frame(list_frame, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
            row.pack(fill="x", pady=5, ipady=8)

            try:
                dt = datetime.fromisoformat(record.get("timestamp", ""))
                date_text = dt.strftime("%b %d, %Y at %I:%M %p")
            except ValueError:
                date_text = record.get("timestamp", "Unknown date")

            top = tk.Frame(row, bg=COLOR_CARD)
            top.pack(fill="x", padx=18, pady=(6, 2))
            tk.Label(top, text=date_text, font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_CARD).pack(side="left")
            tk.Label(top, text=f"{format_bytes(record.get('freed_bytes', 0))} moved to Recovery",
                     font=("Segoe UI", 11, "bold"), fg=COLOR_SUCCESS, bg=COLOR_CARD).pack(side="right")

            categories = ", ".join(record.get("categories", [])) or "Unknown"
            tk.Label(row, text=f"Cleaned: {categories}", font=("Segoe UI", 9), fg=COLOR_MUTED,
                     bg=COLOR_CARD, wraplength=800, justify="left").pack(anchor="w", padx=18)

            skipped = record.get("skipped_files", 0)
            if skipped:
                tk.Label(row, text=f"{skipped} file(s) skipped (too recent or other)",
                         font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD).pack(anchor="w", padx=18)

            skipped_permission = record.get("skipped_permission", 0)
            if skipped_permission:
                tk.Label(
                    row, text=f"{skipped_permission} file(s) skipped -- permission denied or in use",
                    font=("Segoe UI", 9), fg=COLOR_WARNING, bg=COLOR_CARD
                ).pack(anchor="w", padx=18)

            report_path = record.get("report_path")
            if report_path:
                self.make_button(
                    row, "View Report", lambda p=report_path: self.view_report_file(p),
                    bg="#334155", hover="#475569", font=("Segoe UI", 9, "bold"), padx=10, pady=5
                ).pack(anchor="w", padx=18, pady=(8, 0))

    def clear_history_action(self):
        confirmed = messagebox.askyesno("Clear History", "Permanently clear all cleanup history?")
        if not confirmed:
            return
        save_history([])
        self.navigate_history()

    # =====================================================================
    # 10) Recovery
    # =====================================================================

    RECOVERY_PAGE_SIZE = 100

    def navigate_recovery(self):
        self.set_page("recovery")
        page_generation = self._page_generation
        header = self.create_header("Recovery")

        items = load_reconciled_recovery()

        self.make_button(header, "Restore All", self.restore_all_recovery,
                         bg=COLOR_GREEN, hover=COLOR_GREEN_HOVER).pack(side="right", padx=(10, 0))
        self.make_button(header, "Permanently Delete All", self.delete_all_recovery,
                         bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER).pack(side="right")

        total = sum(x.get("size", 0) for x in items)
        unknown = sum(1 for x in items if not has_valid_original(x))
        tk.Label(self.main, text=f"{len(items)} recovered file(s)  •  {format_bytes(total)}",
                 font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_BG).pack(anchor="w", padx=35)
        info_text = ("Inspect files here before permanently deleting them. Emptying Recovery is what "
                     "actually reclaims disk space.")
        if unknown:
            info_text += (f"\n{unknown} file(s) have an unknown original location and can't be restored "
                          "-- they can only be deleted.")
        tk.Label(self.main, text=info_text, font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_BG,
                 wraplength=850, justify="left").pack(anchor="w", padx=35, pady=(5, 10))

        frame = self.make_scrollable(self.main)
        if not items:
            tk.Label(frame, text="Recovery is empty.", font=("Segoe UI", 12), fg="#d1d5db",
                     bg=COLOR_BG).pack(anchor="w", pady=20)
            return

        def render_from(start):
            if self._active_page != "recovery" or self._page_generation != page_generation:
                return
            end = min(start + self.RECOVERY_PAGE_SIZE, len(items))
            for item in items[start:end]:
                self.create_recovery_row(frame, item)

            if end < len(items):
                more = tk.Frame(frame, bg=COLOR_BG)
                more.pack(fill="x", pady=10)

                def load_more(e=end, m=more):
                    m.destroy()
                    render_from(e)

                self.make_button(
                    more, f"Show more ({len(items) - end} remaining)", load_more,
                    bg="#334155", hover="#475569", font=("Segoe UI", 9, "bold"), padx=14, pady=6
                ).pack()

        render_from(0)

    def create_recovery_row(self, parent, item):
        row = tk.Frame(parent, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        row.pack(fill="x", pady=5)
        row.columnconfigure(0, weight=1)
        row.columnconfigure(1, weight=0)

        info = tk.Frame(row, bg=COLOR_CARD)
        info.grid(row=0, column=0, sticky="w", padx=18, pady=10)
        tk.Label(info, text=item.get("name", "Unknown file"), font=("Segoe UI", 11, "bold"),
                 fg="white", bg=COLOR_CARD, wraplength=480, justify="left").pack(anchor="w")
        tk.Label(info, text=f"Original: {item.get('original_path', 'Unknown')}", font=("Segoe UI", 9),
                 fg=COLOR_MUTED, bg=COLOR_CARD, wraplength=480, justify="left").pack(anchor="w", pady=(3, 0))
        tk.Label(info, text=f"Size: {format_bytes(item.get('size', 0))}  •  Moved: {item.get('moved_at', 'Unknown')}",
                 font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD).pack(anchor="w", pady=(3, 0))

        buttons = tk.Frame(row, bg=COLOR_CARD)
        buttons.grid(row=0, column=1, sticky="e", padx=15, pady=10)

        restore_btn = self.make_button(
            buttons, "Restore", lambda i=item: self.restore_single_recovery(i),
            bg=COLOR_GREEN, hover=COLOR_GREEN_HOVER, font=("Segoe UI", 9, "bold"), padx=10, pady=5
        )
        restore_btn.config(width=9)
        restore_btn.pack(side="left", padx=(0, 8))
        if not has_valid_original(item):
            restore_btn.config(state="disabled", bg="#475569")

        delete_btn = self.make_button(
            buttons, "Permanently Delete", lambda i=item: self.delete_single_recovery(i),
            bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER, font=("Segoe UI", 9, "bold"), padx=10, pady=5
        )
        delete_btn.config(width=18)
        delete_btn.pack(side="left")

    def restore_single_recovery(self, item):
        path = item.get("original_path", "Unknown")
        if not has_valid_original(item):
            messagebox.showerror("Can't Restore", "The original location of this file is unknown.")
            return
        if not messagebox.askyesno("Restore File", f"Restore this file?\n\n{path}"):
            return
        ok, error = restore_recovery_item(item)
        if ok:
            messagebox.showinfo("Restored", f"Restored to:\n\n{path}")
            self.navigate_recovery()
        else:
            messagebox.showerror("Restore Failed", error or "Could not restore file.")

    def delete_single_recovery(self, item):
        label = item.get("name", "this file")
        if not messagebox.askyesno("Permanently Delete",
                                   f"Permanently delete this recovered file?\n\n{label}\n\nThis cannot be undone."):
            return
        ok, error = permanently_delete_recovery_item(item)
        if ok:
            self.navigate_recovery()
        else:
            messagebox.showerror("Delete Failed", error or "Could not delete file.")

    def restore_all_recovery(self):
        if self._recovery_busy:
            return
        items = load_reconciled_recovery()
        if not items:
            messagebox.showinfo("Recovery Empty", "There are no files to restore.")
            return
        restorable = [x for x in items if has_valid_original(x)]
        unknown = len(items) - len(restorable)
        if not restorable:
            messagebox.showinfo("Nothing To Restore",
                                "None of these files have a known original location, so none can be restored.")
            return
        extra = f"\n\n{unknown} file(s) with an unknown original location will be skipped." if unknown else ""
        if not messagebox.askyesno("Restore All", f"Restore {len(restorable)} recovered file(s)?{extra}"):
            return

        self._recovery_busy = True

        def finished(result):
            self._recovery_busy = False
            restored, failed = result
            messagebox.showinfo("Restore Complete", f"Restored: {restored}\nFailed: {failed}\nSkipped (unknown location): {unknown}")
            if self._active_page == "recovery":
                self.navigate_recovery()

        def reset():
            self._recovery_busy = False

        self.run_background(lambda: restore_recovery_items(restorable), finished,
                            on_error=self.make_error_handler(reset, "Restore Failed"))

    def delete_all_recovery(self):
        if self._recovery_busy:
            return
        items = load_reconciled_recovery()
        if not items:
            messagebox.showinfo("Recovery Empty", "There are no files to permanently delete.")
            return
        total = sum(x.get("size", 0) for x in items)
        if not messagebox.askyesno(
            "Permanently Delete All",
            f"Permanently delete all {len(items)} recovered file(s)?\n\nTotal: {format_bytes(total)}\n\n"
            "This cannot be undone."
        ):
            return

        self._recovery_busy = True

        def finished(result):
            self._recovery_busy = False
            deleted, failed = result
            messagebox.showinfo("Recovery Cleanup Complete",
                                f"Permanently deleted: {deleted}\nFailed: {failed}")
            if self._active_page == "recovery":
                self.navigate_recovery()

        def reset():
            self._recovery_busy = False

        self.run_background(lambda: permanently_delete_recovery_items(items), finished,
                            on_error=self.make_error_handler(reset, "Delete Failed"))

    # =====================================================================
    # 11) Settings
    # =====================================================================

    AGE_OPTIONS = [("15 minutes", 15), ("30 minutes", 30), ("1 hour", 60), ("6 hours", 360), ("24 hours", 1440)]
    REFRESH_OPTIONS = [("2 seconds", 2), ("5 seconds", 5), ("10 seconds", 10), ("30 seconds", 30)]
    LARGE_FILE_DEFAULT_OPTIONS = [("50 MB", 50), ("100 MB", 100), ("250 MB", 250), ("500 MB", 500), ("1 GB", 1024)]

    def navigate_settings(self):
        self.set_page("settings")
        self.create_header("Settings")

        form = tk.Frame(self.main, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        form.pack(fill="x", padx=35, pady=10, ipady=10)

        self.settings_age_var = tk.StringVar(
            value=self._label_for(self.AGE_OPTIONS, self.config["min_age_minutes"], "1 hour")
        )
        self.settings_refresh_var = tk.StringVar(
            value=self._label_for(self.REFRESH_OPTIONS, self.config["refresh_interval_sec"], "5 seconds")
        )
        self.settings_largefile_var = tk.StringVar(
            value=self._label_for(self.LARGE_FILE_DEFAULT_OPTIONS, self.config["large_file_min_mb"], "100 MB")
        )

        self.create_settings_row(
            form, "Minimum file age before cleanup",
            "Files newer than this are always skipped during cleanup, since they may still be in use.",
            self.settings_age_var, [lbl for lbl, _ in self.AGE_OPTIONS]
        )
        self.create_settings_row(
            form, "Dashboard refresh interval",
            "How often the live CPU/Memory graphs and metric cards update.",
            self.settings_refresh_var, [lbl for lbl, _ in self.REFRESH_OPTIONS]
        )
        self.create_settings_row(
            form, "Large File Finder default threshold",
            "The size threshold pre-selected when you open Large File Finder.",
            self.settings_largefile_var, [lbl for lbl, _ in self.LARGE_FILE_DEFAULT_OPTIONS]
        )

        footer = tk.Frame(form, bg=COLOR_CARD)
        footer.pack(fill="x", padx=25, pady=(15, 5))
        self.make_button(footer, "Save Settings", self.save_settings_action, bg=COLOR_GREEN,
                         hover=COLOR_GREEN_HOVER).pack(side="left")
        self.settings_status_label = tk.Label(footer, text="", font=("Segoe UI", 10, "italic"),
                                              fg=COLOR_SUCCESS, bg=COLOR_CARD)
        self.settings_status_label.pack(side="left", padx=15)

    def _label_for(self, options, value, fallback):
        for label, v in options:
            if v == value:
                return label
        return fallback

    def create_settings_row(self, parent, title, description, var, options):
        row = tk.Frame(parent, bg=COLOR_CARD)
        row.pack(fill="x", padx=25, pady=12)

        text_frame = tk.Frame(row, bg=COLOR_CARD)
        text_frame.pack(side="left", fill="x", expand=True)
        tk.Label(text_frame, text=title, font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_CARD).pack(anchor="w")
        tk.Label(text_frame, text=description, font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD,
                 wraplength=550, justify="left").pack(anchor="w")

        ttk.Combobox(row, textvariable=var, values=options, state="readonly", width=15).pack(side="right")

    def save_settings_action(self):
        age_map = dict(self.AGE_OPTIONS)
        refresh_map = dict(self.REFRESH_OPTIONS)
        largefile_map = dict(self.LARGE_FILE_DEFAULT_OPTIONS)

        self.config["min_age_minutes"] = age_map.get(self.settings_age_var.get(), 60)
        self.config["refresh_interval_sec"] = refresh_map.get(self.settings_refresh_var.get(), 5)
        self.config["large_file_min_mb"] = largefile_map.get(self.settings_largefile_var.get(), 100)

        save_config(self.config)

        self.settings_status_label.config(text="Settings saved.")
        self.root.after(2000, self._clear_settings_status)

    def _clear_settings_status(self):
        try:
            self.settings_status_label.config(text="")
        except tk.TclError:
            pass


# =====================================================================
# Start Application
# =====================================================================

if __name__ == "__main__":
    root = tk.Tk()
    app = PCOptimizer(root)
    root.mainloop()