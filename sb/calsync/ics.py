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
    head = "\r\n".join(lines) + "\r\n"

    # Every capture re-renders the whole calendar, and almost every event in
    # it is the same as last time. Each event's folded text is cached against
    # its own fields with the DTSTAMP left as a placeholder, so an unchanged
    # event costs a dict lookup rather than ~20 formatted, escaped, folded
    # lines. The cache is rebuilt from what this render used, so it never
    # outgrows the calendar, and it is dropped when the colour config moves.
    global _CACHE_CFG, _EVENT_CACHE
    cfg_key = (cfg.calendar.color_events, taxonomy._register(cfg))
    old = _EVENT_CACHE if cfg_key == _CACHE_CFG else {}
    fresh: dict = {}
    colours: dict = {}
    parts = [head]
    for ev in events:
        key = (
            ev.uid, ev.summary, ev.start, ev.end, ev.all_day, ev.description,
            tuple(ev.reminders), ev.kind, ev.note_id, ev.category, ev.rrule, ev.cue,
        )
        block = old.get(key)
        if block is None:
            block = _join(_render_event(ev, _STAMP, cfg, colours))
        fresh[key] = block
        parts.append(block)
    _CACHE_CFG, _EVENT_CACHE = cfg_key, fresh
    for task in tasks or []:
        parts.append(_join(_render_todo(task, _STAMP, cfg, colours)))
    parts.append("END:VCALENDAR\r\n")
    return "".join(parts).replace(_STAMP, stamp)


#: Stands in for the DTSTAMP inside cached event text. Not valid .ics on its
#: own, so it can never be confused with anything a note produced.
_STAMP = "\x00STAMP\x00"
_CACHE_CFG: tuple = ()
_EVENT_CACHE: dict = {}


def _join(lines: List[str]) -> str:
    return "".join(
        (line if len(line) <= 75 and line.isascii() else _fold(line)) + "\r\n"
        for line in lines
    )


def _render_event(ev: CalEvent, stamp: str, cfg: Config, colours: dict | None = None) -> List[str]:
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
    out += _colour_lines(ev.kind, ev.category, cfg, colours)
    out.append(f"X-SB-NOTE-ID:{ev.note_id}")
    out.append("TRANSP:" + ("TRANSPARENT" if ev.all_day else "OPAQUE"))
    out += _alarms(ev.reminders, ev.summary, ev.cue)
    out.append("END:VEVENT")
    return out


def _render_todo(task: CalTask, stamp: str, cfg: Config, colours: dict | None = None) -> List[str]:
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
    out += _colour_lines("due", task.category, cfg, colours)
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
            f"DESCRIPTION:{_esc(_alarm_text('Due: ' + task.summary, task.cue))}",
            f"TRIGGER;RELATED=END:-PT{max(0, int(minutes))}M",
            "END:VALARM",
        ]
    out.append("END:VTODO")
    return out


def _colour_lines(kind: str, category: str, cfg: Config, memo: dict | None = None) -> List[str]:
    if memo is not None:
        hit = memo.get((kind, category))
        if hit is None:
            hit = memo[(kind, category)] = _colour_lines(kind, category, cfg)
        return hit
    cat = taxonomy.get(category, cfg)
    out = [f"CATEGORIES:{kind.upper()},{cat.key.upper()}"]
    if cfg.calendar.color_events:
        # RFC 7986 §5.9 — CSS3 colour name.
        out.append(f"COLOR:{cat.color}")
        # Apple Calendar reads its own property; harmless elsewhere.
        out.append(f"X-APPLE-CALENDAR-COLOR:{cat.hex}")
    out.append(f"X-SB-CATEGORY:{cat.key}")
    return out


def _alarm_text(summary: str, cue: str = "") -> str:
    """What the popup actually says.

    The VALARM DESCRIPTION *is* the notification on most clients — the event
    body is one tap further away, and a reminder that fires while lj is doing
    something else is read once, in full, or not at all. So the implementation
    intention rides in the alarm itself rather than only in the event it hangs
    off. Empty cue leaves the old title-only text exactly as it was: there is
    nothing to add, and a blank line is not information.
    """
    return f"{summary}\n{cue}" if (cue or "").strip() else summary


def _alarms(reminders: List[int], summary: str, cue: str = "") -> List[str]:
    text = _alarm_text(summary, cue)
    out: List[str] = []
    for minutes in reminders:
        out += [
            "BEGIN:VALARM",
            "ACTION:DISPLAY",
            f"DESCRIPTION:{_esc(text)}",
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
        .replace("\r\n", "\n")
        .replace("\r", "\n")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def _fold(line: str) -> str:
    """RFC 5545 §3.1: fold at 75 octets, continuation lines start with a space."""
    # Almost every line is short ASCII, where the character count *is* the
    # octet count. `str.isascii()` reads a flag CPython already keeps, so this
    # skips an encode() per line — and a full calendar render folds tens of
    # thousands of lines.
    if len(line) <= 75 and line.isascii():
        return line
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks, cursor, total = [], 0, len(raw)
    limit = 74
    while cursor < total:
        end = min(cursor + limit, total)
        # Do not split a multi-byte character. The test is on the byte *after*
        # the cut: `end` is a character boundary exactly when it is the end of
        # the string, or the byte sitting there is not a continuation byte
        # (0b10xxxxxx).
        #
        # Backing off over continuation bytes instead — testing raw[end - 1] —
        # stops on the character's *lead* byte and leaves it stranded at the end
        # of the chunk, so `.decode()` raises "unexpected end of data". Every
        # emoji this system prefixes onto a summary (📝 💚 💼) is three or four
        # bytes, so a title long enough to fold crashed the entire calendar
        # sync whenever the emoji happened to land on the boundary.
        while cursor < end < total and (raw[end] & 0xC0) == 0x80:
            end -= 1
        if end <= cursor:  # unreachable for UTF-8 (4 bytes max), but never spin
            end = min(cursor + limit, total)
            chunks.append(raw[cursor:end].decode("utf-8", "replace"))
        else:
            chunks.append(raw[cursor:end].decode("utf-8"))
        cursor = end
        limit = 73
    return "\r\n ".join(chunks)


def read_path(cfg: Config) -> Path:
    return cfg.ics_path
