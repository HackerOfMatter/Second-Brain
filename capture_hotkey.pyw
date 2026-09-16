"""Second Brain -- global capture hotkey (Story F1; extended, housekeeping 1-2).

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

Three things the box does beyond taking a line of text, all of them in
service of the same idea -- that the cheapest moment to record something is
the moment you met it, and every step between the two loses some of it:

  * **It reads what is highlighted.** Press the hotkey with a word selected
    anywhere in Windows and the box opens with that word already in it,
    selected, so Enter accepts it and typing replaces it. See
    `grab_selection` for how that is done without stealing the clipboard.
  * **It knows what a term is.** A short one-line selection opens in Term
    mode, which has a second field for the definition. A term and its
    definition become a note *and a card*, written rather than generated --
    see `Engine.capture_term`.
  * **It can open a note-taking session.** `/start fed tax / ch 4` in the
    box, and every capture until `/end` lands in that class's chapter
    folder, with a summary written when it closes. See sb/session.py.
  * **It notices a Win+Shift+S snip.** Windows owns that key and it cannot be
    intercepted; it does not need to be, because the snip's only effect is a
    bitmap on the clipboard and this process is already resident. During a
    session the snip is filed into the session\'s folder as a note with the
    image in it. Outside one it files *nothing* -- see `watch_clipboard` for
    why that restraint is the whole design -- and instead offers itself to
    the next capture, where the line typed becomes the caption. The DIB is
    turned into a PNG by hand (`png_from_dib`) rather than by pulling in an
    imaging library this file has no business depending on.
"""

from __future__ import annotations

import base64
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
TERM_URL = API_URL.rsplit("/", 1)[0] + "/capture/term"
API_TIMEOUT_SECONDS = 2.0  # generous for a local hung server; instant on refusal
#: A pasted glossary files one term note and card per line, which can take
#: longer than one capture. A timeout here would send the same paste to Drop
#: as well; the server skips terms it already has, but waiting is cleaner.
GLOSSARY_TIMEOUT_SECONDS = 30.0
_GLOSSARY_LINE = re.compile(r"^\s*(?:[-*+•]\s+|\d{1,3}[.)]\s+)?[^\t:=—–]{1,60}?\s*(?:::|\t|:|=|\s[—–-]\s|[—–])\s*\S")


def looks_like_glossary(text: str) -> bool:
    """Two or more `term: definition` lines (the server decides for real)."""
    lines = [l for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    hits = sum(1 for l in lines if _GLOSSARY_LINE.match(l))
    return hits >= 2 and hits >= 0.6 * len(lines)

# -- capture: API first, Drop/ fallback --------------------------------------

_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]]')


def try_api(text: str) -> bool:
    # No bucket. The box has no buttons and asserting one here would be this
    # file inventing a classification it has no basis for — the server's
    # `capture.default_bucket` is where that decision belongs, and it is one
    # line in config.yaml rather than an edit to a startup script.
    payload = json.dumps({"text": text}).encode("utf-8")
    timeout = GLOSSARY_TIMEOUT_SECONDS if looks_like_glossary(text) else API_TIMEOUT_SECONDS
    return _post(API_URL, payload, timeout=timeout)


