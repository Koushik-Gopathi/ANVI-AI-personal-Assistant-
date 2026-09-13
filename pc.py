"""PC control tools for ANVI (Windows)."""

import ctypes
import difflib
import json
import os
import re
import shutil
import subprocess
import time
import webbrowser
from ctypes import wintypes
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

from net import BROWSER_UA, http

HOME = Path.home()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# spoken name -> launch target (exe on PATH/App Paths, or a URI scheme)
APP_ALIASES = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe", "paint": "mspaint.exe",
    "command prompt": "cmd.exe", "cmd": "cmd.exe", "terminal": "wt.exe", "powershell": "powershell.exe",
    "task manager": "taskmgr.exe", "file explorer": "explorer.exe", "explorer": "explorer.exe",
    "files": "explorer.exe", "my files": "explorer.exe", "control panel": "control.exe",
    "settings": "ms-settings:", "bluetooth settings": "ms-settings:bluetooth", "wifi settings": "ms-settings:network-wifi",
    "display settings": "ms-settings:display", "sound settings": "ms-settings:sound",
    "camera": "microsoft.windows.camera:", "store": "ms-windows-store:", "microsoft store": "ms-windows-store:",
    "clock": "ms-clock:", "alarms": "ms-clock:", "photos": "ms-photos:", "snipping tool": "ms-screenclip:",
    "edge": "msedge", "microsoft edge": "msedge", "chrome": "chrome", "google chrome": "chrome",
    "firefox": "firefox", "vs code": "code", "vscode": "code", "visual studio code": "code",
    "word": "winword", "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook",
}

# spoken name -> process image name, for closing apps
PROCESS_ALIASES = {
    "chrome": "chrome.exe", "google chrome": "chrome.exe", "edge": "msedge.exe", "microsoft edge": "msedge.exe",
    "firefox": "firefox.exe", "vs code": "Code.exe", "vscode": "Code.exe", "visual studio code": "Code.exe",
    "word": "WINWORD.EXE", "excel": "EXCEL.EXE", "powerpoint": "POWERPNT.EXE", "outlook": "OUTLOOK.EXE",
    "notepad": "Notepad.exe", "calculator": "CalculatorApp.exe", "spotify": "Spotify.exe",
    "whatsapp": "WhatsApp.exe", "telegram": "Telegram.exe", "discord": "Discord.exe", "vlc": "vlc.exe",
    "paint": "mspaint.exe", "task manager": "Taskmgr.exe", "teams": "ms-teams.exe", "zoom": "Zoom.exe",
}
PROTECTED_PROCESSES = {"explorer.exe", "csrss.exe", "winlogon.exe", "lsass.exe", "svchost.exe", "services.exe",
                       "system", "smss.exe", "wininit.exe", "dwm.exe", "python.exe", "pythonw.exe"}

SKIP_DIRS = {"node_modules", ".git", "venv", ".venv", "__pycache__", "appdata", "site-packages", "$recycle.bin"}


def _clean_name(name: str) -> str:
    name = re.sub(r"\b(the|app|application|program|please)\b", " ", name.lower())
    return re.sub(r"\s+", " ", name).strip()


# ---------------------------------------------------------------------------
# Apps
# ---------------------------------------------------------------------------
_start_apps: list[dict] | None = None


def _installed_apps() -> list[dict]:
    """Every app in the Start menu (desktop + Store apps), cached."""
    global _start_apps
    if _start_apps is None:
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
                capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW, encoding="utf-8",
            ).stdout
            data = json.loads(out or "[]")
            _start_apps = data if isinstance(data, list) else [data]
        except Exception as e:  # noqa: BLE001
            print("Get-StartApps failed:", e)
            _start_apps = []
    return _start_apps


START_NAMES = {"vs code": "visual studio code", "vscode": "visual studio code", "code": "visual studio code",
               "edge": "microsoft edge", "chrome": "google chrome", "store": "microsoft store",
               "teams": "microsoft teams", "word": "word", "whats app": "whatsapp"}


