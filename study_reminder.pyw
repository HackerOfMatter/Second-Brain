"""Second Brain -- desktop reminder for an unstarted study session (Story H3).

Resident, like capture_hotkey.pyw and for the same reason: the thing being
watched for is a *time arriving*, and a process spawned to check once has
already missed every minute it was not running. Started at logon by
install-study-reminder.bat, it wakes every minute, asks whether a study
session is genuinely due and genuinely unstarted, and shows a toast with
three buttons -- Study now, Snooze 30 min, Not today -- the first time the
answer is yes.

It is deliberately quiet. The whole decision lives in sb/reminders.py, which
the test suite covers directly, and the summary of it is:

  * due    = `study.study_time` has passed today AND at least one card is
             actually due. A block on the calendar with an empty queue behind
             it is not a session anyone is missing.
  * started= at least one answer logged in `_decks/_reviews.jsonl` today. A
             graded card is the only honest evidence that the session began.
  * quiet  = snoozed, or already fired within the hour, or already fired
             today without being snoozed since.

Two sources for those numbers, in this order:

  1. `GET http://127.0.0.1:8787/api/study/reminder` -- the running app, which
     has the decks in memory already.
  2. Failing that (app closed, which is the normal evening case), the deck
     store is read straight off disk in this process. This is the same
     shape as capture_hotkey.pyw's API-then-Drop fallback: the feature must
     not depend on the app being open, because the app being closed is
     exactly when a reminder is worth anything.

"Fired at" and "snoozed until" live in `_system/study-reminder.json`, written
before the toast is drawn. That is what makes a restart safe: this process is
restarted at every logon, and an in-memory flag would have it announce the
same session again each time.

Runs under pythonw.exe (no console window). tkinter for the toast, the
standard library for everything else -- no pywin32, no requests, no toast
package. The one non-stdlib dependency is `sb` itself, which is the code
sitting next to this file.
"""

from __future__ import annotations

import ctypes
import json
import os
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent


# -- pythonw.exe safety: sys.stdout/stderr are None under it, so redirect
#    before anything can print or traceback. Same log directory the capture
#    hotkey uses, for the same reason: doctor and anyone debugging look in
#    _system/logs, not in LOCALAPPDATA. ---------------------------------------

def _open_log():
    for base in (
        APP_DIR / "_system" / "logs",
        Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "secondbrain",
    ):
        try:
            base.mkdir(parents=True, exist_ok=True)
            return open(base / "study-reminder.log", "a", buffering=1, encoding="utf-8")
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


sys.path.insert(0, str(APP_DIR))

import datetime as dt  # noqa: E402

from sb import reminders  # noqa: E402
from sb.cards import DeckStore  # noqa: E402
from sb.config import load as load_config  # noqa: E402
from sb.incidents import IncidentStore  # noqa: E402
from sb.tutor import counted_today  # noqa: E402

CFG = load_config()
STATE = reminders.state_path(CFG)
API_URL = f"http://{CFG.host}:{CFG.port}/api/study/reminder"
API_TIMEOUT_SECONDS = 2.0
STUDY_URL = f"http://{CFG.host}:{CFG.port}/study"

#: One tick a minute. The reminder is accurate to the minute and costs a
#: localhost GET (or, with the app closed, one pass over the deck files);
#: anything finer would be spending battery to be early.
POLL_SECONDS = 60

INCIDENTS = IncidentStore(CFG.system_dir / "logs")
INCIDENT_KIND = "study"
INCIDENT_KEY = "reminder"


# -- facts: the app first, the vault second ---------------------------------


def _from_api():
    req = urllib.request.Request(API_URL, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=API_TIMEOUT_SECONDS) as resp:
        if not 200 <= resp.status < 300:
            raise OSError(f"HTTP {resp.status}")
        payload = json.loads(resp.read().decode("utf-8"))
    data = payload.get("data", payload)
    return int(data["cards_due"]), int(data["reviewed_today"])


def _from_vault():
    store = DeckStore(CFG.vault)
    today = dt.date.today()
    cards_due = sum(len(d.due(today)) for d in store.all())
    reviewed, _ = counted_today(store, today)
    return cards_due, reviewed


def facts():
    """(cards_due, reviewed_today, source), or None if neither path worked."""
    try:
        due, reviewed = _from_api()
        return due, reviewed, "api"
    except Exception as exc:
        log(f"app not reachable ({type(exc).__name__}), reading the vault instead")
    try:
        due, reviewed = _from_vault()
        return due, reviewed, "vault"
    except Exception as exc:
        log(f"VAULT READ FAILED: {type(exc).__name__}: {exc}")
        return None


# -- the tick ----------------------------------------------------------------


def tick(root) -> None:
    try:
        got = facts()
        if got is None:
            # Both paths gone means the reminder is not firing and nobody
            # would otherwise know -- which is the definition of an incident
            # rather than a log line. See sb/incidents.py.
            _record_incident("Study reminders cannot read the deck store.")
            return
        cards_due, reviewed, source = got
        _clear_incident()

        state = reminders.read_state(STATE)
        decision = reminders.decide(
            at=reminders.now(),
            cards_due=cards_due,
            reviewed_today=reviewed,
            starts_at=reminders.study_time(CFG),
            state=state,
        )
        if not decision.fire:
            return
        # Persist BEFORE drawing: if this process dies with the toast on
        # screen, the session must still count as announced.
        reminders.record_fired(STATE)
        log(f"firing via {source}: {decision.reason}")
        show_toast(root, cards_due)
    except Exception as exc:
        log(f"tick failed: {type(exc).__name__}: {exc}")
    finally:
        root.after(POLL_SECONDS * 1000, tick, root)


