"""F3' (Sprint 2 spike): "what's going on today", assembled and rendered.

lj's replacement for the phone-capture story is outbound, not inbound: text
the system (or have it text first) and get back today's schedule. The
transport is genuinely undecided — see the spike's decision doc — so this
module is deliberately transport-free. It does two things:

  * `build()` — walk the vault once and produce one `TodayDigest`, reusing
    the execution queue (`workflow.next_actions`), the Area occurrence
    calculator (`calsync.events.occurrences`) and the Gollwitzer sentence
    (`habits.intention_sentence`) rather than re-deriving any of them.
  * `render_text()` / `render_long()` — two views of that one structure. The
    long form is for email or a dashboard panel and has no length limit. The
    short form is for SMS and is capped at 320 characters — two GSM-7
    segments — because a message that silently splits into a third segment
    on a bad day is a message whose cost changes without anyone deciding it
    should.

Why this priority order under the 320-char cap
------------------------------------------------
On a normal day everything fits and the order does not matter. It matters on
the day it does not fit — 40 cards due, three active Projects, a full
calendar — and the cut has to be decided in advance, not by truncation
falling wherever it lands:

  1. **What's on the calendar today, first.** A scheduled block or a habit
     time cannot be renegotiated by working faster later; missing it is a
     real-world conflict, not a slipped estimate. This is the one category
     that is urgent by the clock rather than by importance, so it goes first
     and, unlike everything below it, degrades to a count only after three
     items rather than after one.
  2. **The single highest-urgency next step, and only one.** The execution
     queue (`workflow.next_actions`) already ranks every active Project's
     next step by urgency — showing three of them in a two-segment text
     recreates the "queue that shows six things from the same project is a
     list" problem workflow.py already rejected for the dashboard. One
     project's one step, or nothing.
  3. **Everything else becomes a bare count.** 40 cards due is not 40 lines
     of anything in a channel you cannot scroll; it is one integer that says
     "there is a session waiting" and points at the app for the rest. The
     same goes for habits and the inbox. A count that reads 0 is dropped
     entirely rather than printed, per the empty-day rule below.

Nothing here is ever traded for something later in this list to fit more of
something earlier: the calendar line does not grow past three items to make
room for a longer next-step description, and the next step is never dropped
to fit a fourth count in. When the hard cap is still exceeded regardless —
a pathological title, say — the message is sliced with an ellipsis as a last
resort, never silently by SMS transport.

The empty day
-------------
Right now there are 0 decks and 0 reviews, so "cards due today: 0" is a true
and useless sentence, and printing it above three other empty headers reads
as a broken feature rather than a quiet day. Both renderers check for the
all-empty case first and say so in one line instead.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from . import habits as habitsmod
from . import workflow
from .calsync import events as calevents
from .config import Config
from .models import Bucket, Note

#: SMS: two GSM-7 segments (153 chars each after the concatenation header).
#: Rounded down to 320 as a clean, memorable budget with headroom.
TEXT_CHAR_LIMIT = 320


@dataclass
class TodayDigest:
    date: dt.date
    generated_at: dt.datetime
    cards_due_today: int = 0
    cards_new_waiting: int = 0
    decks: int = 0
    cards_by_deck: List[Dict[str, Any]] = field(default_factory=list)
    next_steps: List[Dict[str, Any]] = field(default_factory=list)
    habits: List[Dict[str, Any]] = field(default_factory=list)
    inbox_count: int = 0
    pending_dates: int = 0
    calendar: List[Dict[str, Any]] = field(default_factory=list)
    projects_active: int = 0

    @property
    def is_empty(self) -> bool:
        """Nothing due, nothing scheduled, nothing waiting — a real, quiet
        day, not a bug. Both renderers branch on this before anything else."""
        return not (
            self.cards_due_today
            or self.next_steps
            or self.habits
            or self.inbox_count
            or self.pending_dates
            or self.calendar
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "date": self.date.isoformat(),
            "generated_at": self.generated_at.isoformat(),
            "cards": {
                "due_today": self.cards_due_today,
                "new_waiting": self.cards_new_waiting,
                "decks": self.decks,
                "by_deck": self.cards_by_deck,
            },
            "next_steps": self.next_steps,
            "habits": self.habits,
            "inbox": {
                "count": self.inbox_count,
                "pending_dates": self.pending_dates,
            },
            "calendar": self.calendar,
            "projects_active": self.projects_active,
            "is_empty": self.is_empty,
        }


# --------------------------------------------------------------------------
# build
# --------------------------------------------------------------------------


def build(
    all_notes: List[Note],
    active_projects: List[Note],
    decks: List[Any],
    cfg: Config,
    *,
    inbox_count: int,
    pending_dates: int,
    on: Optional[dt.date] = None,
) -> TodayDigest:
    """Assemble the digest from what the engine already knows.

    `active_projects` is the same "not done" filter the dashboard and the
    weekly review use; `decks` is `DeckStore.all()`. Nothing here re-reads
    the vault — the caller (`Engine.today_digest`) already did.
    """
    today = on or dt.date.today()

    cards_by_deck = []
    for deck in decks:
        due = len(deck.due(today))
        if due:
            cards_by_deck.append(
                {"note_id": deck.note_id, "subject": deck.subject or deck.note_id, "due": due}
            )
    cards_by_deck.sort(key=lambda d: -d["due"])

    next_steps = [a.as_dict for a in workflow.next_actions(active_projects)]

    day_start = dt.datetime.combine(today, dt.time.min).astimezone()
    day_end = dt.datetime.combine(today, dt.time.max).astimezone()

    habits_today: List[Dict[str, Any]] = []
    for note in all_notes:
        if note.bucket != Bucket.AREA:
            continue
        occ = calevents.occurrences(note, cfg, day_start, day_end)
        if not occ:
            continue
        when, minutes = occ[0]
        habits_today.append(
            {
                "note_id": note.id,
                "title": note.title,
                "time": when.strftime("%H:%M"),
                "minutes": minutes,
                "intention": habitsmod.intention_sentence(note.habit, fallback=note.title),
            }
        )
    habits_today.sort(key=lambda h: h["time"])

    calendar: List[Dict[str, Any]] = []
    for note in active_projects:
        p = note.project
        if not p:
            continue
        for step in p.steps:
            if step.done or not step.scheduled:
                continue
            if step.scheduled.date() == today:
                calendar.append(
                    {
                        "kind": "block",
                        "note_id": note.id,
                        "title": f"{note.title}: {step.text}",
                        "time": step.scheduled.strftime("%H:%M"),
                        "minutes": step.minutes,
                    }
                )
    for h in habits_today:
        calendar.append(
            {
                "kind": "habit",
                "note_id": h["note_id"],
                "title": h["title"],
                "time": h["time"],
                "minutes": h["minutes"],
            }
        )
    for note in all_notes:
        if note.bucket != Bucket.RESOURCE or not note.review or not note.review.next:
            continue
        if note.review.next <= today:
            calendar.append(
                {
                    "kind": "review",
                    "note_id": note.id,
                    "title": f"Review: still need “{note.title}”?",
                    "time": None,
                    "minutes": 0,
                    "overdue_days": (today - note.review.next).days,
                }
            )
    calendar.sort(key=lambda c: (c["time"] is None, c["time"] or ""))

    return TodayDigest(
        date=today,
        generated_at=dt.datetime.now().astimezone(),
        cards_due_today=sum(len(d.due(today)) for d in decks),
        cards_new_waiting=sum(len(d.new()) for d in decks),
        decks=len(decks),
        cards_by_deck=cards_by_deck,
        next_steps=next_steps,
        habits=habits_today,
        inbox_count=inbox_count,
        pending_dates=pending_dates,
        calendar=calendar,
        projects_active=len(active_projects),
    )


# --------------------------------------------------------------------------
# render: text (SMS, <=320 chars)
# --------------------------------------------------------------------------


def _truncate(text: str, width: int) -> str:
    text = (text or "").strip()
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)].rstrip() + "…"


def _short_date(d: dt.date) -> str:
    return d.strftime("%a %d %b")


def render_text(payload: Dict[str, Any]) -> str:
    """The SMS body. Priority order and the reasoning for it are the module
    docstring above — this function only implements the cut."""
    date_label = _short_date(dt.date.fromisoformat(payload["date"]))
    cal = payload["calendar"]
    steps = payload["next_steps"]
    cards = payload["cards"]
    habits = payload["habits"]
    inbox = payload["inbox"]

    if payload.get("is_empty"):
        return f"{date_label}: nothing on the calendar, no cards due, inbox clear."

    parts: List[str] = []

    if cal:
        shown = cal[:3]
        times = ", ".join(
            (c["time"] or "later") for c in shown
        )
        more = f" +{len(cal) - 3} more" if len(cal) > 3 else ""
        parts.append(f"{len(cal)} on the calendar: {times}{more}")

    # The top step is appended last, from whatever budget the fixed-width
    # facts leave behind. Truncating it to a constant 34 characters while
    # 240 characters of the segment sat unused produced "Review Claire's
    # existing notes an… — complete second bra…" on a nearly empty day:
    # two ellipses and no information. A placeholder holds its position so
    # the step still reads before the counts.
    step_slot = None
    if steps:
        step_slot = len(parts)
        parts.append("")

    if cards.get("due_today"):
        parts.append(f"{cards['due_today']} card{'s' if cards['due_today'] != 1 else ''} due")

    if habits:
        parts.append(f"{len(habits)} habit{'s' if len(habits) != 1 else ''}")

    if inbox.get("count"):
        parts.append(f"{inbox['count']} in inbox")

    if not parts:
        # Every category was zero except one this function does not itself
        # branch on (e.g. pending_dates only) — say so rather than emit an
        # empty sentence.
        return f"{date_label}: nothing urgent."

    if step_slot is not None:
        top = steps[0]
        raw_step = (top["step"].get("text", "") or "").strip()
        raw_title = (top["note_title"] or "").strip()
        # Everything else, already decided, plus the separators and prefix.
        fixed = parts[:step_slot] + parts[step_slot + 1 :]
        overhead = len(f"{date_label}: ") + sum(len(p) for p in fixed)
        overhead += 3 * max(len(fixed), 0)          # " | " joins
        budget = TEXT_CHAR_LIMIT - overhead - len("Top:  — ")
        if budget < 16:
            # No room to say anything useful about the step; drop it rather
            # than emit "Top: R… — c…".
            parts.pop(step_slot)
        else:
            # Give the project title up to a third, the step the remainder.
            title_budget = max(12, min(len(raw_title), budget // 3))
            step_budget = budget - title_budget
            step_text = _truncate(raw_step, step_budget)
            title = _truncate(raw_title, title_budget)
            parts[step_slot] = f"Top: {step_text} — {title}"

    msg = f"{date_label}: " + " | ".join(p for p in parts if p)
    if len(msg) > TEXT_CHAR_LIMIT:
        msg = msg[: TEXT_CHAR_LIMIT - 1].rstrip() + "…"
    return msg


# --------------------------------------------------------------------------
# render: long (email / dashboard panel)
# --------------------------------------------------------------------------


def render_long(payload: Dict[str, Any]) -> str:
    date_label = dt.date.fromisoformat(payload["date"]).strftime("%A %d %B %Y")
    lines: List[str] = [f"Today — {date_label}"]

    if payload.get("is_empty"):
        lines.append("")
        lines.append(
            "Nothing on the calendar, no cards due, no habits scheduled, "
            "inbox is clear."
        )
        return "\n".join(lines)

    cal = payload["calendar"]
    lines.append("")
    if cal:
        lines.append(f"ON THE CALENDAR ({len(cal)})")
        for c in cal:
            when = c["time"] or "  — "
            tail = f"  ({c['minutes']}m)" if c.get("minutes") else ""
            if c["kind"] == "review" and c.get("overdue_days", 0) > 0:
                tail = f"  ({c['overdue_days']}d overdue)"
            lines.append(f"  {when}  {c['title']}{tail}")
    else:
        lines.append("ON THE CALENDAR: nothing scheduled.")

    lines.append("")
    steps = payload["next_steps"]
    if steps:
        lines.append(f"NEXT UP ({payload['projects_active']} active project"
                      f"{'s' if payload['projects_active'] != 1 else ''})")
        for a in steps:
            due = f"  due {a['deadline']}" if a.get("deadline") else ""
            when = (
                f"  @ {a['step']['scheduled'][:16].replace('T', ' ')}"
                if a["step"].get("scheduled")
                else ""
            )
            lines.append(
                f"  [{int(a['urgency'] * 100):3d}] {a['step']['text']} — "
                f"{a['note_title']}{due}{when}"
            )
    else:
        lines.append("NEXT UP: no active projects with work outstanding.")

    lines.append("")
    cards = payload["cards"]
    if cards["due_today"] or cards["new_waiting"]:
        lines.append(
            f"CARDS DUE TODAY: {cards['due_today']} (across {cards['decks']} deck"
            f"{'s' if cards['decks'] != 1 else ''})"
        )
        for row in cards["by_deck"]:
            lines.append(f"  {row['subject']}: {row['due']} due")
        if cards["new_waiting"]:
            lines.append(f"  ({cards['new_waiting']} new card(s) waiting)")
    else:
        lines.append(f"CARDS DUE TODAY: none ({cards['decks']} deck(s) total).")

    lines.append("")
    habits = payload["habits"]
    if habits:
        lines.append(f"HABITS DUE TODAY ({len(habits)})")
        for h in habits:
            note = h["intention"] or "no implementation intention written yet"
            lines.append(f"  {h['time']}  {h['title']} ({h['minutes']}m) — {note}")
    else:
        lines.append("HABITS DUE TODAY: none.")

    lines.append("")
    inbox = payload["inbox"]
    bits = []
    if inbox["count"]:
        bits.append(f"{inbox['count']} waiting")
    if inbox["pending_dates"]:
        bits.append(
            f"{inbox['pending_dates']} deadline"
            f"{'s' if inbox['pending_dates'] != 1 else ''} need confirming"
        )
    lines.append("INBOX: " + (", ".join(bits) if bits else "clear"))

    return "\n".join(lines)
