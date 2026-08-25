"""The work-flow engine (blueprint §8).

Two jobs:

  * **Planning** — lay a Project's steps into real time slots, respecting your
    working hours and a daily cap, working backwards from the deadline. These
    slots are what the calendar sink turns into events, so "estimated time and
    steps become scheduled blocks" (§3) happens here, not in the calendar code.
  * **Sequencing** — answer "what's next" across every active Project, ranked
    by urgency rather than by capture order.

Planning is idempotent: re-planning a Project only moves steps that are still
unscheduled or whose slot has passed, so a block you have already worked
around does not jump.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import List, Optional, Tuple

from .config import PlannerConfig
from .models import Note, ProjectStatus, Step, now


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


@dataclass
class PlanReport:
    scheduled: int = 0
    unscheduled: int = 0
    overflowed: bool = False  # work did not fit before the deadline
    first_slot: Optional[dt.datetime] = None
    last_slot: Optional[dt.datetime] = None

    @property
    def message(self) -> str:
        if self.overflowed:
            return (
                f"Scheduled {self.scheduled} step(s), but the work does not fit before "
                "the deadline at your current daily cap — the last blocks run past it."
            )
        if self.scheduled:
            return f"Scheduled {self.scheduled} step(s) through {self.last_slot:%a %d %b}."
        return "Nothing to schedule."


def plan_project(
    note: Note,
    cfg: PlannerConfig,
    start_from: Optional[dt.datetime] = None,
    force: bool = False,
    busy: Optional[List[Tuple[dt.datetime, int]]] = None,
) -> PlanReport:
    """Assign `scheduled` datetimes to a Project's outstanding steps.

    `busy` is every block already committed elsewhere in the vault, as
    (start, minutes). Without it each Project would plan in isolation and
    three projects would all claim Monday 9am -- which is exactly the kind of
    schedule that teaches you to ignore your own calendar.
    """
    report = PlanReport()
    if not note.project:
        return report

    start_from = start_from or now()
    todo = [
        s
        for s in note.project.steps
        if not s.done and (force or s.scheduled is None or s.scheduled < start_from)
    ]
    if not todo:
        return report

    deadline = note.project.deadline
    cursor = _next_slot_start(start_from, cfg)
    day_used: dict[dt.date, int] = {}
    committed: List[Tuple[dt.datetime, dt.datetime]] = []

    for start, minutes in busy or []:
        committed.append((start, start + dt.timedelta(minutes=minutes)))
        day_used[start.date()] = day_used.get(start.date(), 0) + minutes

    # capacity already committed by this project's steps that we are not moving
    for s in note.project.steps:
        if s.scheduled and s not in todo and not s.done:
            day_used[s.scheduled.date()] = day_used.get(s.scheduled.date(), 0) + s.minutes
            committed.append((s.scheduled, s.scheduled + dt.timedelta(minutes=s.minutes)))

    for step in todo:
        slot, cursor = _find_slot(cursor, step.minutes, cfg, day_used, committed)
        committed.append((slot, slot + dt.timedelta(minutes=step.minutes)))
        step.scheduled = slot
        day_used[slot.date()] = day_used.get(slot.date(), 0) + step.minutes
        report.scheduled += 1
        report.first_slot = report.first_slot or slot
        report.last_slot = slot
        if deadline and slot.date() > deadline:
            report.overflowed = True

    report.unscheduled = sum(
        1 for s in note.project.steps if not s.done and s.scheduled is None
    )
    note.touch()
    return report


def _find_slot(
    cursor: dt.datetime,
    minutes: int,
    cfg: PlannerConfig,
    day_used: dict,
    committed: Optional[List[Tuple[dt.datetime, dt.datetime]]] = None,
) -> Tuple[dt.datetime, dt.datetime]:
    """Walk forward until a free working slot with capacity is found. Returns
    (slot start, cursor for the next search)."""
    committed = committed or []
    guard = 0
    while guard < 2000:  # bounded: refuse to loop forever on a pathological config
        guard += 1
        day = cursor.date()
        end_of_day = dt.datetime.combine(day, cfg.end_time()).astimezone(cursor.tzinfo)
        finish = cursor + dt.timedelta(minutes=minutes)

        if _is_workday(day, cfg) and day_used.get(day, 0) + minutes <= cfg.max_minutes_per_day:
            clash = _first_clash(cursor, finish, committed)
            if clash is None:
                if finish <= end_of_day:
                    return cursor, cursor + dt.timedelta(
                        minutes=minutes + cfg.block_gap_minutes
                    )
            else:
                # jump past the conflicting block and try again on the same day
                cursor = clash + dt.timedelta(minutes=cfg.block_gap_minutes)
                if cursor < end_of_day:
                    continue

        cursor = _next_slot_start(
            dt.datetime.combine(day + dt.timedelta(days=1), cfg.start_time()).astimezone(
                cursor.tzinfo
            ),
            cfg,
        )
    return cursor, cursor + dt.timedelta(minutes=minutes)


def _first_clash(
    start: dt.datetime, end: dt.datetime, committed: List[Tuple[dt.datetime, dt.datetime]]
) -> Optional[dt.datetime]:
    """End time of the earliest committed block overlapping [start, end)."""
    hit = None
    for b_start, b_end in committed:
        if b_start < end and start < b_end:
            if hit is None or b_end < hit:
                hit = b_end
    return hit


def _is_workday(day: dt.date, cfg: PlannerConfig) -> bool:
    return day.weekday() in cfg.workdays


def _next_slot_start(when: dt.datetime, cfg: PlannerConfig) -> dt.datetime:
    """Round `when` forward to the next moment inside a working window."""
    when = when.replace(second=0, microsecond=0)
    for _ in range(400):
        start = dt.datetime.combine(when.date(), cfg.start_time()).astimezone(when.tzinfo)
        end = dt.datetime.combine(when.date(), cfg.end_time()).astimezone(when.tzinfo)
        if _is_workday(when.date(), cfg) and when < end:
            return max(when, start)
        when = dt.datetime.combine(
            when.date() + dt.timedelta(days=1), cfg.start_time()
        ).astimezone(when.tzinfo)
    return when


# --------------------------------------------------------------------------
# sequencing: what's next
# --------------------------------------------------------------------------


@dataclass
class NextAction:
    note_id: str
    note_title: str
    step: Step
    urgency: float
    deadline: Optional[dt.date]

    @property
    def as_dict(self) -> dict:
        return {
            "note_id": self.note_id,
            "note_title": self.note_title,
            "step": self.step.model_dump(mode="json"),
            "urgency": round(self.urgency, 3),
            "deadline": self.deadline.isoformat() if self.deadline else None,
        }


def next_actions(notes: List[Note], limit: int = 10) -> List[NextAction]:
    """The execution queue: the single next step of every active Project,
    ranked. Only one step per Project — a queue that shows you six things from
    the same project is a list, not a queue."""
    out: List[NextAction] = []
    for note in notes:
        p = note.project
        if not p or p.status in (ProjectStatus.DONE, ProjectStatus.BLOCKED):
            continue
        remaining = p.remaining_steps
        if not remaining:
            continue
        step = min(remaining, key=lambda s: (s.scheduled is None, s.scheduled or now()))
        out.append(
            NextAction(
                note_id=note.id,
                note_title=note.title,
                step=step,
                urgency=urgency(note),
                deadline=p.deadline,
            )
        )
    out.sort(key=lambda a: -a.urgency)
    return out[:limit]


def urgency(note: Note) -> float:
    """0..1. Rises as the deadline approaches, and faster when there is more
    work left than time to do it."""
    p = note.project
    if not p:
        return 0.0
    if not p.deadline:
        return 0.25 * (1.0 - p.progress)

    days_left = (p.deadline - dt.date.today()).days
    if days_left < 0:
        return 1.0
    time_pressure = 1.0 / (1.0 + max(0, days_left) / 3.0)

    remaining_minutes = sum(s.minutes for s in p.remaining_steps)
    capacity = max(1, days_left) * 180
    load = min(1.0, remaining_minutes / capacity)
    return round(min(1.0, 0.6 * time_pressure + 0.4 * load), 4)


def upcoming(notes: List[Note], days: int = 14) -> List[dict]:
    """Deadlines and scheduled blocks in the near window, for the dashboard."""
    horizon = dt.date.today() + dt.timedelta(days=days)
    items: List[dict] = []
    for note in notes:
        p = note.project
        if not p or p.status == ProjectStatus.DONE:
            continue
        if p.deadline and p.deadline <= horizon:
            items.append(
                {
                    # A deadline is a task now, not an event — the dashboard
                    # renders these in the task list, not the time grid.
                    "kind": "due",
                    "note_id": note.id,
                    "title": note.title,
                    "when": p.deadline.isoformat(),
                    "overdue": p.deadline < dt.date.today(),
                }
            )
        for s in p.steps:
            if s.done or not s.scheduled:
                continue
            if s.scheduled.date() <= horizon:
                items.append(
                    {
                        "kind": "block",
                        "note_id": note.id,
                        "title": f"{note.title} — {s.text}",
                        "when": s.scheduled.isoformat(),
                        "minutes": s.minutes,
                        "overdue": False,
                    }
                )
    items.sort(key=lambda i: i["when"])
    return items
