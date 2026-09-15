"""Agent tools: let Karen actually do work on the PC, not just talk.

Every tool returns a small JSON-able dict. Tools that could destroy or change
something important return {"needs_confirmation": ...} until they are called
again with confirmed=True, which main.py only allows after the user says yes.
"""

import ctypes
import os
import re
import shutil
import statistics
import subprocess
import threading
import time
import winsound
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path

from net import http

HOME = Path.home()
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MAX_OUTPUT = 3500


def _confirm(summary: str) -> dict:
    return {"needs_confirmation": True, "will_do": summary}


# ---------------------------------------------------------------------------
# Folders and paths
# ---------------------------------------------------------------------------
def _shell_folder(csidl: int, fallback: Path) -> Path:
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0 and buf.value:
        return Path(buf.value)
    return fallback


def known_folders() -> dict[str, str]:
    return {
        "desktop": str(_shell_folder(0x10, HOME / "Desktop")),
        "documents": str(_shell_folder(0x05, HOME / "Documents")),
        "downloads": str(HOME / "Downloads"),
        "pictures": str(_shell_folder(0x27, HOME / "Pictures")),
        "music": str(_shell_folder(0x0D, HOME / "Music")),
        "videos": str(_shell_folder(0x0E, HOME / "Videos")),
        "home": str(HOME),
    }


def resolve(path: str) -> Path:
    """Full path from what the model sends: '~/x', '%USERPROFILE%', 'desktop/notes.txt', or absolute."""
    raw = os.path.expandvars(os.path.expanduser((path or "").strip().strip('"')))
    p = Path(raw)
    if p.is_absolute():
        return p
    first, _, rest = raw.replace("\\", "/").partition("/")
    folders = known_folders()
    if first.lower() in folders:
        return Path(folders[first.lower()]) / rest
    return Path(folders["desktop"]) / raw  # bare names land on the Desktop, where people look first


# ---------------------------------------------------------------------------
# PowerShell
# ---------------------------------------------------------------------------
RISKY_COMMAND = re.compile(
    r"(?<![-\w])format\s+[a-z]:|\b(remove-item|rm|rmdir|rd|del|erase|format-volume|diskpart|clear-disk|initialize-disk|stop-computer|"
    r"restart-computer|shutdown|logoff|reg\s+(add|delete|import)|set-itemproperty|new-itemproperty|"
    r"remove-itemproperty|set-executionpolicy|bcdedit|cipher|takeown|icacls|net\s+(user|localgroup)|"
    r"invoke-expression|iex|stop-process|taskkill|stop-service|set-service|sc(\.exe)?\s+(delete|stop|config)|"
    r"uninstall|winget\s+(uninstall|install)|choco|set-content|out-file|clear-content|clear-recyclebin|"
    r"disable-\w+|enable-\w+|set-netadapter|netsh\b[^|;\n]*\b(set|add|delete|reset)|set-mppreference|runas|"
    r"git\s+(push|reset|clean))\b|-verb\s+runas",
    re.I,
)
_NESTED_PS = re.compile(r"^\s*(?:powershell|pwsh)(?:\.exe)?\s+(?:-\w+\s+)*?-(?:c|command)\s+(.+)$", re.I | re.S)


def _trim(text: str, limit: int = MAX_OUTPUT) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n... [{len(text) - limit} characters cut] ...\n{text[-half:]}"


def run_command(command: str, timeout_seconds: int = 60, confirmed: bool = False) -> dict:
    command = command.strip()
    nested = _NESTED_PS.match(command)
    if nested:  # models sometimes wrap the command in another powershell call
        command = nested.group(1).strip().strip('"')
    if not command:
        return {"error": "empty command"}
    if RISKY_COMMAND.search(command) and not confirmed:
        return _confirm(f"run this PowerShell command: {command}")

    timeout_seconds = max(5, min(int(timeout_seconds or 60), 600))
    script = "$ProgressPreference='SilentlyContinue'; [Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command
    started = time.time()
    proc = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=str(HOME),
        creationflags=NO_WINDOW,
    )
    try:
        out, err = proc.communicate(timeout=timeout_seconds)
        timed_out = False
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, creationflags=NO_WINDOW)
        out, err = proc.communicate()
        timed_out = True

    def decode(b: bytes) -> str:
        return b.decode("utf-8", "replace") if b else ""

    result = {"exit_code": proc.returncode, "seconds": round(time.time() - started, 1),
              "output": _trim(decode(out)) or "(no output)"}
    if err and err.strip():
        result["errors"] = _trim(decode(err), 1500)
    if timed_out:
        result["timed_out"] = f"stopped after {timeout_seconds}s"
    return result


