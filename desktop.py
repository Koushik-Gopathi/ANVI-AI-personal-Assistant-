"""Karen desktop app: her own window. She runs only while it is open; closing it quits her.

Run:    pythonw desktop.py            (or Karen.exe after packaging)
Build:  build_desktop.bat
"""

import ctypes
import os
import sys
import threading
import time
import urllib.request
from ctypes import wintypes
from pathlib import Path


def _find_home() -> Path:
    """Folder with .env: next to the script, or next to / above Karen.exe."""
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    exe_dir = Path(sys.executable).resolve().parent
    for folder in (exe_dir, *exe_dir.parents[:3]):
        if (folder / ".env").exists():
            return folder
    return exe_dir


HOME = _find_home()
os.environ.setdefault("ANVI_HOME", str(HOME))
os.environ["ANVI_OPEN_BROWSER"] = "0"
# let the window play sound and listen for "Karen" without a click first
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--autoplay-policy=no-user-gesture-required"

if getattr(sys, "frozen", False) and sys.stdout is None:  # windowed exe has no console
    (HOME / ".anvi").mkdir(exist_ok=True)
    sys.stdout = sys.stderr = open(HOME / ".anvi" / "anvi.log", "a", encoding="utf-8", buffering=1)

import webview  # noqa: E402

import main  # noqa: E402

APP_URL = f"http://127.0.0.1:{main.PORT}"
HOTKEY_TEXT = "Ctrl+Alt+A"

window: webview.Window | None = None


# ---------------------------------------------------------------------------
# Microphone permission inside the window
# ---------------------------------------------------------------------------
def _allow_microphone() -> None:
    """WebView2 would ask for mic access on every launch; allow it for Karen's own page."""
    from webview.platforms import edgechromium

    original_ready = edgechromium.EdgeChrome.on_webview_ready

    def on_ready(self, sender, args):
        original_ready(self, sender, args)
        if not args.IsSuccess:
            return
        from Microsoft.Web.WebView2.Core import CoreWebView2PermissionKind, CoreWebView2PermissionState

        def on_permission(_sender, event):
            if event.PermissionKind == CoreWebView2PermissionKind.Microphone and event.Uri.startswith(APP_URL):
                event.State = CoreWebView2PermissionState.Allow

        self._anvi_permission_handler = on_permission  # keep a reference alive
        sender.CoreWebView2.PermissionRequested += on_permission

    edgechromium.EdgeChrome.on_webview_ready = on_ready


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------
def _server_up() -> bool:
    try:
        with urllib.request.urlopen(f"{APP_URL}/api/health", timeout=1) as r:
            return r.status == 200
    except OSError:
        return False


def start_server() -> None:
    if _server_up():  # e.g. `python main.py` is already running: just use it
        return
    threading.Thread(target=main._warm_up, daemon=True).start()
    threading.Thread(target=lambda: main.asyncio.run(main.serve()), daemon=True).start()
    for _ in range(600):  # up to 60 s: startup is slow when the PC is busy
        if _server_up():
            return
        time.sleep(0.1)
    raise RuntimeError(f"Karen's server didn't start on port {main.PORT}")


# ---------------------------------------------------------------------------
# Window actions
# ---------------------------------------------------------------------------
def show_window(*_):
    if window:
        window.show()
        window.restore()
        window.on_top = True  # pop in front of other apps, then behave normally
        window.on_top = False


def toggle_listening(*_):
    if window:
        window.evaluate_js("toggle()")


def _remove_old_autostart() -> None:
    """Earlier versions could start with Windows and hide in the tray; Karen now runs only when opened."""
    import winreg

    run_key = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, run_key, 0, winreg.KEY_READ | winreg.KEY_SET_VALUE) as key:
            for name in ("ANVI", "Karen"):
                try:
                    winreg.DeleteValue(key, name)
                except OSError:
                    pass
    except OSError:
        pass


def listen_for_hotkey() -> None:
    """Ctrl+Alt+A from any app, while Karen is open: bring her to the front and wake her (or put her to sleep)."""
    user32 = ctypes.windll.user32
    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT, WM_HOTKEY = 0x0001, 0x0002, 0x4000, 0x0312
    if not user32.RegisterHotKey(None, 1, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, ord("A")):
        print(f"{HOTKEY_TEXT} is already used by another app")
        return
    msg = wintypes.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
        if msg.message == WM_HOTKEY:
            show_window()
            toggle_listening()


def _single_instance() -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW(None, False, "Local\\Karen-desktop-app")
    return kernel32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def run() -> None:
    global window
    if not _single_instance():
        # already open: bring that window to the front instead
        try:
            request = urllib.request.Request(f"{APP_URL}/api/desktop/show", method="POST")
            urllib.request.urlopen(request, timeout=3).close()
        except OSError:
            ctypes.windll.user32.MessageBoxW(None, "Karen is already open.",
                                             "Karen", 0x40)
        return

    _remove_old_autostart()
    start_server()
    main.show_window_hook = show_window
    _allow_microphone()
    window = webview.create_window(
        "Karen", f"{APP_URL}/?app=desktop", width=1100, height=760, min_size=(420, 560),
        background_color="#080B14",
    )
    threading.Thread(target=listen_for_hotkey, daemon=True).start()
    if os.getenv("ANVI_DEBUG") == "1":
        def report():
            window.events.loaded.wait(30)
            for _ in range(6):
                time.sleep(2.5)
                print("window state:", window.evaluate_js(
                    "JSON.stringify({audio: audioCtx && audioCtx.state, passive, state, mic: !!micStream,"
                    " recording: !!recorder, status: statusEl.textContent})"), flush=True)
        threading.Thread(target=report, daemon=True).start()
    webview.start(private_mode=False, storage_path=str(HOME / ".anvi" / "webview"))
    # the window was closed: stop everything (server, listening, workers) right away
    os._exit(0)


if __name__ == "__main__":
    run()