def _get(url: str):
    """A GET that answers `None` rather than raising. Everything this is used
    for is a label on a window; none of it is worth a traceback."""
    try:
        with urllib.request.urlopen(url, timeout=API_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def try_term_api(term: str, definition: str, source: str) -> bool:
    payload = json.dumps(
        {"term": term, "definition": definition, "source": source}
    ).encode("utf-8")
    return _post(TERM_URL, payload)


def _post(url: str, payload: bytes, timeout: float = API_TIMEOUT_SECONDS) -> bool:
    req = urllib.request.Request(
        url, data=payload, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
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


def write_drop_fallback(text: str, name: str = "") -> Path:
    """Mirror of the Obsidian plugin's own fallback (main.js `dropFile`):
    a plain .md file, first line in the name, no frontmatter -- the same
    shape intake.py already reads. Bucket choice isn't preserved because it
    isn't in this plugin's fallback either; the file goes through the same
    rule/model classifier as any other Drop file."""
    DROP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%dT%H-%M-%S")
    first_line = name or text.split("\n", 1)[0]
    safe = _INVALID_NAME_CHARS.sub("", first_line).strip()[:50] or "capture"
    target = DROP_DIR / f"{stamp} {safe}.md"
    n = 2
    while target.exists():
        target = DROP_DIR / f"{stamp} {safe} ({n}).md"
        n += 1
    target.write_text(text, encoding="utf-8")
    return target


# --8<-- testable: the two rules the box's behaviour turns on, and the
# offline shape of a term note. Sliced out by tests/test_all.py, which
# cannot import this file off Windows. Keep both markers in place.
def term_markdown(term: str, definition: str, source: str) -> str:
    """The Term note, written by hand, for the Drop fallback.

    Must match `engine._term_body` closely enough that a term filed while the
    app was closed reads the same as one filed while it was open — that is
    the whole promise of the fallback. It cannot match it *exactly*: this
    file goes through `intake`, which classifies it rather than trusting a
    bucket we assert, so the frontmatter is deliberately absent and the
    shape is what carries the meaning.
    """
    lines = [f"# {term}", "", "## Definition", ""]
    if definition:
        lines += [definition, ""]
    lines += ["## In my own words", "", "## Seen in", ""]
    if source:
        lines += [f"- {source}", ""]
    return "\n".join(lines)


# -- Win+Shift+S: the snip, turned into a file --------------------------------
# Windows owns Win+Shift+S; it cannot be intercepted and does not need to be.
# What the snip *does* is put a device-independent bitmap on the clipboard, and
# this process is already resident with a message loop — so watching the
# clipboard sequence number costs one DWORD call a second and tells us the
# moment a new image appears.
#
# Turning that DIB into a PNG by hand, with no Pillow and no pywin32, is the
# unglamorous half. It is worth it: a screenshot that needs a third-party
# imaging stack installed is a screenshot that stops working the first time
# the venv is rebuilt, and this file's whole contract is that it is stdlib
# only and therefore always runs.
#
# Only 24- and 32-bit uncompressed DIBs are handled, which is what every
# Windows screen capture produces. Anything else is reported rather than
# guessed at, because a wrong guess here writes a corrupt file into the vault
# and calls it a note.

CF_DIB = 8
BI_RGB, BI_BITFIELDS = 0, 3


def png_from_dib(data: bytes) -> "tuple[bytes, int, int]":
    """(PNG bytes, width, height) from a CF_DIB clipboard payload.

    Alpha is deliberately dropped. A 32-bit BI_RGB DIB has a fourth byte that
    is *undefined*, and Windows screen capture leaves it at zero — read as
    alpha, every screenshot comes out fully transparent, which looks exactly
    like the feature silently not working. Screenshots are opaque; the fourth
    byte is padding and is treated as padding.
    """
    import struct
    import zlib

    if len(data) < 40:
        raise ValueError("clipboard bitmap is too small to be an image")
    header_size, width, height = struct.unpack_from("<Iii", data, 0)
    planes, bits = struct.unpack_from("<HH", data, 12)
    compression = struct.unpack_from("<I", data, 16)[0]
    clr_used = struct.unpack_from("<I", data, 32)[0]

    if header_size < 40:
        raise ValueError(f"unsupported bitmap header ({header_size} bytes)")
    if bits not in (24, 32):
        raise ValueError(f"unsupported colour depth ({bits}-bit)")
    if compression not in (BI_RGB, BI_BITFIELDS):
        raise ValueError("the bitmap is compressed in a format we do not read")

    offset = header_size
    # BI_BITFIELDS puts three masks after a 40-byte header. Larger headers
    # (V4, V5) carry the masks inside themselves and need no skip.
    if compression == BI_BITFIELDS and header_size == 40:
        offset += 12
    offset += clr_used * 4  # a palette, if one is claimed

    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0:
        raise ValueError("the bitmap has no size")

    stride = ((width * bits + 31) // 32) * 4
    step = bits // 8
    needed = offset + stride * height
    if len(data) < needed:
        raise ValueError("the clipboard bitmap is truncated")

    rows = []
    for y in range(height):
        # A DIB is bottom-up unless its height is negative.
        src = offset + (y if top_down else height - 1 - y) * stride
        row = bytearray(width * 3)
        for x in range(width):
            b, g, r = data[src], data[src + 1], data[src + 2]
            row[x * 3:x * 3 + 3] = bytes((r, g, b))   # BGR on the wire, RGB in a PNG
            src += step
        rows.append(bytes(row))

    raw = b"".join(b"\x00" + row for row in rows)  # filter byte 0 per scanline

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (struct.pack(">I", len(payload)) + kind + payload
                + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(raw, 6))
           + chunk(b"IEND", b""))
    return png, width, height


# --8<-- end testable

# -- reading the image off the clipboard --------------------------------------

ATTACH_URL = API_URL.rsplit("/api/", 1)[0] + "/api/attach"

user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.GetClipboardData.argtypes = [ctypes.c_uint]
user32.GetClipboardData.restype = wintypes.HANDLE
kernel32.GlobalLock.argtypes = [wintypes.HANDLE]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.argtypes = [wintypes.HANDLE]
kernel32.GlobalSize.restype = ctypes.c_size_t


def clipboard_dib():
    """The clipboard's bitmap as bytes, or None.

    Opening the clipboard can fail outright — another process holds it for a
    moment on every copy anywhere in Windows — and that is a retry, not an
    error. A few attempts over a fifth of a second covers it; failing after
    that means somebody else is genuinely using it and we simply do not have
    the image this time round.
    """
    if not user32.IsClipboardFormatAvailable(CF_DIB):
        return None
    for _ in range(6):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.03)
    else:
        return None
    try:
        handle = user32.GetClipboardData(CF_DIB)
        if not handle:
            return None
        size = kernel32.GlobalSize(handle)
        ptr = kernel32.GlobalLock(handle)
        if not ptr or not size:
            return None
        try:
            return ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


#: An image seen but not yet filed, held for the next capture. See
#: `watch_clipboard` for why a snip outside a session waits rather than files.
_pending = {"png": None, "seen_at": 0.0}
PENDING_SECONDS = 180.0


def file_screenshot(png: bytes, caption: str = "", source: str = "") -> bool:
    payload = json.dumps({
        "data": base64.b64encode(png).decode("ascii"),
        "caption": caption, "source": source,
    }).encode("utf-8")
    return _post(ATTACH_URL, payload)


def watch_clipboard() -> None:
    """Notice a snip and, during a session, file it where the session is.

    Windows owns Win+Shift+S and there is no way to intercept it. There is
    also no need: the snip's only effect is to put a bitmap on the clipboard,
    and this process is already resident, so a sequence-number check once a
    second sees it land.

    **Outside a session it files nothing**, and that restraint is the whole
    design. Most screenshots anyone takes are not notes — they are a receipt
    being pasted into an email, a bug going to a colleague — and a tool that
    quietly copied every one of them into a vault would be something you
    would want to turn off within a day. Instead it is held for three
    minutes, and if the capture box is opened in that time it offers to
    attach it. Nothing is read, nothing is written, and the clipboard is
    never modified.
    """
    last = 0
    while True:
        time.sleep(1.0)
        try:
            seq = user32.GetClipboardSequenceNumber()
            if seq == last:
                continue
            last = seq
            dib = clipboard_dib()
            if not dib:
                continue
            png, w, h = png_from_dib(dib)
        except ValueError as exc:
            log(f"clipboard image not readable: {exc}")
            continue
        except Exception as exc:
            log(f"clipboard watch: {type(exc).__name__}: {exc}")
            continue

        label = session_label()
        if label:
            source = foreground_title()
            if file_screenshot(png, source=source):
                log(f"screenshot filed into {label} ({w}x{h})")
                beep_ok()
                notify(f"Screenshot → {label}")
            else:
                log("screenshot could not be filed — server unreachable")
                notify("Screenshot not filed — Second Brain is not running.", error=True)
            continue

        # No session: hold it, say so once, and let the capture box offer it.
        _pending["png"], _pending["seen_at"] = png, time.time()
        EVENTS.put(("pending", True))


def pending_png():
    """The held screenshot, if it is still fresh."""
    if not _pending["png"]:
        return None
    if time.time() - _pending["seen_at"] > PENDING_SECONDS:
        _pending["png"] = None
        return None
    return _pending["png"]


# -- sessions, from the box --------------------------------------------------
# A slash command rather than a second window. Starting a lecture's session is
# a thing done with a laptop half-open thirty seconds before it begins, and
# the fastest path to it is the box that is already bound to a hotkey. Three
# commands is the whole surface:
#
#     /start fed tax / ch 4      open a session on that class and chapter
#     /end                       close it and write the summary
#     /status                    say what is open
#
# The class name is *matched* against folders that already exist, so "fed tax"
# finds "Federal Taxation" rather than making a fourth spelling of it — see
# sb/session.py for why that matters more than it sounds like it should.

SESSION_BASE = API_URL.rsplit("/api/", 1)[0] + "/api/session"


def session_label() -> str:
    """"Federal Taxation · Ch 4", or "" when nothing is open."""
    data = _get(SESSION_BASE) or {}
    session = data.get("session")
    if not isinstance(session, dict):
        return ""
    return " · ".join(p for p in (session.get("course"), session.get("chapter")) if p)


def handle_command(text: str) -> None:
    """`/start`, `/end`, `/status`. Anything else is captured as written —
    a note that happens to begin with a slash is still a note."""
    body = text[1:].strip()
    word, _, rest = body.partition(" ")
    word = word.lower()

    if word in ("end", "stop", "close"):
        out = _get_json_post(f"{SESSION_BASE}/end", {})
        if out is None:
            beep_fail()
            notify("Could not close the session — is Second Brain running?", error=True)
            return
        if out.get("error"):
            notify(out["error"], error=True)
            return
        data = out.get("data", out)
        beep_ok()
        notify(f"Session closed — {len(data.get('notes') or [])} notes, "
               f"{len(data.get('undefined') or [])} to look up")
        return

    if word in ("status", "?"):
        label = session_label()
        notify(f"Session: {label}" if label else "No session open")
        return

    if word in ("start", "session", "class"):
        course, _, chapter = rest.partition("/")
        if not course.strip():
            notify("Say which class: /start fed tax / ch 4", error=True)
            return
        out = _get_json_post(
            f"{SESSION_BASE}/start",
            {"course": course.strip(), "chapter": chapter.strip()},
        )
        if out is None:
            beep_fail()
            notify("Could not start a session — is Second Brain running?", error=True)
            return
        if out.get("error"):
            notify(out["error"], error=True)
            return
        data = out.get("data", out)
        session = data.get("session") or {}
        # Which folder it picked, and how. "matched: new" is the one worth
        # seeing — it means a folder was created, which is either right or a
        # typo, and only lj can tell which.
        beep_ok()
        notify(f"Session: {session.get('folder', '')}"
               + (f"  (new folder)" if data.get("matched") == "new" else ""))
        return

    # Not a command. File it as typed, slash and all.
    handle_capture(text)


def _get_json_post(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"error": f"HTTP {exc.code}"}
    except Exception as exc:
        log(f"session call failed: {type(exc).__name__}: {exc}")
        return None


def handle_capture(text: str) -> None:
    text = text.strip()
    if not text:
        return
    if try_api(text):
        log(f"filed via API: {text[:60]!r}")
        beep_ok()
        notify("Terms captured — a card each." if looks_like_glossary(text) else "Captured.")
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


def handle_term(term: str, definition: str, source: str) -> None:
    term, definition = term.strip(), definition.strip()
    if not term:
        return
    if try_term_api(term, definition, source):
        log(f"term filed via API: {term!r} ({'defined' if definition else 'no definition'})")
        beep_ok()
        notify(f"Term: {term}" + ("" if definition else " — no definition yet"))
        return
    try:
        path = write_drop_fallback(term_markdown(term, definition, source), name=term)
        log(f"API unreachable, term filed to Drop: {path}")
        beep_ok()
        notify("Term saved (Second Brain offline -- saved to Drop).")
    except Exception as exc:
        log(f"DROP FALLBACK FAILED, term lost: {exc!r}")
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

# -- reading what is highlighted, without stealing the clipboard -------------
# There is no Win32 call for "give me the selected text in some other app".
# The only general mechanism is the one the user would use: send the
# foreground window Ctrl+C and see what turns up. Every tool that does this
# has the same two problems, and both are solvable:
#
#   1. The hotkey is Ctrl+Alt+Z, so at the moment it fires Ctrl and Alt are
#      physically held down. A synthetic 'C' on top of that is Ctrl+Alt+C,
#      which is not copy. So the modifiers we did not want are released
#      synthetically first, and Ctrl is pressed cleanly.
#   2. It clobbers the clipboard. `GetClipboardSequenceNumber` is what makes
#      this honest: if the number does not move, the app had nothing selected
#      (or ignored us), and the clipboard is left exactly as it was rather
#      than the old contents being "restored" over themselves. When it does
#      move, the previous *text* is put back afterwards. A previous clipboard
#      holding an image cannot be restored and is reported rather than lied
#      about — see `grab_selection`.

VK_CONTROL, VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN = 0x11, 0x12, 0x10, 0x5B, 0x5C
KEYEVENTF_KEYUP = 0x0002

user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.keybd_event.argtypes = [
    wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.POINTER(wintypes.ULONG)
]


def foreground_title() -> str:
    """The title of the window lj was looking at — the term's "Seen in".

    Free, and it is the difference between a term note that says where it was
    met and one that does not. A term met in three places is a term you
    understand; a term met nowhere is a word you wrote down.
    """
    try:
        buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(user32.GetForegroundWindow(), buf, 512)
        return buf.value.strip()
    except Exception:
        return ""


def _tap(vk: int) -> None:
    user32.keybd_event(vk, 0, 0, None)
    user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)