# ---------------------------------------------------------------------------
# Files and folders
# ---------------------------------------------------------------------------
TEXT_LIMIT = 6000


def read_file(path: str) -> dict:
    p = resolve(path)
    if not p.is_file():
        return {"error": f"no file at {p}"}
    size = p.stat().st_size
    raw = p.read_bytes()[:400_000]
    if b"\x00" in raw[:4000]:
        return {"error": f"{p.name} is a binary file ({size} bytes); open it with open_path instead"}
    return {"path": str(p), "size_bytes": size, "content": _trim(raw.decode("utf-8", "replace"), TEXT_LIMIT)}


def write_file(path: str, content: str, append: bool = False, confirmed: bool = False) -> dict:
    p = resolve(path)
    if p.is_dir():
        return {"error": f"{p} is a folder"}
    if p.exists() and not append and not confirmed:
        return _confirm(f"overwrite the existing file {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a" if append else "w", encoding="utf-8", newline="") as f:
        f.write(content)
    return {"saved": str(p), "bytes": p.stat().st_size, "appended": bool(append)}


def list_folder(path: str = "desktop") -> dict:
    p = resolve(path)
    if not p.is_dir():
        return {"error": f"no folder at {p}"}
    entries = []
    for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        try:
            st = child.stat()
        except OSError:
            continue
        entries.append({"name": child.name, "type": "folder" if child.is_dir() else "file",
                        "size_kb": None if child.is_dir() else round(st.st_size / 1024, 1),
                        "modified": f"{datetime.fromtimestamp(st.st_mtime):%d %b %Y %H:%M}"})
    result = {"folder": str(p), "count": len(entries), "entries": entries[:80]}
    if len(entries) > 80:
        result["note"] = f"showing 80 of {len(entries)}"
    return result


def create_folder(path: str) -> dict:
    p = resolve(path)
    existed = p.exists()
    p.mkdir(parents=True, exist_ok=True)
    return {"folder": str(p), "already_existed": existed}


def move_path(source: str, destination: str, copy: bool = False, confirmed: bool = False) -> dict:
    src, dst = resolve(source), resolve(destination)
    if not src.exists():
        return {"error": f"{src} doesn't exist"}
    if dst.is_dir():
        dst = dst / src.name
    if dst.exists() and not confirmed:
        return _confirm(f"replace the existing {dst} with {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if copy:
        if src.is_dir():
            shutil.copytree(src, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(src, dst)
    else:
        shutil.move(str(src), str(dst))
    return {"copied" if copy else "moved": str(src), "to": str(dst)}


class _SHFILEOPSTRUCTW(ctypes.Structure):
    _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]


def delete_path(path: str, confirmed: bool = False) -> dict:
    p = resolve(path)
    if not p.exists():
        return {"error": f"{p} doesn't exist"}
    protected = {Path(v).resolve() for v in known_folders().values()} | {Path(p.anchor).resolve()}
    if p.resolve() in protected:
        return {"error": f"refusing to delete {p}"}
    if not confirmed:
        return _confirm(f"move {p} to the Recycle Bin")
    # FO_DELETE with FOF_ALLOWUNDO (Recycle Bin), no dialogs
    op = _SHFILEOPSTRUCTW(wFunc=3, pFrom=str(p) + "\0\0", fFlags=0x0040 | 0x0010 | 0x0004 | 0x0400)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or p.exists():
        return {"error": f"couldn't delete {p} (code {code})"}
    return {"moved_to_recycle_bin": str(p)}


# ---------------------------------------------------------------------------
# Keyboard and windows
# ---------------------------------------------------------------------------
ULONG_PTR = ctypes.c_size_t


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", _KEYBDINPUT), ("mi", _MOUSEINPUT), ("pad", ctypes.c_byte * 32)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTUNION)]


_user32 = ctypes.windll.user32
_user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int)
_user32.GetForegroundWindow.restype = wintypes.HWND
KEYUP, UNICODE = 0x0002, 0x0004
VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B, "windows": 0x5B, "enter": 0x0D,
    "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "insert": 0x2D, "capslock": 0x14, "printscreen": 0x2C,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}


