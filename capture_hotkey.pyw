"""Second Brain -- global capture hotkey (Story F1).

Ctrl+Alt+Z, anywhere in Windows, opens a one-line capture box. Enter
files it; Escape throws it away. This process is the whole point of the
story: it must be resident (started once, at login) rather than launched
fresh on every keypress, because a cold Python + tkinter start is commonly
several hundred milliseconds to over a second -- against a 5-second budget
for the *whole* keypress-to-filed interaction, spawning a process per press
is not a safe design. Registering the hotkey once and keeping the process
alive means the only per-keypress costs are a Win32 message and a localhost
HTTP call.

Capture path:
  1. Try `POST http://127.0.0.1:8787/api/capture` (the same endpoint the
     dashboard and the Obsidian plugin use) with a short timeout.
  2. On *any* failure -- connection refused (app not running), timeout, or
     a non-2xx response -- write the text straight into the vault's Drop/
     folder instead. The app (or `python run.py intake`) files it from
     there on its own next pass. This is the fallback the story exists for:
     a thought must not be lost because the server happened to be down.
  3. Only if *both* of those fail (e.g. disk unwritable) does the user see
     a failure toast -- reporting success when nothing was actually saved
     would be worse than reporting nothing.

Runs under pythonw.exe (no console window). Standard library only: no
pywin32, no requests. The system-wide hotkey is registered with
`RegisterHotKey` via ctypes -- the only way to get an OS-wide hotkey from
pure Python without a third-party package -- using a NULL window handle,
which posts WM_HOTKEY straight to this thread's message queue and avoids
the extra ceremony of creating a real window class just to receive it.
tkinter (stdlib, ships with the python.org Windows build used here --
confirmed by the presence of .venv/Scripts/pythonw.exe, which only that
build produces) provides the input box and toast.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from ctypes import wintypes
from pathlib import Path

# -- make pythonw.exe safe to run: sys.stdout/stderr are None under it, and
#    an untouched print() or traceback would crash the process with no
#    console to show why. Redirect to a small log file before anything else
#    can go wrong. -----------------------------------------------------------

def _open_log():
    """Log beside the rest of the system's logs, not off in LOCALAPPDATA.

    A capture tool that fails silently is indistinguishable from a broken
    one, and the first question is always "is it even running?". Keeping
    the log in _system/logs puts the answer where doctor and anyone
    debugging the vault will actually look. Falls back to LOCALAPPDATA if
    the vault is read-only for some reason."""
    for base in (
        Path(__file__).resolve().parent / "_system" / "logs",
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "secondbrain",
    ):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return open(base / "capture-hotkey.log", "a", buffering=1, encoding="utf-8")
        except Exception:
            continue
    return open(os.devnull, "w")


_LOG = _open_log()
sys.stdout = _LOG
sys.stderr = _LOG


def log(msg: str) -> None:
    try:
        _LOG.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
    except Exception:
        pass


# -- config: same host/port/drop-folder the app itself uses -----------------
# Read straight out of config.yaml (a couple of regexes -- no PyYAML import
# needed just for two scalars) so this stays in sync with the running app
# without ever writing to that file. Falls back to the documented defaults
# (sb/config.py: host 127.0.0.1, port 8787, drop folder "Drop") if the file
# is missing or unreadable.

APP_DIR = Path(__file__).resolve().parent


def _load_endpoint() -> tuple[str, Path]:
    host, port, drop_folder = "127.0.0.1", 8787, "Drop"
    try:
        text = (APP_DIR / "config.yaml").read_text(encoding="utf-8")
        m = re.search(r"^host:\s*(\S+)", text, re.M)
        if m:
            host = m.group(1).strip()
        m = re.search(r"^port:\s*(\d+)", text, re.M)
        if m:
            port = int(m.group(1))
        m = re.search(r"^intake:\s*(?:.*\n)*?\s*folder:\s*(\S+)", text, re.M)
        if m:
            drop_folder = m.group(1).strip().strip("'\"")
    except Exception as exc:
        log(f"config.yaml not read, using defaults: {exc!r}")
    return f"http://{host}:{port}/api/capture", APP_DIR / drop_folder


API_URL, DROP_DIR = _load_endpoint()
API_TIMEOUT_SECONDS = 2.0  # generous for a local hung server; instant on refusal

# -- capture: API first, Drop/ fallback --------------------------------------

_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def try_api(text: str) -> bool:
    payload = json.dumps({"text": text, "bucket": "inbox"}).encode("utf-8")
    req = urllib.request.Request(
        API_URL, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            return 200 <= resp.status < 300
    except urllib.error.HTTPError as exc:
        log(f"API non-2xx: HTTP {exc.code}")
        return False
    except Exception as exc:
        # Connection refused, timeout, DNS-in-a-weird-VPN-state, whatever --
        # all of it means "cannot reach the app right now", which is exactly
        # the case the Drop-folder fallback exists for.
        log(f"API unreachable: {type(exc).__name__}: {exc}")
        return False


def write_drop_fallback(text: str) -> Path:
    """Mirror of the Obsidian plugin's own fallback (main.js `dropFile`):
    a plain .md file, first line in the name, no frontmatter -- the same
    shape intake.py already reads. Bucket choice isn't preserved because it
    isn't in this plugin's fallback either; the file goes through the same
    rule/model classifier as any other Drop file."""
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    first_line = text.split("\n", 1)[0]
    safe = _INVALID_NAME_CHARS.sub("", first_line).strip()[:50] or "capture"
    target = DROP_DIR / f"{stamp} {safe}.md"
    n = 2
    while target.exists():
        target = DROP_DIR / f"{stamp} {safe} ({n}).md"
        n += 1
    target.write_text(text, encoding="utf-8")
    return target


def handle_capture(text: str) -> None:
    text = text.strip()
    if not text:
        return
    if try_api(text):
        log(f"filed via API: {text[:60]!r}")
        beep_ok()
        notify("Captured.")
        return
    try:
        path = write_drop_fallback(text)
        log(f"API unreachable, filed to Drop: {path}")
        beep_ok()
        notify("Captured (Second Brain offline -- saved to Drop).")
    except Exception as exc:
        log(f"DROP FALLBACK FAILED, capture lost: {exc!r}")
        beep_fail()
        notify("Capture FAILED -- nothing was saved.", error=True)


def beep_ok() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_OK)
    except Exception:
        pass


def beep_fail() -> None:
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONHAND)
    except Exception:
        pass


# -- Win32 global hotkey, via ctypes -----------------------------------------
# MOD_ALT=0x0001, MOD_CONTROL=0x0002 (Windows' own modifier bit values --
# https://learn.microsoft.com/windows/win32/inputdev/wm-hotkey), MOD_NOREPEAT
# =0x4000 (stop a held-down key from queuing a WM_HOTKEY per auto-repeat
# tick). VK_SPACE=0x20 is the standard virtual-key code for the space bar.
#
# hWnd=NULL on both RegisterHotKey and GetMessage: with no window handle the
# hotkey is bound to *this thread*, and Windows posts WM_HOTKEY straight into
# this thread's message queue -- retrievable with a plain GetMessage loop and
# no window class, WNDPROC, or CreateWindowEx ceremony at all.

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000
VK_SPACE = 0x20
WM_HOTKEY = 0x0312
HOTKEY_ID = 1
ERROR_ALREADY_EXISTS = 183

_MODS = {
    "ctrl": MOD_CONTROL, "control": MOD_CONTROL,
    "alt": MOD_ALT, "shift": MOD_SHIFT,
    "win": MOD_WIN, "super": MOD_WIN, "meta": MOD_WIN,
}
_KEYS = {"space": VK_SPACE, "enter": 0x0D, "return": 0x0D, "tab": 0x09}
_KEYS.update({f"f{n}": 0x6F + n for n in range(1, 13)})  # F1=0x70 .. F12=0x7B


def parse_hotkey(spec):
    """'ctrl+alt+n' -> (modifier bitmask, virtual-key code), or None.

    Only used for the value in config.yaml; the built-in candidates below
    are already parsed. Unknown names return None so a typo falls through
    to the defaults instead of registering something surprising."""
    mods, vk = 0, None
    for part in str(spec).lower().replace(" ", "").split("+"):
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _KEYS:
            vk = _KEYS[part]
        elif len(part) == 1 and (part.isalpha() or part.isdigit()):
            vk = ord(part.upper())
        else:
            return None
    return (mods, vk) if (mods and vk) else None


# Ctrl+Alt+Space is NOT a candidate: the Claude desktop app registers it
# globally, wins the race at login, and this process then never sees the
# key. Candidates are tried in order and the first that registers wins;
# whichever it is gets logged and shown once at startup, because a capture
# box on an unknown key is the same as no capture box.
HOTKEY_CANDIDATES = [
    ("Ctrl+Alt+Z", MOD_CONTROL | MOD_ALT, ord("Z")),
    ("Ctrl+Alt+N", MOD_CONTROL | MOD_ALT, ord("N")),
    ("Ctrl+Shift+F9", MOD_CONTROL | MOD_SHIFT, 0x78),
    ("Win+Alt+Z", MOD_WIN | MOD_ALT, ord("Z")),
]

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG), wintypes.HWND, ctypes.c_uint, ctypes.c_uint
]
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_uint]
kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE

EVENTS: "queue.Queue" = queue.Queue()


def ensure_single_instance() -> bool:
    """A second copy registering the same hotkey would silently steal it
    from the first (or fail); a named mutex makes a double-launch (a stray
    manual start plus the Startup-folder shortcut, say) a harmless no-op."""
    kernel32.CreateMutexW(None, False, "SecondBrainCaptureHotkeyMutex")
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def _candidates():
    """Config first, then the built-in fallbacks."""
    out = []
    try:
        import yaml  # noqa
        cfgp = APP_DIR / "config.yaml"
        if cfgp.exists():
            raw = yaml.safe_load(cfgp.read_text(encoding="utf-8")) or {}
            spec = raw.get("capture_hotkey")
            if spec:
                parsed = parse_hotkey(spec)
                if parsed:
                    out.append((str(spec), parsed[0], parsed[1]))
                else:
                    log(f"capture_hotkey {spec!r} in config.yaml is not a hotkey I understand")
    except Exception as exc:  # config is optional; never let it stop capture
        log(f"could not read capture_hotkey from config.yaml: {exc}")
    return out + HOTKEY_CANDIDATES


def hotkey_thread() -> None:
    chosen = None
    for label, mods, vk in _candidates():
        if user32.RegisterHotKey(None, HOTKEY_ID, mods | MOD_NOREPEAT, vk):
            chosen = label
            break
        log(f"{label} unavailable (error={ctypes.get_last_error()}) — trying the next one")

    if chosen is None:
        log("RegisterHotKey FAILED for every candidate")
        user32.MessageBoxW(
            None,
            "Second Brain could not register a capture hotkey — every "
            "candidate is already owned by another application.\n\n"
            "Set one explicitly in config.yaml, e.g.\n"
            "    capture_hotkey: ctrl+alt+k",
            "Second Brain Capture",
            0x30,  # MB_ICONWARNING
        )
        return

    log(f"hotkey registered: {chosen}")
    notify(f"Capture ready — press {chosen}")

    msg = wintypes.MSG()
    try:
        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                EVENTS.put(("hotkey",))
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


def notify(text: str, error: bool = False) -> None:
    EVENTS.put(("notify", text, error))


# -- tkinter UI ----------------------------------------------------------
# One resident Tk root (withdrawn -- never shown itself); the capture box
# and the toast are Toplevels created on demand. All Tk calls happen on the
# main thread only -- the hotkey thread and the network call each just drop
# an event on EVENTS, and poll_events() (running via root.after, on the main
# thread) is what actually touches widgets.

import tkinter as tk  # noqa: E402  (after the stdout/stderr redirect above)

_box = {"win": None, "entry": None}


def show_capture_box(root: tk.Tk) -> None:
    win = _box["win"]
    if win is not None:
        try:
            win.deiconify()
            win.lift()
            win.focus_force()
            _box["entry"].focus_set()
            _box["entry"].selection_range(0, tk.END)
            return
        except tk.TclError:
            _box["win"] = None

    win = tk.Toplevel(root)
    win.title("Second Brain Capture")
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#3a3a3a")

    frame = tk.Frame(win, bg="#3a3a3a", padx=10, pady=8)
    frame.pack(fill="both", expand=True)
    tk.Label(
        frame, text="Capture  (Enter to file · Esc to cancel)",
        bg="#3a3a3a", fg="#bbbbbb", font=("Segoe UI", 9),
        anchor="w",
    ).pack(fill="x", pady=(0, 4))
    entry = tk.Entry(frame, width=64, font=("Segoe UI", 13))
    entry.pack(fill="x")

    def submit(_event=None):
        text = entry.get()
        close()
        if text.strip():
            threading.Thread(target=handle_capture, args=(text,), daemon=True).start()

    def cancel(_event=None):
        close()

    def close():
        win.withdraw()

    entry.bind("<Return>", submit)
    entry.bind("<KP_Enter>", submit)
    entry.bind("<Escape>", cancel)
    win.bind("<Escape>", cancel)
    win.protocol("WM_DELETE_WINDOW", cancel)

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh // 3}")
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.focus_force()
    entry.focus_set()

    _box["win"] = win
    _box["entry"] = entry


def show_toast(root: tk.Tk, text: str, error: bool) -> None:
    toast = tk.Toplevel(root)
    toast.overrideredirect(True)
    toast.attributes("-topmost", True)
    bg = "#5c1a1a" if error else "#1f4d2b"
    tk.Label(
        toast, text=text, bg=bg, fg="#ffffff", font=("Segoe UI", 10),
        padx=14, pady=8,
    ).pack()
    toast.update_idletasks()
    sw, sh = toast.winfo_screenwidth(), toast.winfo_screenheight()
    w, h = toast.winfo_reqwidth(), toast.winfo_reqheight()
    toast.geometry(f"+{sw - w - 24}+{sh - h - 64}")
    toast.after(1600, toast.destroy)


def poll_events(root: tk.Tk) -> None:
    try:
        while True:
            item = EVENTS.get_nowait()
            if item[0] == "hotkey":
                show_capture_box(root)
            elif item[0] == "notify":
                show_toast(root, item[1], item[2])
    except queue.Empty:
        pass
    root.after(80, poll_events, root)


def main() -> int:
    log("=== capture_hotkey starting ===")
    if not ensure_single_instance():
        log("another instance already holds the mutex; exiting")
        return 0

    threading.Thread(target=hotkey_thread, daemon=True).start()

    root = tk.Tk()
    root.withdraw()
    root.after(80, poll_events, root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("FATAL:\n" + traceback.format_exc())
        raise