def grab_selection(root: "tk.Tk") -> str:
    """Whatever is highlighted in the foreground window, or "".

    Runs on the Tk main thread, before the box is shown, and blocks for up to
    ~250 ms waiting for the other application to answer. That is the budget:
    a box that appears a quarter-second late with the word already in it is a
    better trade than one that appears instantly and empty.
    """
    try:
        before = user32.GetClipboardSequenceNumber()
    except Exception as exc:
        log(f"clipboard sequence unavailable: {exc!r}")
        return ""

    try:
        old = root.clipboard_get()
        old_was_text = True
    except Exception:
        old, old_was_text = "", False

    try:
        # Let go of what the hotkey is holding, then copy cleanly.
        for vk in (VK_MENU, VK_SHIFT, VK_LWIN, VK_RWIN, VK_CONTROL):
            user32.keybd_event(vk, 0, KEYEVENTF_KEYUP, None)
        user32.keybd_event(VK_CONTROL, 0, 0, None)
        _tap(ord("C"))
        user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, None)
    except Exception as exc:
        log(f"could not synthesise Ctrl+C: {exc!r}")
        return ""

    deadline = time.time() + 0.25
    while time.time() < deadline:
        time.sleep(0.02)
        try:
            if user32.GetClipboardSequenceNumber() != before:
                break
        except Exception:
            return ""
    else:
        # Nothing was selected, or the app ignored us. The clipboard was never
        # touched, so there is nothing to put back.
        return ""

    try:
        grabbed = root.clipboard_get()
    except Exception:
        grabbed = ""

    if old_was_text:
        try:
            root.clipboard_clear()
            root.clipboard_append(old)
        except Exception as exc:
            log(f"could not restore the clipboard: {exc!r}")
    elif grabbed:
        # We overwrote something that was not text and cannot put it back.
        # Saying so is the only honest option.
        log("clipboard held non-text content and was overwritten by the grab")

    return grabbed.strip()