def _record_incident(message: str) -> None:
    try:
        INCIDENTS.record(
            INCIDENT_KIND,
            message,
            hint="Check _system/logs/study-reminder.log.",
            key=INCIDENT_KEY,
        )
    except Exception:
        pass


def _clear_incident() -> None:
    try:
        INCIDENTS.clear(INCIDENT_KIND, INCIDENT_KEY)
    except Exception:
        pass


# -- the toast ---------------------------------------------------------------
# Same Tcl/Tk path fix capture_hotkey.pyw needs: inside a venv, _tkinter
# guesses lib/tcl8.6 under the base install while python.org ships tcl/tcl8.6,
# so a perfectly good Tcl sits on disk and is never found.

def _fix_tcl_paths() -> None:
    base = Path(getattr(sys, "base_prefix", sys.prefix))
    for var, sub in (("TCL_LIBRARY", "tcl8.6"), ("TK_LIBRARY", "tk8.6")):
        if os.environ.get(var):
            continue
        for candidate in (base / "tcl" / sub, base / "lib" / sub):
            if (candidate / "init.tcl").exists() or (candidate / "tk.tcl").exists():
                os.environ[var] = str(candidate)
                break


_fix_tcl_paths()

import tkinter as tk  # noqa: E402


def show_toast(root, cards_due: int) -> None:
    win = tk.Toplevel(root)
    win.title("Second Brain")
    win.overrideredirect(True)
    win.attributes("-topmost", True)
    win.configure(bg="#1f3550")

    frame = tk.Frame(win, bg="#1f3550", padx=16, pady=12)
    frame.pack(fill="both", expand=True)
    plural = "s" if cards_due != 1 else ""
    tk.Label(
        frame, text="Study session due", bg="#1f3550", fg="#ffffff",
        font=("Segoe UI", 11, "bold"), anchor="w",
    ).pack(fill="x")
    tk.Label(
        frame,
        text=f"{cards_due} card{plural} waiting, none answered today.",
        bg="#1f3550", fg="#cfe0f5", font=("Segoe UI", 10), anchor="w",
    ).pack(fill="x", pady=(2, 10))

    row = tk.Frame(frame, bg="#1f3550")
    row.pack(fill="x")

    def close():
        try:
            win.destroy()
        except tk.TclError:
            pass

    def study():
        close()
        try:
            webbrowser.open(STUDY_URL)
        except Exception as exc:
            log(f"could not open {STUDY_URL}: {exc!r}")

    def later():
        close()
        state = reminders.snooze(STATE, reminders.DEFAULT_SNOOZE_MINUTES)
        log(f"snoozed until {state.snoozed_until}")

    def not_today():
        close()
        reminders.dismiss(STATE)
        log("dismissed for today")

    for text, command, bg in (
        ("Study now", study, "#2e7d43"),
        (f"Snooze {reminders.DEFAULT_SNOOZE_MINUTES} min", later, "#3d5a7a"),
        ("Not today", not_today, "#3d5a7a"),
    ):
        tk.Button(
            row, text=text, command=command, bg=bg, fg="#ffffff",
            activebackground=bg, relief="flat", font=("Segoe UI", 9),
            padx=10, pady=4, borderwidth=0,
        ).pack(side="left", padx=(0, 6))

    win.update_idletasks()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    win.geometry(f"+{sw - w - 24}+{sh - h - 64}")
    win.lift()

    # No auto-dismiss timer. Unlike the capture toast, which reports something
    # that already happened, this one asks a question -- closing it after two
    # seconds would answer it on lj's behalf with the one answer they cannot
    # undo. It stays until a button is pressed; the once-an-hour and
    # once-a-day rules mean an ignored toast still cannot become a second one.
    try:
        import winsound
        winsound.MessageBeep(winsound.MB_ICONASTERISK)
    except Exception:
        pass


# -- single instance ---------------------------------------------------------

ERROR_ALREADY_EXISTS = 183


def ensure_single_instance() -> bool:
    """Two copies would each read the state file, and the second would find
    `fired_at` already written and stay quiet -- so a double-launch is not
    harmful here, only wasteful. The mutex makes it a clean no-op anyway, and
    matches what the capture hotkey does."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        kernel32.CreateMutexW(None, False, "SecondBrainStudyReminderMutex")
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception:
        return True  # not Windows, or no kernel32: do not refuse to run


def main() -> int:
    log("=== study_reminder starting ===")
    if not ensure_single_instance():
        log("another instance already holds the mutex; exiting")
        return 0
    log(f"vault={CFG.vault}  study_time={CFG.study.study_time}  state={STATE}")

    root = tk.Tk()
    root.withdraw()
    # First tick immediately: a logon at 21:00 should not wait a minute to
    # notice that the 19:30 session never happened.
    root.after(1000, tick, root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        log("FATAL:\n" + traceback.format_exc())
        raise