def _send(events: list[tuple[int, int, int]]) -> None:
    """events: (virtual key, unicode/scan code, flags)"""
    arr = (_INPUT * len(events))()
    for i, (vk, scan, flags) in enumerate(events):
        arr[i].type = 1  # INPUT_KEYBOARD
        arr[i].u.ki = _KEYBDINPUT(vk, scan, flags, 0, 0)
    _user32.SendInput(len(events), arr, ctypes.sizeof(_INPUT))


def _window_title(hwnd) -> str:
    n = _user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    _user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


_ENUM_PROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


def _visible_windows() -> list[tuple[int, str]]:
    found = []

    def collect(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            title = _window_title(hwnd)
            if title and title not in ("Program Manager", "Windows Input Experience"):
                found.append((hwnd, title))
        return True

    _user32.EnumWindows(_ENUM_PROC(collect), 0)
    return found


def list_windows() -> dict:
    return {"windows": [t for _, t in _visible_windows()][:40]}


def focus_window(title: str) -> dict:
    want = title.lower()
    match = next((h for h, t in _visible_windows() if want in t.lower() and not re.match(r"^\s*karen\b", t, re.I)),
                 None)
    if not match:
        return {"error": f"no open window with '{title}' in its title", **list_windows()}
    if _user32.IsIconic(match):
        _user32.ShowWindow(match, 9)  # SW_RESTORE
    _send([(VK["alt"], 0, 0), (VK["alt"], 0, KEYUP)])  # Windows only lets the process that got input move focus
    _user32.SetForegroundWindow(match)
    time.sleep(0.4)
    return {"focused": _window_title(match)}


def _prepare_target(window: str) -> dict | None:
    if window:
        result = focus_window(window)
        return result if "error" in result else None
    if re.match(r"^\s*karen\b", _window_title(_user32.GetForegroundWindow()), re.I):
        return {"error": "Karen's own window is in front. Pass window= the app to type into.", **list_windows()}
    return None


def type_text(text: str, window: str = "") -> dict:
    problem = _prepare_target(window)
    if problem:
        return problem
    events = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [(VK["enter"], 0, 0), (VK["enter"], 0, KEYUP)]
            continue
        code = ord(ch)
        units = [code] if code < 0x10000 else [0xD800 + ((code - 0x10000) >> 10), 0xDC00 + ((code - 0x10000) & 0x3FF)]
        for u in units:
            events += [(0, u, UNICODE), (0, u, UNICODE | KEYUP)]
    for i in range(0, len(events), 200):
        _send(events[i:i + 200])
        time.sleep(0.02)
    return {"typed_characters": len(text), "into": _window_title(_user32.GetForegroundWindow())}


def press_keys(keys: str, window: str = "", times: int = 1) -> dict:
    problem = _prepare_target(window)
    if problem:
        return problem
    codes = []
    for k in (part.strip().lower() for part in keys.split("+") if part.strip()):
        if k in VK:
            codes.append(VK[k])
        elif len(k) == 1 and k.isalnum():
            codes.append(ord(k.upper()))
        else:
            return {"error": f"unknown key '{k}'"}
    times = max(1, min(int(times or 1), 50))
    for _ in range(times):
        _send([(c, 0, 0) for c in codes] + [(c, 0, KEYUP) for c in reversed(codes)])
        time.sleep(0.05)
    return {"pressed": keys, "times": times, "in": _window_title(_user32.GetForegroundWindow())}


# ---------------------------------------------------------------------------
# Network speed test (Cloudflare)
# ---------------------------------------------------------------------------
def speed_test() -> dict:
    base = "https://speed.cloudflare.com"
    headers = {"Referer": f"{base}/"}

    pings = []
    for _ in range(6):
        t = time.perf_counter()
        http.get(f"{base}/__down?bytes=0", headers=headers, timeout=8).raise_for_status()
        pings.append((time.perf_counter() - t) * 1000)
    pings = pings[1:]  # the first one includes connection setup

    # Cloudflare refuses single downloads of 100 MB or more, so fetch 25 MB pieces for ~8 seconds
    received, t0 = 0, time.perf_counter()
    while time.perf_counter() - t0 < 8:
        with http.get(f"{base}/__down?bytes=25000000", headers=headers, stream=True, timeout=(8, 15)) as r:
            r.raise_for_status()
            for chunk in r.iter_content(256 * 1024):
                received += len(chunk)
                if time.perf_counter() - t0 > 8:
                    break
    download = received * 8 / (time.perf_counter() - t0) / 1e6

    blob, sent, t1 = os.urandom(1_000_000), 0, time.perf_counter()
    while time.perf_counter() - t1 < 6:
        http.post(f"{base}/__up", data=blob, headers=headers, timeout=30).raise_for_status()
        sent += len(blob)
    upload = sent * 8 / (time.perf_counter() - t1) / 1e6

    return {"download_mbps": round(download, 1), "upload_mbps": round(upload, 1),
            "ping_ms": round(statistics.median(pings)), "jitter_ms": round(statistics.pstdev(pings)),
            "measured_from": "this PC, to Cloudflare's nearest server"}


# ---------------------------------------------------------------------------
# Reminders and clipboard
# ---------------------------------------------------------------------------
_reminders: list[dict] = []


def set_reminder(minutes: float, message: str) -> dict:
    minutes = float(minutes)
    if not 0 < minutes <= 24 * 60:
        return {"error": "a reminder must be between a few seconds and 24 hours away"}
    due = datetime.now() + timedelta(minutes=minutes)
    entry = {"message": message, "due": f"{due:%I:%M %p}"}

    def fire():
        if entry in _reminders:
            _reminders.remove(entry)
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
        # MB_ICONINFORMATION | MB_SYSTEMMODAL | MB_SETFOREGROUND: shows over every window
        _user32.MessageBoxW(None, message, "Karen reminder", 0x40 | 0x1000 | 0x10000)

    timer = threading.Timer(minutes * 60, fire)
    timer.daemon = True
    timer.start()
    _reminders.append(entry)
    return {"reminder_set_for": entry["due"], "message": message,
            "note": "pops up on the PC; reminders are forgotten if Karen is closed"}


def list_reminders() -> dict:
    return {"reminders": list(_reminders)}


_kernel32 = ctypes.windll.kernel32
_kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
_kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
_kernel32.GlobalLock.restype = ctypes.c_void_p
_kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
_kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
_user32.GetClipboardData.restype = wintypes.HANDLE
_user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
CF_UNICODETEXT = 13


def _open_clipboard() -> bool:
    for _ in range(10):  # another app may be holding it for a moment
        if _user32.OpenClipboard(None):
            return True
        time.sleep(0.05)
    return False


def get_clipboard() -> dict:
    if not _open_clipboard():
        return {"error": "the clipboard is busy"}
    try:
        handle = _user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return {"clipboard": "", "note": "no text on the clipboard"}
        ptr = _kernel32.GlobalLock(handle)
        try:
            return {"clipboard": _trim(ctypes.wstring_at(ptr), 4000)}
        finally:
            _kernel32.GlobalUnlock(handle)
    finally:
        _user32.CloseClipboard()


def set_clipboard(text: str) -> dict:
    data = text.encode("utf-16-le") + b"\0\0"
    handle = _kernel32.GlobalAlloc(0x0042, len(data))  # GHND: movable, zeroed
    ptr = _kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, data, len(data))
    _kernel32.GlobalUnlock(handle)
    if not _open_clipboard():
        return {"error": "the clipboard is busy"}
    try:
        _user32.EmptyClipboard()
        _user32.SetClipboardData(CF_UNICODETEXT, handle)  # the clipboard owns the memory now
    finally:
        _user32.CloseClipboard()
    return {"copied_characters": len(text)}