#: A selection this short, on one line, is a term. Anything longer is a
#: passage someone highlighted to keep, and opening it in Term mode would
#: mean retyping it out of the wrong field.
TERM_MAX_WORDS = 6
TERM_MAX_CHARS = 60


def looks_like_a_term(text: str) -> bool:
    t = text.strip()
    return bool(t) and "\n" not in t and len(t) <= TERM_MAX_CHARS \
        and len(t.split()) <= TERM_MAX_WORDS


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

# -- Tcl/Tk lookup ---------------------------------------------------------
# `import tkinter` succeeds without the Tcl runtime; it is tk.Tk() that
# dies, with "Can't find a usable init.tcl". Inside a venv, _tkinter
# derives its search path from sys.prefix (the venv), which has no tcl/
# directory, and the paths it then guesses under the base install are
# lib/tcl8.6 -- while python.org actually ships tcl/tcl8.6. So a perfectly
# good Tcl sits on disk and is never found. Point at it explicitly, from
# sys.base_prefix so this holds inside and outside the venv.
def _fix_tcl_paths() -> None:
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    for var, sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        if os.environ.get(var):
            continue  # respect an explicit override
        for candidate in (base / "tcl" / sub, base / "lib" / sub):
            if (candidate / "init.tcl").exists() or (candidate / "tk.tcl").exists():
                os.environ[var] = str(candidate)
                log(f"{var}={candidate}")
                break
        else:
            log(f"{var}: no Tcl/Tk library directory found under {base}")