def _find_start_app(name: str) -> dict | None:
    name = START_NAMES.get(name, name)
    apps = _installed_apps()
    by_name = {a["Name"].lower(): a for a in apps if a.get("Name") and a.get("AppID")}
    if name in by_name:
        return by_name[name]
    starts = [a for n, a in by_name.items() if n.startswith(name)]
    if starts:
        return min(starts, key=lambda a: len(a["Name"]))
    contains = [a for n, a in by_name.items() if re.search(rf"\b{re.escape(name)}\b", n)]
    if contains:
        return min(contains, key=lambda a: len(a["Name"]))
    close = difflib.get_close_matches(name, list(by_name), n=1, cutoff=0.8)
    return by_name[close[0]] if close else None


def open_app(name: str) -> dict:
    n = _clean_name(name)
    target = APP_ALIASES.get(n)
    if target and (target.endswith(":") or ":" in target or target.endswith(".exe")):
        os.startfile(target)
        return {"opened": name}

    app = _find_start_app(n)
    if app:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{app['AppID']}"], creationflags=NO_WINDOW)
        return {"opened": app["Name"]}

    if target:  # registered in App Paths (chrome, winword, ...)
        subprocess.Popen(["cmd", "/c", "start", "", target], creationflags=NO_WINDOW)
        return {"opened": name}
    return {"error": f"Couldn't find an app called '{name}' on this PC."}


def close_app(name: str) -> dict:
    n = _clean_name(name)
    image = PROCESS_ALIASES.get(n) or (n if n.endswith(".exe") else f"{n.replace(' ', '')}.exe")
    if image.lower() in PROTECTED_PROCESSES:
        return {"error": f"I won't close {image}, Windows or ANVI need it."}
    running = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True,
                             creationflags=NO_WINDOW).stdout
    names = {line.split('","')[0].strip('"') for line in running.splitlines() if line}
    match = next((p for p in names if p.lower() == image.lower()), None)
    if not match:
        return {"error": f"{name} doesn't seem to be running."}
    # no /F: apps close gracefully and can still ask to save work
    subprocess.run(["taskkill", "/IM", match], capture_output=True, creationflags=NO_WINDOW)
    return {"closed": name}


# ---------------------------------------------------------------------------
# Web / media
# ---------------------------------------------------------------------------
def open_website(url_or_query: str) -> dict:
    s = url_or_query.strip()
    if re.match(r"^(https?://)?[\w-]+(\.[\w-]+)+(/\S*)?$", s):
        url = s if s.startswith("http") else f"https://{s}"
    else:
        url = f"https://www.google.com/search?q={quote_plus(s)}"
    webbrowser.open(url)
    return {"opened": url}


def play_youtube(query: str) -> dict:
    r = http.get("https://www.youtube.com/results", params={"search_query": query},
                 headers={"User-Agent": BROWSER_UA, "Accept-Language": "en-IN,en;q=0.9"}, timeout=12)
    r.encoding = "utf-8"
    m = re.search(r'"videoRenderer":\{"videoId":"([\w-]{11})".*?"title":\{"runs":\[\{"text":"(.*?)"\}', r.text)
    if not m:
        webbrowser.open(f"https://www.youtube.com/results?search_query={quote_plus(query)}")
        return {"opened": "YouTube search results", "query": query}
    webbrowser.open(f"https://www.youtube.com/watch?v={m.group(1)}")
    return {"playing": m.group(2).encode().decode("unicode_escape", "ignore"), "on": "YouTube"}


VK = {"volume_up": 0xAF, "volume_down": 0xAE, "mute": 0xAD,
      "play_pause": 0xB3, "next": 0xB0, "previous": 0xB1, "stop": 0xB2}


def _press(vk: int, times: int = 1) -> None:
    for _ in range(times):
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)
        time.sleep(0.01)


def set_volume(level: int | None = None, action: str = "") -> dict:
    """Absolute level 0-100, or action up/down/mute. Windows moves 2% per key press."""
    if level is not None:
        level = max(0, min(100, int(level)))
        _press(VK["volume_down"], 50)
        _press(VK["volume_up"], round(level / 2))
        return {"volume": level}
    action = action.lower()
    if action in ("mute", "unmute"):
        _press(VK["mute"])
        return {"toggled": "mute"}
    if action in ("up", "down"):
        _press(VK[f"volume_{action}"], 5)
        return {"volume": f"{action} 10%"}
    return {"error": "give a level 0-100 or action up/down/mute"}


def media_control(action: str) -> dict:
    key = {"pause": "play_pause", "play": "play_pause", "resume": "play_pause", "skip": "next",
           "back": "previous", "prev": "previous"}.get(action.lower(), action.lower())
    if key not in ("play_pause", "next", "previous", "stop"):
        return {"error": "action must be play_pause, next, previous or stop"}
    _press(VK[key])
    return {"done": key}


