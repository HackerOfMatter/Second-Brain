"""When to interrupt someone about a study session, and when not to.

Story H3. The tutor already puts a daily study block on the calendar, which is
a reminder lj sees only if the calendar happens to be open. A desktop
notification is the one channel that reaches them while they are doing
something else — which is exactly why it has to be right. A notification that
fires when there is nothing due, or fires again ten minutes after being
dismissed, gets muted, and a muted channel is worse than no channel: it takes
the honest reminders down with it.

So the decision is a pure function over four facts, and it lives here rather
than in the resident process, because a decision inside a `.pyw` that only
runs under `pythonw.exe` on Windows is a decision no test can reach.

**Due** means two things at once, and both are checked:

  * the study block has started — `study.study_time` has passed today. Before
    that the session is not late, it is simply not yet.
  * there is something in it — at least one card is actually due. A block on
    the calendar with an empty queue behind it is not a session lj is missing.

**Unstarted** means no answer has been logged today (`_decks/_reviews.jsonl`,
via `tutor.counted_today`). Not "the app is closed", not "the study page was
not opened" — a card actually graded is the only evidence that the session
began, and it is evidence that survives the app being closed and reopened.

Three guards then decide whether to speak, in this order:

  1. **snoozed** — an explicit "not now" is honoured to the minute.
  2. **never twice in an hour** — a hard floor under everything else,
     including a snooze set shorter than an hour.
  3. **once a day** — having fired and not been snoozed, it does not fire
     again today. A second identical notification says nothing the first did
     not.

State on disk, not in memory
---------------------------
`_system/study-reminder.json` holds `fired_at` and `snoozed_until`, written
through `sb/atomic.py` the moment either happens. That file is the whole
reason a restart cannot double-fire: a resident process that kept this in
memory would forget it had spoken every time it was restarted — at logon, or
after a crash, or because lj ran the installer again — and the failure mode
would be a notification storm on exactly the day something else went wrong.
Reading the file at every tick, rather than caching it, also means a snooze
taken in one process is respected by another.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from . import atomic

FILENAME = "study-reminder.json"

#: "Never twice in an hour", as a number. A floor under every other rule.
MIN_GAP = dt.timedelta(hours=1)

#: What the Snooze button asks for. Long enough to finish whatever
#: interrupted, short enough that the session is still today.
DEFAULT_SNOOZE_MINUTES = 30

#: The same fallback events.py uses when `study.study_time` is unparseable, so
#: the notification and the calendar block never disagree about when the
#: session starts.
DEFAULT_STUDY_TIME = dt.time(19, 30)


def _local(value: Optional[dt.datetime]) -> Optional[dt.datetime]:
    """Aware local time. A naive value on disk is read as local rather than
    rejected — an older state file, or one edited by hand, should cost a
    guess, not a crash."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.astimezone()
    return value


def now() -> dt.datetime:
    return dt.datetime.now().astimezone().replace(microsecond=0)


def study_time(cfg) -> dt.time:
    try:
        return dt.time.fromisoformat(cfg.study.study_time)
    except (ValueError, TypeError, AttributeError):
        return DEFAULT_STUDY_TIME


def state_path(cfg) -> Path:
    return cfg.system_dir / FILENAME


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------


@dataclass
class ReminderState:
    """What has already been said, and what lj asked for instead."""

    fired_at: Optional[dt.datetime] = None
    snoozed_until: Optional[dt.datetime] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "fired_at": self.fired_at.isoformat() if self.fired_at else None,
            "snoozed_until": (
                self.snoozed_until.isoformat() if self.snoozed_until else None
            ),
        }

    @property
    def snoozed_since_firing(self) -> bool:
        """Did lj ask to be reminded again *after* the last time it spoke?

        This is what re-arms a reminder that has already fired today. Without
        it, Snooze would set a wake-up time that the once-a-day rule then
        ignored — a button that does nothing.
        """
        if self.snoozed_until is None:
            return False
        if self.fired_at is None:
            return True
        return self.snoozed_until > self.fired_at