_fix_tcl_paths()

import tkinter as tk  # noqa: E402  (after the stdout/stderr redirect above)

_box = {
    "win": None, "entry": None, "defn": None, "defrow": None,
    "hint": None, "banner": None, "mode": "note", "source": "",
    "session": "",
}

_HINTS = {
    "note": "Capture  ·  Enter files it  ·  Tab for a term  ·  Esc cancels",
    "term": "Term  ·  Enter → definition  ·  Enter again files it  ·  Tab back",
}


def _set_mode(mode: str) -> None:
    """Note or Term. The two shapes of thing that get captured while reading,
    and the only difference between them that matters here is that a term has
    a second field — because a term without a definition is a word you wrote
    down, and the moment you are most likely to know the definition is the
    moment you met the word."""
    _box["mode"] = mode
    _box["hint"].configure(text=_HINTS[mode])
    row = _box["defrow"]
    if mode == "term":
        row.pack(fill="x", pady=(6, 0))
    else:
        row.pack_forget()


def show_capture_box(root: tk.Tk) -> None:
    # Before the window is raised, while the other application is still the
    # foreground one — a box that has taken focus has no selection to read.
    #
    # Skipped entirely when a screenshot is waiting: the grab works by sending
    # Ctrl+C, which would overwrite the image on the clipboard with whatever
    # text happened to be selected, and the image cannot be put back. The
    # note would survive (we already hold the bytes) but lj's clipboard would
    # not, and the box is open to caption the picture anyway.
    grabbed = "" if pending_png() else grab_selection(root)
    source = foreground_title()

    win = _box["win"]
    if win is None:
        win = _build_capture_box(root)

    entry, defn = _box["entry"], _box["defn"]
    _box["source"] = source
    entry.delete(0, tk.END)
    defn.delete(0, tk.END)
    if grabbed:
        entry.insert(0, grabbed)
    _set_mode("term" if looks_like_a_term(grabbed) else "note")

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"{w}x{h}+{(sw - w) // 2}+{sh // 3}")
    win.deiconify()
    win.lift()
    win.attributes("-topmost", True)
    win.focus_force()
    entry.focus_set()
    # Selected, not just present: Enter accepts what was highlighted and
    # typing replaces it. Either way it is one keystroke.
    entry.selection_range(0, tk.END)
    entry.icursor(tk.END)

    # Painted from what we knew last time so the box is never waiting on the
    # network to appear, then corrected off-thread. A banner that is one
    # keypress stale is fine; a capture box that takes 200 ms to open is not.
    set_session_banner(_box["session"])
    threading.Thread(
        target=lambda: EVENTS.put(("session", session_label())), daemon=True
    ).start()


