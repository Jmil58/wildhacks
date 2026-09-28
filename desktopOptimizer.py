import tkinter as tk
from tkinter import ttk, messagebox
import psutil
import platform
import socket
import os
import sys
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


def load_config():
    ensure_config_dir()
    config = dict(DEFAULT_CONFIG)
    if os.path.isfile(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, dict):
                config.update(data)
        except (json.JSONDecodeError, OSError):
            pass
    return config


def save_config(config):
    ensure_config_dir()
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(config, f, indent=2)
        return True
    except OSError:
        return False


def load_history():
    ensure_config_dir()
    if os.path.isfile(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                data = json.load(f)
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError):
            pass
    return []


def save_history(history):
    ensure_config_dir()
    try:
        with open(HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
        return True
    except OSError:
        return False


def append_history_record(record, max_records=200):
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
    except (json.JSONDecodeError, OSError):
        return []

def save_recovery_index(items):
    ensure_config_dir()
    try:
        with RECOVERY_LOCK:
            with open(RECOVERY_INDEX_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, indent=2)
        return True
    except OSError:
        return False

def move_file_to_recovery(path):
    if not os.path.isfile(path):
        return False, None, "File does not exist."
    if not ensure_recovery_dir():
        return False, None, "Could not create the Recovery folder."
    try:
        size = os.path.getsize(path)
        original_path = os.path.abspath(path)
        recovery_id = str(uuid.uuid4())
        recovery_path = os.path.join(RECOVERY_DIR, recovery_id + "_" + os.path.basename(original_path))
        shutil.move(original_path, recovery_path)
        record = {
            "id": recovery_id, "name": os.path.basename(original_path),
            "original_path": original_path, "recovery_path": recovery_path,
            "size": size, "moved_at": datetime.now().isoformat(timespec="seconds"),
        }
        with RECOVERY_LOCK:
            items = load_recovery_index()
            items.insert(0, record)
            if not save_recovery_index(items):
                try:
                    os.makedirs(os.path.dirname(original_path), exist_ok=True)
                    shutil.move(recovery_path, original_path)
                except OSError:
                    pass
                return False, None, "Could not save the Recovery index."
        return True, record, None
    except PermissionError as e:
        return False, None, f"Permission denied: {e}"
    except OSError as e:
        return False, None, str(e)

def reconcile_recovery_index(items):
    """Make sure every physical file in Recovery is represented in the index."""
    try:
        os.makedirs(RECOVERY_DIR, exist_ok=True)
        known_paths = {os.path.abspath(x.get("recovery_path")) for x in items if x.get("recovery_path")}
        changed = False
        for entry in os.scandir(RECOVERY_DIR):
            if not entry.is_file():
                continue
            path = os.path.abspath(entry.path)
            if path in known_paths:
                continue
            try:
                size = entry.stat().st_size
                moved_at = datetime.fromtimestamp(entry.stat().st_mtime).isoformat(timespec="seconds")
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

def remove_recovery_record(record_id):
    with RECOVERY_LOCK:
        items = [x for x in load_recovery_index() if x.get("id") != record_id]
        return save_recovery_index(items)

def restore_recovery_item(record):
    src = record.get("recovery_path")
    dst = record.get("original_path")
    if not src or not os.path.isfile(src):
        return False, "The recovered file could not be found."
    if not dst:
        return False, "The original path is missing."
    if os.path.exists(dst):
        return False, "A file already exists at the original location:\n\n" + dst
    try:
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        shutil.move(src, dst)
        remove_recovery_record(record.get("id"))
        return True, None
    except OSError as e:
        return False, str(e)

def permanently_delete_recovery_item(record):
    path = record.get("recovery_path")
    try:
        if path and os.path.isfile(path):
            os.remove(path)
        remove_recovery_record(record.get("id"))
        return True, None
    except OSError as e:
        return False, str(e)

# =====================================================================
# System metrics
# =====================================================================

def get_root_path():
    return os.path.abspath(os.sep)


def get_temp_dir():
    return tempfile.gettempdir()


def get_cpu_usage():
    # interval=None returns the delta since the last call instead of
    # blocking the calling thread for `interval` seconds.
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
# Drives
# =====================================================================

def get_all_drives():
    """Usage info for every mounted/mapped drive on the system.
    """
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
    system = platform.system()
    if system == "Windows":
        candidates = [
            os.path.join(mountpoint, "Temp"),
            os.path.join(mountpoint, "TEMP"),
            os.path.join(mountpoint, "tmp"),
            os.path.join(mountpoint, "Windows", "Temp"),
            os.path.join(mountpoint, "$RECYCLE.BIN"),
            os.path.join(mountpoint, "RECYCLER"),
        ]
    else:
        candidates = [
            os.path.join(mountpoint, "tmp"),
            os.path.join(mountpoint, "temp"),
            os.path.join(mountpoint, ".Trash-1000"),
            os.path.join(mountpoint, ".Trashes"),
        ]
    return [c for c in candidates if os.path.isdir(c)]


def get_drive_junk_paths_all():
    paths = []
    for d in get_all_drives():
        if d.get("error"):
            continue
        for p in get_junk_folders_for_drive(d["mountpoint"]):
            if os.path.basename(p).upper() not in ("$RECYCLE.BIN", "RECYCLER"):
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
# Cleanup categories (used by the Cleanup page's scan-then-clean flow)
# =====================================================================

CLEANUP_CATEGORIES = [
    {
        "id": "system_temp", "label": "System Temp Files",
        "description": "Temporary files created by the OS and apps.",
        "paths_func": lambda: [get_temp_dir()],
    },
    {
        "id": "drive_junk", "label": "Other Drives' Temp Folders",
        "description": "Temp folders found on other local/mapped drives.",
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
    """Shape shared by clean_folder / clean_category / clean_drive_junk,
    so the GUI layer only ever deals with one consistent dict."""
    return {
        "deleted_bytes": 0,
        "skipped_recent": 0,
        "skipped_permission": 0,
        "deleted_paths": [],      # list of (path, size) actually removed
        "permission_paths": [],   # list of paths that need admin rights
    }


def merge_clean_result(into, other):
    into["deleted_bytes"] += other["deleted_bytes"]
    into["skipped_recent"] += other["skipped_recent"]
    into["skipped_permission"] += other["skipped_permission"]
    into["deleted_paths"].extend(other["deleted_paths"])
    into["permission_paths"].extend(other["permission_paths"])
    return into


def clean_folder(folder, min_age_seconds=3600):
    """Move cleanable files into PC Optimizer Recovery instead of deleting them."""
    result = new_clean_result()
    if not folder or not os.path.isdir(folder):
        return result
    now = time.time()
    for root, dirs, files in os.walk(folder, topdown=False):
        for file in files:
            path = os.path.join(root, file)
            try:
                stat = os.stat(path)
                if now - stat.st_mtime < min_age_seconds:
                    result["skipped_recent"] += 1
                    continue
                size = stat.st_size
                success, record, error = move_file_to_recovery(path)
                if success:
                    result["deleted_bytes"] += size
                    result["deleted_paths"].append((path, size))
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
    return result

def scan_folder_cleanable(folder, min_age_seconds):
    """Compute how much a clean_folder() call WOULD free, without deleting."""
    if not folder or not os.path.isdir(folder):
        return 0, 0

    total = 0
    count = 0
    now = time.time()

    for root, dirs, files in os.walk(folder):
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
# Cleanup reports (the "what actually got deleted" .txt log)
# =====================================================================

REPORTS_DIR = os.path.join(CONFIG_DIR, "reports")


def write_cleanup_report(deleted_paths, permission_paths, skipped_recent, freed_bytes, categories):
    """Write a plain-text report listing every file that was deleted
    (and every one that was blocked by permissions) in one cleanup run.
    Returns the report's file path, or None if it couldn't be written.
    """
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
        f"Total space freed: {format_bytes(freed_bytes)}",
        f"Files deleted: {len(deleted_paths)}",
        f"Files skipped (in use / too recent): {skipped_recent}",
        f"Files skipped (need administrator rights): {len(permission_paths)}",
        "",
        "=" * 70,
        f"DELETED FILES ({len(deleted_paths)})",
        "=" * 70,
    ]
    for path, size in deleted_paths:
        lines.append(f"{format_bytes(size):>10}  {path}")

    if permission_paths:
        lines.append("")
        lines.append("=" * 70)
        lines.append(f"SKIPPED -- NEED ADMINISTRATOR RIGHTS ({len(permission_paths)})")
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
    """Size of each immediate child of `path` (folders sized recursively)."""
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

def find_large_files(root_path, min_size_bytes, max_results=100):
    results = []
    for dirpath, dirnames, filenames in os.walk(root_path, onerror=lambda e: None):
        for f in filenames:
            path = os.path.join(dirpath, f)
            try:
                size = os.path.getsize(path)
                if size >= min_size_bytes:
                    results.append((path, size))
            except OSError:
                continue
    results.sort(key=lambda x: x[1], reverse=True)
    return results[:max_results]


# =====================================================================
# Startup apps (Windows only -- guarded elsewhere on other OSes)
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
        except FileNotFoundError:
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
    """Best-effort removal: delete the registry value, or the shortcut file."""
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
# Process list helper (keeps a persistent Process cache so cpu_percent
# deltas are meaningful across refreshes)
# =====================================================================

def refresh_process_rows(proc_cache, limit=60):
    current_pids = set()

    for p in psutil.process_iter(["pid"]):
        pid = p.info["pid"]
        current_pids.add(pid)
        if pid not in proc_cache:
            try:
                p.cpu_percent(None)  # prime; first read is always 0.0
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

        self._drives_request_id = 0

    # Thread-safe queue used to send completed background work
    # back to Tkinter's main thread.
        self._gui_queue = queue.Queue()

# Start checking the queue from the Tkinter thread.
        self.root.after(50, self.process_gui_queue)

        self._setup_ttk_style()

        # Safety net: any exception raised inside a Tk callback (button
        # clicks, after() timers, etc.) normally either prints a bare
        # traceback to the console or silently kills a scheduled repeat
        # (e.g. the dashboard/process auto-refresh loop just stops).
        # Both look like "the app crashed" to a user. Route these
        # through a visible, recoverable error dialog instead.
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
            pass  # if even the error dialog can't show, don't crash on that too

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
    # Small UI helpers
    # -------------------------

    def add_hover(self, widget, normal_bg, hover_bg):
        widget.bind("<Enter>", lambda e: widget.config(bg=hover_bg))
        widget.bind("<Leave>", lambda e: widget.config(bg=normal_bg))

    def make_button(self, parent, text, command, bg=COLOR_ACCENT, hover=COLOR_ACCENT_HOVER,
                     font=("Segoe UI", 10, "bold"), padx=18, pady=10):
        btn = tk.Button(
            parent, text=text, command=command, font=font, fg="white",
            bg=bg, activebackground=hover, bd=0, padx=padx, pady=pady,
            cursor="hand2"
        )
        self.add_hover(btn, bg, hover)
        return btn

    def start_loading(self, label_widget, base_text="Scanning"):
        self._loading_active = True
        self._loading_dots = 0

        def step():
            if not getattr(self, "_loading_active", False):
                return
            try:
                dots = "." * (self._loading_dots % 4)
                label_widget.config(text=f"{base_text}{dots}")
            except tk.TclError:
                return  # widget was destroyed (page changed)
            self._loading_dots += 1
            self.root.after(400, step)

        step()

    def stop_loading(self):
        self._loading_active = False

    def show_text_viewer(self, title, content):
        """A simple in-app, read-only window for showing report text --
        no dependency on the OS having a text editor associated, unlike
        os.startfile()/subprocess-based "open externally" approaches."""
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

    def process_gui_queue(self):
        """Process a small batch of completed jobs so navigation stays responsive."""
        max_callbacks = 4
        processed = 0

        while processed < max_callbacks:
            try:
                item = self._gui_queue.get_nowait()
            except queue.Empty:
                break

            # Older versions of the queue stored a page-generation value.
            # Keep accepting that format, but do not silently discard the
            # result here: some jobs (cleanup, indexing, etc.) must finish
            # even if the user navigates to another page.
            if len(item) == 3:
                callback, result, _job_generation = item
            else:
                callback, result = item

            try:
                callback(result)
            except tk.TclError:
                pass
            except Exception as e:
                print("GUI callback error:", e)
            processed += 1

        try:
            self.root.after(50, self.process_gui_queue)
        except tk.TclError:
            pass


    def run_background(self, work_fn, on_done):
        """
        Run work_fn() (no args) in a background thread and deliver its
        return value to on_done(result) safely on the Tk main thread.

        work_fn must NEVER touch any Tkinter object -- it should only
        compute and return plain data. on_done is what's allowed to
        touch widgets, and it always runs via process_gui_queue() on
        the main thread, never directly from the worker thread.
        """
        job_generation = self._page_generation

        def worker():
            try:
                result = work_fn()
            except Exception as e:
                print("Background worker error:", e)
                result = e
            self._gui_queue.put((on_done, result, job_generation))

        threading.Thread(target=worker, daemon=True).start()

    def fetch_drives_async(self, callback):
        """
        Fetch drive information in a background thread.

        The worker does not call Tkinter. When the operation finishes,
        the result is placed into _gui_queue. process_gui_queue()
        delivers it from Tkinter's main thread.
        """

        job_generation = self._page_generation

        def worker():
            try:
                drives = get_all_drives()
            except Exception as e:
                print("Drive detection error:", e)
                drives = []

            self._gui_queue.put(
                (callback, drives, job_generation)
            )

        threading.Thread(
            target=worker,
            daemon=True
        ).start()
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
        for widget in self.main.winfo_children():
            widget.destroy()

    def set_page(self, key):
        # Every navigation creates a new page generation.
        #
        # Background tasks capture this number when they start.
        # If the number changes before they finish, we know their
        # page was destroyed and their result must be ignored.
            self._page_generation += 1

            self._active_page = key

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
        """Returns (outer_container_already_packed, inner_frame_to_fill)."""
        container = tk.Frame(parent, bg=COLOR_BG)
        container.pack(fill="both", expand=True, padx=35, pady=(0, 20))

        canvas = tk.Canvas(container, bg=COLOR_BG, highlightthickness=0)
        scrollbar = tk.Scrollbar(container, orient="vertical", command=canvas.yview)
        inner = tk.Frame(canvas, bg=COLOR_BG)

        inner.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        return inner

    # =====================================================================
    # 1) Dashboard (with live CPU / RAM graphs)
    # =====================================================================

    def navigate_dashboard(self):
        self.set_page("dashboard")
        self.create_header("System Dashboard")

        # Metrics are filled in by the first tick_dashboard() call below,
        # which fetches them on a background thread. We don't call
        # get_cpu_usage()/get_memory_usage()/get_disk_usage() here on the
        # main thread, since disk_usage() in particular can occasionally
        # stall (a slow/removable/network drive) and freeze the whole UI
        # right as the page opens.
        cards = tk.Frame(self.main, bg=COLOR_BG)
        cards.pack(fill="x", padx=35)

        self.dash_labels = {}
        self.dash_labels["cpu"] = self.create_metric_card(cards, "CPU", "…", 0)
        self.dash_labels["memory"] = self.create_metric_card(cards, "Memory", "…", 1)
        self.dash_labels["disk"] = self.create_metric_card(cards, "Disk (C:)", "…", 2)
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
        # Self-terminating: once the user navigates away, _active_page
        # changes and this loop simply stops rescheduling itself.
        if self._active_page != "dashboard":
            return

        # Don't stack up a second fetch if the previous one hasn't
        # returned yet (e.g. disk_usage() stalling on a slow/removable/
        # network drive). We still reschedule below so the loop keeps
        # trying on the normal cadence once it's free again.
        if getattr(self, "_dashboard_tick_pending", False):
            self.root.after(int(self.config["refresh_interval_sec"] * 1000), self.tick_dashboard)
            return
        self._dashboard_tick_pending = True

        def collect_metrics():
            # Runs on a background thread. psutil calls here -- especially
            # disk_usage() -- can occasionally block for a while (a sleepy
            # USB/optical drive, a network share that's gone away, etc.).
            # Never touch Tk widgets in here.
            cpu = get_cpu_usage()
            memory = get_memory_usage()
            disk = get_disk_usage()
            return (cpu, memory, disk)

        def apply_metrics(result):
            self._dashboard_tick_pending = False
            if self._active_page != "dashboard":
                return  # navigated away while the fetch was in flight

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
                pass  # dashboard widgets were torn down mid-update; harmless

            self.root.after(int(self.config["refresh_interval_sec"] * 1000), self.tick_dashboard)

        self.run_background(collect_metrics, apply_metrics)

    # =====================================================================
    # 2) Diagnostics
    # =====================================================================

    def navigate_diagnostics(self):
        self.set_page("diagnostics")
        self.create_header("System Diagnostics")

        frame = tk.Frame(self.main, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
        frame.pack(fill="both", expand=True, padx=35, pady=10)

        # Every one of these checks (disk_usage especially, and the
        # network socket for connectivity) can occasionally stall for a
        # noticeable moment, so none of them run on the main thread --
        # all four are fetched together in one background call and the
        # rows are filled in once that's done.
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
            if self._active_page != "diagnostics":
                return
            try:
                for (symbol_label, text_label), name, ok in zip(rows, row_names, results):
                    symbol_label.config(text="✓" if ok else "⚠", fg=COLOR_SUCCESS if ok else COLOR_WARNING)
                    text_label.config(text=name)
            except tk.TclError:
                pass  # diagnostics page was closed mid-check; harmless

        self.run_background(collect_checks, finished)

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
        # Runs on a background thread via run_background -- never call
        # this directly from the main thread, the socket connect below
        # can block for up to 2 seconds.
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

        # Tag this specific navigate_drives() call with a request id, and
        # disable Refresh until it resolves. If the user re-opens this
        # page (or hits Refresh again) before this fetch returns, the
        # widgets above get destroyed by the NEXT navigate_drives() call.
        # Without this guard, this call's stale callback would still try
        # to draw into a destroyed list_frame and throw an uncaught Tk
        # error. Checking the request id (not just the page name) lets
        # us tell "still on Drives" apart from "still on Drives, but a
        # newer instance of it".
        self._drives_request_id += 1
        my_request_id = self._drives_request_id
        try:
            refresh_btn.config(state="disabled")
        except tk.TclError:
            pass

        def on_drives(drives):
            self.stop_loading()
            if self._active_page != "drives" or my_request_id != self._drives_request_id:
                return  # navigated away, or a newer Drives fetch superseded this one

            try:
                refresh_btn.config(state="normal")
            except tk.TclError:
                pass  # button belongs to a page that's already gone; harmless

            try:
                for widget in list_frame.winfo_children():
                    widget.destroy()
            except tk.TclError:
                return  # list_frame itself is already gone; nothing to draw into

            if not drives:
                tk.Label(list_frame, text="No drives could be detected.", font=("Segoe UI", 12),
                          fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
                return

            for drive in drives:
                try:
                    self.create_drive_card(list_frame, drive)
                except Exception as e:
                    # One malformed/unusual drive entry shouldn't take
                    # down the whole page -- show it as an error row and
                    # keep rendering the rest.
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
        if getattr(self, "_drive_clean_running", False):
            return

        junk_folders = get_junk_folders_for_drive(mountpoint)
        folder_list = "\n".join(junk_folders) if junk_folders else "(none found)"
        confirmed = messagebox.askyesno(
            "Confirm Cleanup",
            f"This will move files older than 1 hour into PC Optimizer Recovery from:\n\n{folder_list}\n\nContinue?"
        )
        if not confirmed:
            return

        self._drive_clean_running = True
        if button is not None:
            button.config(state="disabled")

        def finished(result):
            self._drive_clean_running = False
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
            permission_line = (
                f"\nSkipped (need administrator rights): {result['skipped_permission']}"
                if result["skipped_permission"] else ""
            )
            messagebox.showinfo(
                "Cleanup Complete",
                f"Cleanup finished for {mountpoint}.\n\n"
                f"Moved to Recovery: {format_bytes(result['deleted_bytes'])}\n"
                f"Files skipped (in use/recent): {result['skipped_recent']}"
                f"{permission_line}\n"
                f"Folders scanned: {len(result['folders'])}"
            )
            if report_path and messagebox.askyesno(
                "View Report?", "View the full list of deleted files now?"
            ):
                self.view_report_file(report_path)
            if self._active_page == "drives":
                self.navigate_drives()

        self.run_background(lambda: clean_drive_junk(mountpoint), finished)

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
        if getattr(self, "_storage_running", False):
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

        self.run_background(do_analysis, finished)

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
    # 5) Cleanup (categories with checkboxes, scan-before-cleanup)
    # =====================================================================

    def navigate_cleanup(self):
        self.set_page("cleanup")
        self.create_header("Cleanup")

        intro = tk.Frame(self.main, bg=COLOR_BG)
        intro.pack(fill="x", padx=35, pady=(0, 10))
        tk.Label(
            intro,
            text=("Scan first to see exactly what's cleanable and how much space it would free, "
                  "then pick which categories to clean. Files modified in the last "
                  f"{self.config['min_age_minutes']} minutes are always skipped."),
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
        if getattr(self, "_cleanup_running", False):
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

        self.run_background(do_scan, finished)

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
        if getattr(self, "_cleanup_running", False):
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
            f"{labels}\n\nEstimated space freed: {format_bytes(total_est)}\n\nContinue?"
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
                f"\nFiles skipped (need administrator rights): {combined['skipped_permission']}"
                if combined["skipped_permission"] else ""
            )
            messagebox.showinfo(
                "Cleanup Complete",
                f"Cleanup finished.\n\nMoved to Recovery: {format_bytes(combined['deleted_bytes'])}\n"
                f"Files skipped (in use/recent): {combined['skipped_recent']}"
                f"{permission_line}\n\nCategories cleaned:\n" +
                "\n".join(f"• {l}" for l in labels_done)
            )
            if report_path and messagebox.askyesno(
                "View Report?", "View the full list of deleted files now?"
            ):
                self.view_report_file(report_path)
            if self._active_page == "cleanup":
                self.navigate_cleanup()

        self.run_background(do_clean, finished)

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
        self.largefile_find_btn.pack(side="left", padx=15)

        self.largefile_status_label = tk.Label(controls, text="", font=("Segoe UI", 10, "italic"),
                                                 fg=COLOR_MUTED, bg=COLOR_BG)
        self.largefile_status_label.pack(side="left")
        self.start_loading(self.largefile_status_label, "Loading drives")

        self.largefile_results_frame = self.make_scrollable(self.main)
        tk.Label(
            self.largefile_results_frame,
            text="Pick a drive and size threshold, then click Find Large Files. "
                 "Scanning a full drive can take a while.",
            font=("Segoe UI", 11), fg="#d1d5db", bg=COLOR_BG, wraplength=700, justify="left"
        ).pack(anchor="w", pady=20)

        self._largefile_request_id = getattr(self, "_largefile_request_id", 0) + 1
        my_request_id = self._largefile_request_id

        def on_drives(drives):
            if self._active_page != "largefiles" or my_request_id != self._largefile_request_id:
                return  # navigated away, or a newer Large File Finder load superseded this one

            self.stop_loading()
            try:
                self.largefile_status_label.config(text="")
                mountpoints = [d["mountpoint"] for d in drives if not d.get("error")] or [get_root_path()]
                drive_combo.config(values=mountpoints, state="readonly")
                self.largefile_drive_var.set(mountpoints[0])
                self.largefile_find_btn.config(state="normal")
            except tk.TclError:
                return  # widgets from this page instance are already gone

        self.fetch_drives_async(on_drives)

    def run_large_file_scan(self):
        if getattr(self, "_largefile_running", False):
            return

        mount = self.largefile_drive_var.get()
        size_label = self.largefile_size_var.get()
        min_mb = dict(self.LARGE_FILE_SIZE_OPTIONS).get(size_label, 100)
        min_bytes = min_mb * 1024 * 1024

        self._largefile_running = True
        self.largefile_find_btn.config(state="disabled")
        self.start_loading(self.largefile_status_label, "Scanning")

        def finished(results):
            self._largefile_running = False
            self.stop_loading()
            try:
                self.largefile_find_btn.config(state="normal")
                self.largefile_status_label.config(text="")
            except tk.TclError:
                pass
            if self._active_page == "largefiles":
                self.render_large_file_results(results)

        self.run_background(lambda: find_large_files(mount, min_bytes, max_results=100), finished)

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

            del_btn = self.make_button(
                row, "Delete", None, bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER,
                font=("Segoe UI", 9, "bold"), padx=10, pady=4
            )
            del_btn.config(command=lambda p=path, r=row: self.delete_large_file(p, r))
            del_btn.pack(side="right", padx=15)

    def delete_large_file(self, path, row_widget):
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
        selected = self.process_tree.selection()
        self.process_end_btn.config(state="normal" if selected else "disabled")

    def tick_processes(self):
        if self._active_page != "processes":
            return

        if getattr(self, "_processes_tick_pending", False):
            self.root.after(2000, self.tick_processes)
            return
        self._processes_tick_pending = True

        def collect_rows():
            # Background thread: only touches psutil, never Tk.
            return refresh_process_rows(self._proc_cache)

        def apply_rows(rows):
            self._processes_tick_pending = False
            if self._active_page != "processes":
                return

            try:
                selected_pid = None
                selection = self.process_tree.selection()
                if selection:
                    selected_pid = selection[0]

                for item in self.process_tree.get_children():
                    self.process_tree.delete(item)

                for pid, name, cpu, mem in rows:
                    self.process_tree.insert("", "end", iid=str(pid), values=(name, pid, f"{cpu:.1f}", f"{mem:.1f}"))

                if selected_pid and self.process_tree.exists(selected_pid):
                    self.process_tree.selection_set(selected_pid)
            except tk.TclError:
                pass  # process viewer widgets were torn down mid-update; harmless

            self.root.after(2000, self.tick_processes)

        self.run_background(collect_rows, apply_rows)

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
            if self._active_page != "startup":
                return
            try:
                self.startup_tree.delete(loading_row)
                self._startup_items = items
                for i, item in enumerate(items):
                    self.startup_tree.insert("", "end", iid=str(i), values=(item["name"], item["location"], item["command"]))
            except tk.TclError:
                pass  # startup page was closed mid-load; harmless

        self.run_background(get_startup_items, finished)

    def on_startup_select(self, event):
        selected = self.startup_tree.selection()
        self.startup_remove_btn.config(state="normal" if selected else "disabled")

    def remove_selected_startup_item(self):
        selection = self.startup_tree.selection()
        if not selection:
            return
        item = self._startup_items[int(selection[0])]

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
            tk.Label(top, text=format_bytes(record.get("freed_bytes", 0)), font=("Segoe UI", 11, "bold"),
                      fg=COLOR_SUCCESS, bg=COLOR_CARD).pack(side="right")

            categories = ", ".join(record.get("categories", [])) or "Unknown"
            tk.Label(row, text=f"Cleaned: {categories}", font=("Segoe UI", 9), fg=COLOR_MUTED,
                      bg=COLOR_CARD, wraplength=800, justify="left").pack(anchor="w", padx=18)

            skipped = record.get("skipped_files", 0)
            if skipped:
                tk.Label(row, text=f"{skipped} file(s) skipped (in use or too recent)",
                          font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD).pack(anchor="w", padx=18)

            skipped_permission = record.get("skipped_permission", 0)
            if skipped_permission:
                tk.Label(
                    row, text=f"{skipped_permission} file(s) skipped -- need administrator rights",
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

    def navigate_recovery(self):
        self.set_page("recovery")
        page_generation = self._page_generation
        header = self.create_header("Recovery")

        items = reconcile_recovery_index(load_recovery_index())
        valid = [x for x in items if x.get("recovery_path") and os.path.isfile(x.get("recovery_path"))]
        if len(valid) != len(items):
            save_recovery_index(valid)
        items = valid

        self.make_button(header, "Restore All", self.restore_all_recovery, bg=COLOR_GREEN, hover=COLOR_GREEN_HOVER).pack(side="right", padx=(10, 0))
        self.make_button(header, "Permanently Delete All", self.delete_all_recovery, bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER).pack(side="right")

        total = sum(x.get("size", 0) for x in items)
        tk.Label(self.main, text=f"{len(items)} recovered file(s)  •  {format_bytes(total)}",
                 font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_BG).pack(anchor="w", padx=35)
        tk.Label(self.main, text="Inspect files here before permanently deleting them. You can restore any file to its original location.",
                 font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_BG, wraplength=850, justify="left").pack(anchor="w", padx=35, pady=(5, 10))

        frame = self.make_scrollable(self.main)
        if not items:
            tk.Label(frame, text="Recovery is empty.", font=("Segoe UI", 12), fg="#d1d5db", bg=COLOR_BG).pack(anchor="w", pady=20)
            return

        def render_batch(index=0):
            # If the user navigated away, stop creating old-page widgets.
            # The files themselves remain untouched in Recovery.
            if self._active_page != "recovery" or self._page_generation != page_generation:
                return

            batch_size = 20
            end_index = min(index + batch_size, len(items))

            for item in items[index:end_index]:
                row = tk.Frame(frame, bg=COLOR_CARD, highlightbackground=COLOR_BORDER, highlightthickness=1)
                row.pack(fill="x", pady=5, ipady=8)
                info = tk.Frame(row, bg=COLOR_CARD)
                info.pack(side="left", fill="x", expand=True, padx=18, pady=7)
                tk.Label(info, text=item.get("name", "Unknown file"), font=("Segoe UI", 11, "bold"), fg="white", bg=COLOR_CARD).pack(anchor="w")
                tk.Label(info, text=f"Original: {item.get('original_path', 'Unknown')}", font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD, wraplength=560, justify="left").pack(anchor="w", pady=(3, 0))
                tk.Label(info, text=f"Size: {format_bytes(item.get('size', 0))}  •  Moved: {item.get('moved_at', 'Unknown')}", font=("Segoe UI", 9), fg=COLOR_MUTED, bg=COLOR_CARD).pack(anchor="w", pady=(3, 0))
                buttons = tk.Frame(row, bg=COLOR_CARD)
                buttons.pack(side="right", padx=15)
                self.make_button(buttons, "Restore", lambda i=item: self.restore_single_recovery(i), bg=COLOR_GREEN, hover=COLOR_GREEN_HOVER, font=("Segoe UI", 9, "bold"), padx=10, pady=5).pack(side="left", padx=(0, 8))
                self.make_button(buttons, "Permanently Delete", lambda i=item: self.delete_single_recovery(i), bg=COLOR_DANGER, hover=COLOR_DANGER_HOVER, font=("Segoe UI", 9, "bold"), padx=10, pady=5).pack(side="left")

            if end_index < len(items):
                self.root.after(1, lambda n=end_index: render_batch(n))

        render_batch()

    def restore_single_recovery(self, item):
        path = item.get("original_path", "Unknown")
        if not messagebox.askyesno("Restore File", f"Restore this file?\n\n{path}"):
            return
        ok, error = restore_recovery_item(item)
        if ok:
            messagebox.showinfo("Restored", f"Restored to:\n\n{path}")
            self.navigate_recovery()
        else:
            messagebox.showerror("Restore Failed", error or "Could not restore file.")

    def delete_single_recovery(self, item):
        path = item.get("original_path", "Unknown")
        if not messagebox.askyesno("Permanently Delete", f"Permanently delete this recovered file?\n\n{path}\n\nThis cannot be undone."):
            return
        ok, error = permanently_delete_recovery_item(item)
        if ok:
            self.navigate_recovery()
        else:
            messagebox.showerror("Delete Failed", error or "Could not delete file.")

    def restore_all_recovery(self):
        items = load_recovery_index()
        if not items:
            messagebox.showinfo("Recovery Empty", "There are no files to restore.")
            return
        if not messagebox.askyesno("Restore All", f"Restore all {len(items)} recovered file(s)?"):
            return
        restored = 0
        failed = 0
        for item in list(items):
            ok, _ = restore_recovery_item(item)
            if ok:
                restored += 1
            else:
                failed += 1
        messagebox.showinfo("Restore Complete", f"Restored: {restored}\nFailed: {failed}")
        self.navigate_recovery()

    def delete_all_recovery(self):
        items = load_recovery_index()
        if not items:
            messagebox.showinfo("Recovery Empty", "There are no files to permanently delete.")
            return
        total = sum(x.get("size", 0) for x in items)
        if not messagebox.askyesno("Permanently Delete All", f"Permanently delete all {len(items)} recovered file(s)?\n\nTotal: {format_bytes(total)}\n\nThis cannot be undone."):
            return
        deleted = 0
        failed = 0
        for item in list(items):
            ok, _ = permanently_delete_recovery_item(item)
            if ok:
                deleted += 1
            else:
                failed += 1
        messagebox.showinfo("Recovery Cleanup Complete", f"Permanently deleted: {deleted}\nFailed: {failed}")
        self.navigate_recovery()

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
        self.root.after(2000, lambda: self._clear_settings_status())

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