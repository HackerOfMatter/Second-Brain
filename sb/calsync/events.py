"""Deriving calendar items from vault state.

Every sink consumes this, so an item looks identical whether it lands in a
.ics file, in Google Calendar or in Google Tasks, and its id is stable across
regenerations (that is what makes a re-sync an update rather than a duplicate).

Two kinds come out of here, and the split is the point:

  * **Events** — things that occupy physical time. Project work blocks and
    Area habit blocks. An event answers "when am I doing this".
  * **Tasks** — things that are *due*. Only Projects produce these, because
    only a Project has a defined end (§2). A task answers "when must this be
    finished", which is not the same question and does not deserve an hour on
    the grid.

A deadline used to be an all-day event, which is how a calendar ends up with a
row of banners that mean "not today, but soon" — unactionable and easy to
scroll past. It is a task now.

Areas get the opposite treatment: no due date ever, and a recurring event
series instead, because an ongoing responsibility is a rhythm, not a date.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .. import taxonomy
from ..config import Config
from ..models import AreaSchedule, Bucket, Cadence, MaterialKind, Note, ProjectStatus

DOMAIN = "secondbrain.local"

#: RFC 5545 weekday codes, indexed by Python's Monday=0 convention.
BYDAY = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass
class CalEvent:
    """Something that occupies time."""

    uid: str
    summary: str
    start: dt.datetime | dt.date
    end: Optional[dt.datetime | dt.date] = None
    all_day: bool = False
    description: str = ""
    reminders: List[int] = field(default_factory=list)  # minutes before
    kind: str = "block"  # block | area | review | schedule-review
    note_id: str = ""
    category: str = taxonomy.FALLBACK
    rrule: str = ""  # RFC 5545 recurrence rule, without the "RRULE:" prefix

    @property
    def end_or_default(self):
        if self.end:
            return self.end
        if self.all_day:
            return self.start + dt.timedelta(days=1)
        return self.start + dt.timedelta(minutes=30)

    @property
    def minutes(self) -> int:
        if self.all_day:
            return 0
        return int((self.end_or_default - self.start).total_seconds() // 60)


@dataclass
class CalTask:
    """Something that is due. Projects only."""

    uid: str
    summary: str
    due: dt.date
    description: str = ""
    reminders: List[int] = field(default_factory=list)
    note_id: str = ""
    category: str = taxonomy.FALLBACK
    priority: int = 5      # RFC 5545: 1 highest, 9 lowest, 0 undefined
    percent: int = 0       # 0..100, from step progress
    overdue: bool = False


# --------------------------------------------------------------------------
# vault -> items
# --------------------------------------------------------------------------


def events_for_vault(
    notes: List[Note], cfg: Config, decks: Optional[int] = None
) -> List[CalEvent]:
    """`decks` is how many flashcard decks exist. Passing None means "don't
    know", and the study block is left off — a sink that has no view of the
    decks should not guess."""
    events: List[CalEvent] = []
    for note in notes:
        events += events_for_note(note, cfg)
    events += schedule_review_events(notes, cfg)
    events += study_events(cfg, decks)
    events.sort(key=lambda e: _sort_key(e.start))
    return events


def tasks_for_vault(notes: List[Note], cfg: Config) -> List[CalTask]:
    tasks: List[CalTask] = []
    for note in notes:
        tasks += tasks_for_note(note, cfg)
    tasks.sort(key=lambda t: t.due)
    return tasks


def _sort_key(value):
    if isinstance(value, dt.datetime):
        return value.replace(tzinfo=None)
    return dt.datetime.combine(value, dt.time.min)


# --------------------------------------------------------------------------
# tasks: Projects only, one per deadline (§2, §3)
# --------------------------------------------------------------------------


def tasks_for_note(note: Note, cfg: Config) -> List[CalTask]:
    p = note.project
    if not p or note.bucket != Bucket.PROJECT or p.status == ProjectStatus.DONE:
        return []
    if not p.deadline:
        return []
    category = taxonomy.categorize(note, cfg)
    return [
        CalTask(
            uid=f"{note.id}-due@{DOMAIN}",
            summary=taxonomy.decorate(
                note.title, category, cfg, cfg.calendar.emoji_prefix
            ),
            due=p.deadline,
            description=_project_description(note),
            reminders=cfg.calendar.reminder_minutes,
            note_id=note.id,
            category=category,
            priority=_priority(p.level, p.deadline),
            percent=int(round(p.progress * 100)),
            overdue=p.deadline < dt.date.today(),
        )
    ]


def _priority(level: int, deadline: dt.date) -> int:
    """RFC 5545 priority, 1 = highest. Difficulty raises it; so does the
    deadline getting close, because a level-2 task due tomorrow outranks a
    level-5 one due next month."""
    days = (deadline - dt.date.today()).days
    base = 6 - max(1, min(5, int(level)))          # level 5 -> 1, level 1 -> 5
    if days <= 1:
        base -= 2
    elif days <= 3:
        base -= 1
    elif days > 21:
        base += 1
    return max(1, min(9, base))


# --------------------------------------------------------------------------
# events: physical time
# --------------------------------------------------------------------------


def events_for_note(note: Note, cfg: Config) -> List[CalEvent]:
    out: List[CalEvent] = []
    category = taxonomy.categorize(note, cfg)

    # --- Projects: scheduled step blocks. The deadline is a task, not an
    #     event, and is emitted by tasks_for_note() instead.
    p = note.project
    if p and note.bucket == Bucket.PROJECT and p.status != ProjectStatus.DONE:
        for step in p.steps:
            if step.done or not step.scheduled:
                continue
            out.append(
                CalEvent(
                    uid=f"{note.id}-{step.id}@{DOMAIN}",
                    summary=taxonomy.decorate(
                        f"{note.title}: {step.text}",
                        category,
                        cfg,
                        cfg.calendar.emoji_prefix,
                    ),
                    start=step.scheduled,
                    end=step.scheduled + dt.timedelta(minutes=step.minutes),
                    description=(
                        f"Step {step.id} of “{note.title}”.\n"
                        f"Estimate: {step.minutes} min."
                        + (f"\nDue: {p.deadline:%a %d %b}" if p.deadline else "")
                    ),
                    reminders=[10],
                    kind="block",
                    note_id=note.id,
                    category=category,
                )
            )

    # --- Areas: a recurring block of real time (§2). No deadline, ever.
    if note.bucket == Bucket.AREA:
        ev = area_event(note, cfg, category=category)
        if ev:
            out.append(ev)

    # --- Resources: the "still needed?" review prompt (§2) -----------------
    if note.review and note.review.next and note.bucket == Bucket.RESOURCE:
        out.append(
            CalEvent(
                uid=f"{note.id}-review@{DOMAIN}",
                summary=f"Review: still need “{note.title}”?",
                start=note.review.next,
                all_day=True,
                description="Open Second Brain and answer keep / archive.",
                reminders=[0],
                kind="review",
                note_id=note.id,
                category=category,
            )
        )
    return out


def area_event(note: Note, cfg: Config, category: Optional[str] = None) -> Optional[CalEvent]:
    """One recurring event per Area, repeating on its habit cadence."""
    sched = note.schedule or AreaSchedule(
        time=cfg.areas.default_time,
        duration_minutes=cfg.areas.default_duration_minutes,
    )
    if not sched.enabled:
        return None
    cadence = note.habit.cadence if note.habit else Cadence.WEEKLY
    first = first_occurrence(sched, cadence, note.habit)
    if first is None:
        return None
    if sched.until and first.date() > sched.until:
        return None

    category = category or taxonomy.categorize(note, cfg)
    target = note.habit.target_count if note.habit else 1
    return CalEvent(
        uid=f"{note.id}-area@{DOMAIN}",
        summary=taxonomy.decorate(note.title, category, cfg, cfg.calendar.emoji_prefix),
        start=first,
        end=first + dt.timedelta(minutes=sched.duration_minutes),
        description=(
            f"Area — ongoing.\n"
            f"Target: {target}× per {cadence.value}.\n"
            f"{sched.duration_minutes} min, {_days_label(sched, note)}.\n"
            "Change the time, length or days at the weekly schedule review."
        ),
        reminders=[10],
        kind="area",
        note_id=note.id,
        category=category,
        rrule=rrule_for(sched, cadence, note.habit),
    )


def schedule_review_events(notes: List[Note], cfg: Config) -> List[CalEvent]:
    """The "option to change": one weekly recurring event where every Area's
    schedule is revisited. One event for the whole system rather than one per
    Area — a calendar with nine check-in banners on it is a calendar you stop
    reading."""
    if not cfg.areas.schedule_review:
        return []
    areas = [n for n in notes if n.bucket == Bucket.AREA]
    if not areas:
        return []
    weekday = int(cfg.areas.schedule_review_weekday) % 7
    try:
        at = dt.time.fromisoformat(cfg.areas.schedule_review_time)
    except ValueError:
        at = dt.time(19, 0)
    first = _next_weekday(dt.date.today(), weekday)
    start = dt.datetime.combine(first, at).astimezone()
    listing = "\n".join(f"  • {n.title}" for n in sorted(areas, key=lambda n: n.title))
    return [
        CalEvent(
            uid=f"schedule-review@{DOMAIN}",
            summary="🔁 Schedule review — Areas",
            start=start,
            end=start + dt.timedelta(minutes=cfg.areas.schedule_review_minutes),
            description=(
                "Keep, change or pause each Area's recurring time.\n\n"
                f"{listing}\n\n"
                f"Open http://{cfg.host}:{cfg.port}/#areas"
            ),
            reminders=[10],
            kind="schedule-review",
            note_id="",
            category=taxonomy.FALLBACK,
            rrule=f"FREQ=WEEKLY;BYDAY={BYDAY[weekday]}",
        )
    ]


def study_events(cfg: Config, decks: Optional[int]) -> List[CalEvent]:
    """One daily recurring block for spaced repetition.

    A review queue only works if you meet it every day, which makes it a
    habit, not a task — so it gets the same treatment an Area does: a
    recurring block of real time rather than a due date. Deliberately not one
    event per day with a card count in the title: that would mean rewriting
    365 events on every sync, and a number that is stale by the afternoon.
    """
    if not cfg.study.calendar_event or not decks:
        return []
    try:
        at = dt.time.fromisoformat(cfg.study.study_time)
    except ValueError:
        at = dt.time(19, 30)
    start = dt.datetime.combine(dt.date.today(), at).astimezone()
    return [
        CalEvent(
            uid=f"study-session@{DOMAIN}",
            summary="📚 Study — spaced repetition",
            start=start,
            end=start + dt.timedelta(minutes=cfg.study.study_minutes),
            description=(
                "Review whatever is due, across every subject.\n\n"
                f"Open http://{cfg.host}:{cfg.port}/study"
            ),
            reminders=[10],
            kind="study",
            note_id="",
            category="study",
            rrule="FREQ=DAILY",
        )
    ]


# --------------------------------------------------------------------------
# recurrence
# --------------------------------------------------------------------------


def rrule_for(sched: AreaSchedule, cadence: Cadence, habit=None) -> str:
    if cadence == Cadence.DAILY:
        rule = "FREQ=DAILY"
    elif cadence == Cadence.MONTHLY:
        rule = f"FREQ=MONTHLY;BYMONTHDAY={sched.monthday}"
    else:
        days = ",".join(BYDAY[d] for d in sched.effective_days(habit))
        rule = f"FREQ=WEEKLY;BYDAY={days}"
    if sched.until:
        # DTSTART is floating local, so UNTIL is floating too (RFC 5545 §3.3.10).
        rule += f";UNTIL={sched.until:%Y%m%d}T235959"
    return rule


def first_occurrence(
    sched: AreaSchedule, cadence: Cadence, habit=None, today: Optional[dt.date] = None
) -> Optional[dt.datetime]:
    """The DTSTART of the series: the first day matching the rule, on or after
    the schedule's start date."""
    today = today or dt.date.today()
    day = sched.start or today
    if day < today:
        day = today
    at = sched.start_time()

    if cadence == Cadence.DAILY:
        return dt.datetime.combine(day, at).astimezone()
    if cadence == Cadence.MONTHLY:
        target = min(28, max(1, sched.monthday))
        candidate = day.replace(day=target) if day.day <= target else _next_month(day, target)
        return dt.datetime.combine(candidate, at).astimezone()

    days = sched.effective_days(habit)
    if not days:
        return None
    for offset in range(0, 8):
        candidate = day + dt.timedelta(days=offset)
        if candidate.weekday() in days:
            return dt.datetime.combine(candidate, at).astimezone()
    return None