# ---------------------------------------------------------------------------
# System
# ---------------------------------------------------------------------------
def take_screenshot() -> dict:
    from PIL import ImageGrab

    folder = HOME / "Pictures" / "ANVI Screenshots"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png"
    ImageGrab.grab(all_screens=True).save(path)
    return {"saved": str(path)}


def lock_pc() -> dict:
    ctypes.windll.user32.LockWorkStation()
    return {"done": "locked"}


class _PowerStatus(ctypes.Structure):
    _fields_ = [("ACLineStatus", ctypes.c_byte), ("BatteryFlag", ctypes.c_byte),
                ("BatteryLifePercent", ctypes.c_byte), ("SystemStatusFlag", ctypes.c_byte),
                ("BatteryLifeTime", wintypes.DWORD), ("BatteryFullLifeTime", wintypes.DWORD)]


class _MemStatus(ctypes.Structure):
    _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]


def _cpu_percent(interval: float = 0.4) -> float:
    def times():
        idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
        ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user))
        as_int = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime  # noqa: E731
        return as_int(idle), as_int(kernel) + as_int(user)

    i1, t1 = times()
    time.sleep(interval)
    i2, t2 = times()
    return round(100 * (1 - (i2 - i1) / max(1, t2 - t1)), 1)


def system_info() -> dict:
    power = _PowerStatus()
    ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(power))
    mem = _MemStatus()
    mem.dwLength = ctypes.sizeof(_MemStatus)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(mem))
    disk = shutil.disk_usage(os.environ.get("SystemDrive", "C:") + "\\")
    info = {
        "cpu_percent": _cpu_percent(),
        "ram_used_percent": mem.dwMemoryLoad,
        "ram_total_gb": round(mem.ullTotalPhys / 2**30, 1),
        "disk_free_gb": round(disk.free / 2**30, 1),
        "disk_total_gb": round(disk.total / 2**30, 1),
    }
    if power.BatteryFlag != -128 and power.BatteryLifePercent != -1:  # has a battery
        info["battery_percent"] = power.BatteryLifePercent & 0xFF
        info["charging"] = power.ACLineStatus == 1
    return info


def power_action(action: str) -> dict:
    action = action.lower()
    if action == "shutdown":
        subprocess.run(["shutdown", "/s", "/t", "30"], creationflags=NO_WINDOW)
        return {"done": "shutting down in 30 seconds (say cancel shutdown to stop)"}
    if action == "restart":
        subprocess.run(["shutdown", "/r", "/t", "30"], creationflags=NO_WINDOW)
        return {"done": "restarting in 30 seconds (say cancel shutdown to stop)"}
    if action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"], creationflags=NO_WINDOW)
        return {"done": "sleeping"}
    if action == "cancel":
        subprocess.run(["shutdown", "/a"], creationflags=NO_WINDOW)
        return {"done": "cancelled pending shutdown/restart"}
    return {"error": "action must be shutdown, restart, sleep or cancel"}


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def _search_roots() -> list[Path]:
    roots = []
    for base in (HOME, HOME / "OneDrive"):
        for sub in ("Desktop", "Documents", "Downloads", "Pictures", "Music", "Videos"):
            p = base / sub
            if p.is_dir() and p not in roots:
                roots.append(p)
    return roots


def find_files(name: str) -> dict:
    words = [w for w in re.findall(r"\w+", name.lower()) if w not in ("file", "folder", "my", "the")]
    if not words:
        return {"error": "what should I look for?"}
    found, deadline = [], time.time() + 4
    for root in _search_roots():
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")]
            for entry in dirnames + filenames:
                low = entry.lower()
                if all(w in low for w in words):
                    p = Path(dirpath) / entry
                    try:
                        found.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
            if time.time() > deadline:
                break
    found.sort(reverse=True)
    return {"matches": [{"name": p.name, "path": str(p), "modified": f"{datetime.fromtimestamp(t):%d %b %Y}"}
                        for t, p in found[:8]]}


def open_path(path: str) -> dict:
    p = Path(os.path.expandvars(os.path.expanduser(path)))
    if not p.exists():
        return {"error": f"{path} doesn't exist"}
    os.startfile(p)
    return {"opened": str(p)}
