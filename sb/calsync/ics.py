"""RFC 5545 .ics writer.

Hand-rolled rather than pulled from a library: the subset needed here is
small, and a personal system with fewer moving parts is a personal system that
still runs in two years. Handles the three things naive .ics writers get wrong
— text escaping, 75-octet line folding, and the fact that a due date is a
VTODO, not an all-day VEVENT.

Colour rides along as RFC 7986 `COLOR:` (a CSS3 name) plus a `CATEGORIES:`
line. Clients that understand COLOR paint the item; clients that don't still
get a category they can filter or rule on, and the emoji in the summary works
everywhere.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import List

from .. import taxonomy
from ..config import Config
from ..models import Note
from .events import CalEvent, CalTask, events_for_vault, tasks_for_vault

PRODID = "-//Second Brain//PARA Engine//EN"


class IcsSink:
    name = "ics"

    def sync(self, notes: List[Note], cfg: Config, decks: int | None = None) -> dict:
        events = events_for_vault(notes, cfg, decks)
        tasks = (
            tasks_for_vault(notes, cfg)
            if cfg.resolved_task_sink() in ("ics", "both")
            else []
        )
        path = cfg.ics_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render(events, cfg, tasks), encoding="utf-8")
        return {"events": len(events), "tasks": len(tasks), "path": str(path)}


def render(
    events: List[CalEvent], cfg: Config, tasks: List[CalTask] | None = None
) -> str:
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Second Brain",
        "X-WR-CALDESC:Work blocks\\, recurring areas\\, reviews and due tasks",
    ]
    stamp = _utc(dt.datetime.now(dt.timezone.utc))
    for ev in events:
        lines += _render_event(ev, stamp, cfg)
    for task in tasks or []:
        lines += _render_todo(task, stamp, cfg)
    lines.append("END:VCALENDAR")
    return "\r\n".join(_fold(line) for line in lines) + "\r\n"


def _render_event(ev: CalEvent, stamp: str, cfg: Config) -> List[str]:
    out = ["BEGIN:VEVENT", f"UID:{ev.uid}", f"DTSTAMP:{stamp}"]
    if ev.all_day:
        start = ev.start if isinstance(ev.start, dt.date) else ev.start.date()
        end = start + dt.timedelta(days=1)
        out.append(f"DTSTART;VALUE=DATE:{start:%Y%m%d}")
        out.append(f"DTEND;VALUE=DATE:{end:%Y%m%d}")
    else:
        out.append(f"DTSTART:{_local(ev.start)}")
        out.append(f"DTEND:{_local(ev.end_or_default)}")
    if ev.rrule:
        out.append(f"RRULE:{ev.rrule}")
    out.append(f"SUMMARY:{_esc(ev.summary)}")
    if ev.description:
        out.append(f"DESCRIPTION:{_esc(ev.description)}")
    out += _colour_lines(ev.kind, ev.category, cfg)
    out.append(f"X-SB-NOTE-ID:{ev.note_id}")
    out.append("TRANSP:" + ("TRANSPARENT" if ev.all_day else "OPAQUE"))
    out += _alarms(ev.reminders, ev.summary)
    out.append("END:VEVENT")
    return out


def _render_todo(task: CalTask, stamp: str, cfg: Config) -> List[str]:
    """A Project deadline. VTODO rather than VEVENT because a due date is not
    an appointment — it has no duration, it can be completed, and it should
    stay visible until it is."""
    out = [
        "BEGIN:VTODO",
        f"UID:{task.uid}",
        f"DTSTAMP:{stamp}",
        f"DUE;VALUE=DATE:{task.due:%Y%m%d}",
        f"SUMMARY:{_esc(task.summary)}",
    ]
    if task.description:
        out.append(f"DESCRIPTION:{_esc(task.description)}")
    out += _colour_lines("due", task.category, cfg)
    out.append(f"PRIORITY:{task.priority}")
    out.append("STATUS:NEEDS-ACTION")
    if task.percent:
        out.append(f"PERCENT-COMPLETE:{min(100, max(0, task.percent))}")
    out.append(f"X-SB-NOTE-ID:{task.note_id}")
    # A VTODO has no DTSTART, so alarms hang off the due date instead.
    for minutes in task.reminders:
        out += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_esc('Due: ' + task.summary)}",
            f"TRIGGER;RELATED=END:-PT{max(0, int(minutes))}M",
            "END:VALARM",
        ]
    out.append("END:VTODO")
    return out


def _colour_lines(kind: str, category: str, cfg: Config) -> List[str]:
    cat = taxonomy.get(category, cfg)
    out = [f"CATEGORIES:{kind.upper()},{cat.key.upper()}"]
    if cfg.calendar.color_events:
        # RFC 7986 §5.9 — CSS3 colour name.
        out.append(f"COLOR:{cat.color}")
        # Apple Calendar reads its own property; harmless elsewhere.
        out.append(f"X-APPLE-CALENDAR-COLOR:{cat.hex}")
    out.append(f"X-SB-CATEGORY:{cat.key}")
    return out


def _alarms(reminders: List[int], summary: str) -> List[str]:
    out: List[str] = []
    for minutes in reminders:
        out += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_esc(summary)}",
            f"TRIGGER:-PT{max(0, int(minutes))}M",
            "END:VALARM",
        ]
    return out


def _local(value) -> str:
    """Floating local time (no TZID): the vault is single-user and single-
    machine, so local wall-clock is the honest representation."""
    if isinstance(value, dt.datetime):
        return value.strftime("%Y%m%dT%H%M%S")
    return dt.datetime.combine(value, dt.time(9, 0)).strftime("%Y%m%dT%H%M%S")


def _utc(value: dt.datetime) -> str:
    return value.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _esc(text: str) -> str:
    return (
        str(text)
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1: fold at 75 octets, continuation lines start with a space."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, cursor = [], 0
    limit = 74
    while cursor < len(raw):
        end = min(cursor + limit, len(raw))
        # do not split a multi-byte character
        while end > cursor and (raw[end - 1] & 0xC0) == 0x80:
            end -= 1
        chunks.append(raw[cursor:end].decode("utf-8"))
        cursor = end
        limit = 73
    return "\r\n ".join(chunks)


def read_path(cfg: Config) -> Path:
    return cfg.ics_path