def _parse(value: Any) -> Optional[dt.datetime]:
    if not value:
        return None
    try:
        return _local(dt.datetime.fromisoformat(str(value)))
    except (ValueError, TypeError):
        return None


def read_state(path: Path) -> ReminderState:
    """The state on disk, or a blank one.

    Never raises. A missing file is the normal first-run case; a corrupt one
    is a bad shutdown, and the safe reading of "I cannot tell whether I have
    spoken" is "I have not" — the alternative is a reminder that goes silent
    permanently because of one torn write.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ReminderState()
    if not isinstance(raw, dict):
        return ReminderState()
    return ReminderState(
        fired_at=_parse(raw.get("fired_at")),
        snoozed_until=_parse(raw.get("snoozed_until")),
    )


def write_state(path: Path, state: ReminderState) -> ReminderState:
    """Persist atomically. The caller has just fired or just snoozed, and a
    half-written file here is precisely the double-fire this module exists to
    prevent."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.tmp"
    tmp.write_text(json.dumps(state.as_dict(), indent=2) + "\n", encoding="utf-8")
    atomic.replace(tmp, path)
    return state


def record_fired(path: Path, at: Optional[dt.datetime] = None) -> ReminderState:
    """Write "it spoke" *before* the notification is shown, not after — a
    crash while the toast is on screen must not un-remember it."""
    state = read_state(path)
    state.fired_at = _local(at) or now()
    return write_state(path, state)


def snooze(
    path: Path,
    minutes: int = DEFAULT_SNOOZE_MINUTES,
    at: Optional[dt.datetime] = None,
) -> ReminderState:
    state = read_state(path)
    state.snoozed_until = (_local(at) or now()) + dt.timedelta(
        minutes=max(1, int(minutes))
    )
    return write_state(path, state)


def dismiss(path: Path, at: Optional[dt.datetime] = None) -> ReminderState:
    """Dismiss is "not again today", so it clears any snooze rather than
    leaving one behind to fire later and contradict the button just pressed."""
    state = read_state(path)
    state.fired_at = _local(at) or now()
    state.snoozed_until = None
    return write_state(path, state)


# --------------------------------------------------------------------------
# the decision
# --------------------------------------------------------------------------


@dataclass
class Decision:
    fire: bool
    reason: str

    def as_dict(self) -> Dict[str, Any]:
        return {"fire": self.fire, "reason": self.reason}


def decide(
    *,
    at: dt.datetime,
    cards_due: int,
    reviewed_today: int,
    starts_at: dt.time,
    state: ReminderState,
) -> Decision:
    """Whether to interrupt, and the evidence for the answer either way.

    `reason` is filled in on both branches on purpose. "It did not fire" is
    the answer lj will want explained — from the log, or from
    `python run.py study-reminder` — and "quiet" is not an explanation.
    """
    at = _local(at) or now()

    if cards_due <= 0:
        return Decision(False, "nothing is due")
    if reviewed_today > 0:
        return Decision(False, f"already started — {reviewed_today} answered today")
    if at.time() < starts_at:
        return Decision(
            False, f"the study block has not started yet ({starts_at:%H:%M})"
        )

    if state.snoozed_until and at < state.snoozed_until:
        return Decision(False, f"snoozed until {state.snoozed_until:%H:%M}")

    if state.fired_at:
        since = at - state.fired_at
        if since < MIN_GAP:
            mins = max(0, int(since.total_seconds() // 60))
            return Decision(False, f"fired {mins} min ago — never twice in an hour")
        if state.fired_at.date() == at.date() and not state.snoozed_since_firing:
            return Decision(False, f"already fired today at {state.fired_at:%H:%M}")

    return Decision(
        True,
        f"{cards_due} card{'s' if cards_due != 1 else ''} due and none answered today",
    )


__all__ = [
    "DEFAULT_SNOOZE_MINUTES",
    "Decision",
    "MIN_GAP",
    "ReminderState",
    "decide",
    "dismiss",
    "read_state",
    "record_fired",
    "snooze",
    "state_path",
    "study_time",
    "write_state",
]