def _build_capture_box(root: tk.Tk) -> tk.Toplevel:
    win = tk.Toplevel(root)
    win.title("Second Brain Capture")
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#3a3a3a")

    frame = tk.Frame(win, bg="#3a3a3a", padx=10, pady=8)
    frame.pack(fill="both", expand=True)
    hint = tk.Label(
        frame, text=_HINTS["note"],
        bg="#3a3a3a", fg="#bbbbbb", font=("Segoe UI", 9),
        anchor="w",
    )
    hint.pack(fill="x", pady=(0, 4))
    # Where captures are going right now. Blank and invisible when no session
    # is open, because a permanently-present "no session" line would be one
    # more thing to read past on every capture for the 95% of them that are
    # not in a lecture.
    banner = tk.Label(
        frame, text="", bg="#3a3a3a", fg="#8fb98f", font=("Segoe UI", 9, "bold"),
        anchor="w",
    )
    entry = tk.Entry(frame, width=64, font=("Segoe UI", 13))
    entry.pack(fill="x")

    defrow = tk.Frame(frame, bg="#3a3a3a")
    tk.Label(
        defrow, text="means", bg="#3a3a3a", fg="#8fb98f",
        font=("Segoe UI", 9), anchor="w",
    ).pack(side="left", padx=(0, 6))
    defn = tk.Entry(defrow, width=56, font=("Segoe UI", 12))
    defn.pack(side="left", fill="x", expand=True)

    def close():
        win.withdraw()

    def submit(_event=None):
        mode = _box["mode"]
        text, definition, source = entry.get(), defn.get(), _box["source"]
        close()
        if not text.strip():
            return
        png = pending_png()
        if text.lstrip().startswith("/"):
            threading.Thread(
                target=_run_command, args=(text.strip(),), daemon=True
            ).start()
        elif png:
            # The typed line becomes the caption, which is the title, which
            # is what makes the image findable six weeks later. An image
            # filed under "Screenshot — 14 Sep" is an image nobody finds.
            _pending["png"] = None
            threading.Thread(
                target=_file_pending, args=(png, text, source), daemon=True
            ).start()
        elif mode == "term":
            threading.Thread(
                target=handle_term, args=(text, definition, source), daemon=True
            ).start()
        else:
            threading.Thread(target=handle_capture, args=(text,), daemon=True).start()

    def _file_pending(png, caption, source):
        if file_screenshot(png, caption=caption, source=source):
            beep_ok()
            notify(f"Screenshot filed: {caption[:50]}")
        else:
            beep_fail()
            notify("Screenshot not filed — Second Brain is not running.", error=True)
        EVENTS.put(("pending", False))

    def _run_command(text):
        handle_command(text)
        # The banner is stale the moment a session opens or closes, and this
        # is the only thread that knows it happened.
        EVENTS.put(("session", session_label()))

    def on_term_enter(_event=None):
        """Enter from the term field means "yes, that is the term".

        It moves to the definition rather than filing, because the definition
        is the half that makes the note worth having and asking for it costs
        one keystroke. Pressing Enter again on an empty definition files it
        anyway — an undefined term is still the thing you did not know.
        """
        if not defn.get().strip() and win.focus_get() is not defn:
            defn.focus_set()
            return "break"
        return submit()

    def _drop_pending(_event=None):
        """Keep the note, throw away the picture. A screenshot offered is not
        a screenshot wanted, and having to cancel the whole capture to say so
        would make the offer a nuisance."""
        _pending["png"] = None
        set_session_banner()
        return "break"

    def toggle_mode(_event=None):
        _set_mode("note" if _box["mode"] == "term" else "term")
        # Focus stays on the first field either way: switching mode is a
        # correction of what the thing *is*, not a jump to a different part
        # of it, and the text already typed is still the term or the note.
        entry.focus_set()
        return "break"  # Tab must not walk the focus ring as well

    for widget in (entry, defn, win):
        widget.bind("<Escape>", lambda _e: close())
        widget.bind("<Tab>", toggle_mode)
        # Ctrl+Enter files from wherever the cursor is, including a
        # half-typed definition.
        widget.bind("<Control-Return>", submit)
        widget.bind("<Control-d>", _drop_pending)
    entry.bind("<Return>", lambda e: on_term_enter(e) if _box["mode"] == "term" else submit(e))
    entry.bind("<KP_Enter>", lambda e: on_term_enter(e) if _box["mode"] == "term" else submit(e))
    defn.bind("<Return>", submit)
    defn.bind("<KP_Enter>", submit)
    win.protocol("WM_DELETE_WINDOW", lambda: close())

    _box.update({"win": win, "entry": entry, "defn": defn, "defrow": defrow,
                 "hint": hint, "banner": banner})
    return win


