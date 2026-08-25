"""Calendar sinks.

Google is the blueprint's notification channel (§3): work blocks, recurring
areas, reviews and due dates all surface there rather than through a separate
push system. That is the destination — but OAuth setup is a wall to put in
front of a system you want to be using this week, so there are sinks behind one
interface:

  * `ics`     writes a standards-compliant .ics into the vault. Works with no
              accounts, no network, no consent screen. Import or subscribe to
              it from Google Calendar, Outlook or Apple Calendar. Carries both
              events (VEVENT) and due tasks (VTODO).
  * `google`  pushes and updates *events* directly via the Calendar API.
  * `gtasks`  pushes *due dates* to Google Tasks, which is where a due date
              belongs — it shows in Calendar's Tasks strip and stays until
              ticked. Attached automatically when `calendar.task_sink` asks
              for Google; there is no reason to select it by hand.

`both` runs the ics and Google pair. Switching is a one-line config change, and
no sink holds state: items are always regenerated from the vault, which stays
the single source of truth.
"""

from __future__ import annotations

from typing import List, Protocol

from ..config import Config
from ..models import Note
from .events import CalEvent, CalTask, events_for_vault, tasks_for_vault


class CalendarSink(Protocol):
    name: str

    def sync(self, notes: List[Note], cfg: Config, decks: int | None = None) -> dict: ...


def get_sink(cfg: Config):
    """Build the sink chain from `calendar.sink` (events) and
    `calendar.task_sink` (due dates), which move independently — you can keep
    events local while pushing tasks to Google, or the reverse."""
    sink = (cfg.calendar.sink or "ics").lower()
    task_sink = cfg.resolved_task_sink()

    sinks = []
    if sink in ("ics", "both"):
        from .ics import IcsSink

        sinks.append(IcsSink())
    if sink in ("google", "both"):
        from .google import GoogleSink

        sinks.append(GoogleSink())
    if not sinks:
        raise ValueError(f"unknown calendar sink {cfg.calendar.sink!r}")

    if task_sink in ("google", "both"):
        from .gtasks import GoogleTasksSink

        sinks.append(GoogleTasksSink())

    if len(sinks) == 1:
        return sinks[0]
    return _MultiSink(sinks, name=sink)


class _MultiSink:
    def __init__(self, sinks, name: str = "both"):
        self.sinks = sinks
        self.name = name

    def sync(self, notes, cfg, decks=None):
        out = {}
        for s in self.sinks:
            try:
                out[s.name] = s.sync(notes, cfg, decks)
            except Exception as exc:
                out[s.name] = {"error": f"{type(exc).__name__}: {exc}"}
        return out


# kept under the old name so nothing that imported it breaks
_BothSink = _MultiSink


__all__ = [
    "CalEvent",
    "CalTask",
    "CalendarSink",
    "events_for_vault",
    "tasks_for_vault",
    "get_sink",
]