def occurrences(
    note: Note,
    cfg: Config,
    start: dt.datetime,
    end: dt.datetime,
) -> List[Tuple[dt.datetime, int]]:
    """Expand an Area's series into concrete (start, minutes) blocks inside a
    window. Used by the planner when `planner.respect_area_blocks` is on, and
    by the dashboard's upcoming list."""
    if note.bucket != Bucket.AREA:
        return []
    sched = note.schedule
    if not sched or not sched.enabled:
        return []
    cadence = note.habit.cadence if note.habit else Cadence.WEEKLY
    first = first_occurrence(sched, cadence, note.habit)
    if first is None:
        return []

    at = sched.start_time()
    out: List[Tuple[dt.datetime, int]] = []
    day = min(first.date(), start.date())
    last = end.date()
    days = sched.effective_days(note.habit)
    guard = 0
    while day <= last and guard < 800:
        guard += 1
        if sched.until and day > sched.until:
            break
        hit = (
            cadence == Cadence.DAILY
            or (cadence == Cadence.WEEKLY and day.weekday() in days)
            or (cadence == Cadence.MONTHLY and day.day == min(28, sched.monthday))
        )
        if hit and day >= first.date():
            when = dt.datetime.combine(day, at).astimezone()
            if start <= when <= end:
                out.append((when, sched.duration_minutes))
        day += dt.timedelta(days=1)
    return out