def set_session_banner(label: str = None) -> None:
    """One line above the box saying where this capture is going, and whether
    a screenshot is riding along. Hidden when neither is true — a permanent
    "no session, no screenshot" line is one more thing to read past on every
    capture, for the majority of captures that are neither."""
    if label is not None:
        _box["session"] = label
    banner = _box.get("banner")
    if banner is None:
        return
    bits = []
    if _box["session"]:
        bits.append(f"→ {_box['session']}")
    if pending_png():
        bits.append("📎 screenshot attached  (Ctrl+D to drop it)")
    if bits:
        banner.configure(text="   ".join(bits))
        banner.pack(fill="x", pady=(0, 4), before=_box["entry"])
    else:
        banner.pack_forget()


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
            elif item[0] == "session":
                set_session_banner(item[1])
            elif item[0] == "pending":
                # Only repaints the box; nothing pops up. A snip should not
                # steal focus from whatever it was taken out of.
                set_session_banner()
    except queue.Empty:
        pass
    root.after(80, poll_events, root)


def main() -> int:
    log("=== capture_hotkey starting ===")
    if not ensure_single_instance():
        log("another instance already holds the mutex; exiting")
        return 0

    threading.Thread(target=hotkey_thread, daemon=True).start()
    threading.Thread(target=watch_clipboard, daemon=True).start()

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