def _next_month(day: dt.date, monthday: int) -> dt.date:
    first_next = (day.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return first_next.replace(day=monthday)


def _next_weekday(day: dt.date, weekday: int) -> dt.date:
    return day + dt.timedelta(days=(weekday - day.weekday()) % 7)


def _days_label(sched: AreaSchedule, note: Note) -> str:
    cadence = note.habit.cadence if note.habit else Cadence.WEEKLY
    if cadence == Cadence.DAILY:
        return f"every day at {sched.time}"
    if cadence == Cadence.MONTHLY:
        return f"day {sched.monthday} of each month at {sched.time}"
    names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    picked = ", ".join(names[d] for d in sched.effective_days(note.habit))
    return f"{picked} at {sched.time}"


# --------------------------------------------------------------------------


def next_habit_checkin(note: Note, cfg: Config) -> Optional[dt.date]:
    """Kept for the habit log: the date the next check-in question applies to.
    It no longer produces its own calendar event — the weekly schedule review
    covers every Area at once."""
    if not note.habit:
        return None
    today = dt.date.today()
    if note.habit.cadence == Cadence.DAILY:
        return today + dt.timedelta(days=1)
    if note.habit.cadence == Cadence.MONTHLY:
        return (today.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    return _next_weekday(today + dt.timedelta(days=1), int(cfg.review.habit_checkin_weekday) % 7)


def _project_description(note: Note) -> str:
    p = note.project
    if not p:
        return note.title
    lines = [
        f"Level {p.level}/5 · est. {p.estimate_minutes} min · {int(p.progress * 100)}% done",
    ]
    if p.ideal_end:
        lines.append(f"Done means: {p.ideal_end}")
    if p.skills:
        lines.append("Skills: " + ", ".join(p.skills))
    if p.materials:
        # Only what is still outstanding: a calendar description is read on
        # the way to doing the work, so a list of things already gathered is
        # noise. Hardware and software are called out — they are the ones you
        # have to physically have before you start.
        pending = [m for m in p.materials if not m.done]
        if pending:
            lines.append(
                "Materials: "
                + "; ".join(
                    m.text if m.kind is MaterialKind.MATERIAL
                    else f"{m.text} ({m.kind.value})"
                    for m in pending
                )
            )
    remaining = p.remaining_steps
    if remaining:
        lines.append("")
        lines.append("Remaining steps:")
        lines += [f"  {i+1}. {s.text} ({s.minutes}m)" for i, s in enumerate(remaining)]
    return "\n".join(lines)
