"""The application service — one object the UI, the CLI and (later) the
scheduler all talk to.

Keeping this layer separate from the HTTP layer means the same operations are
scriptable, testable without a server, and reusable by the background jobs
that will drive habit check-ins and Resource reviews.
"""

from __future__ import annotations

import datetime as dt
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import (
    ask as askmod,
    connect as connectmod,
    extract,
    generate,
    parser,
    taxonomy,
    tutor,
    workflow,
)
from .calsync import events as calevents, get_sink
from .cards import Card, Deck, DeckStore, fingerprint
from .config import Config
from .index import Index
from .models import (
    AreaSchedule,
    Bucket,
    Cadence,
    HabitMeta,
    Material,
    MaterialKind,
    Note,
    ProjectStatus,
    ReviewMeta,
    SrsState,
)
from .vault import Vault


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vault = Vault(cfg.vault)
        self.vault.ensure_structure()
        self.decks = DeckStore(cfg.vault)
        self.index = Index(cfg)

    # -- capture ------------------------------------------------------------

    def capture(
        self, text: str, bucket: str, title: str = "", due: Optional[str] = None
    ) -> Dict[str, Any]:
        """The three-button entry point (§2). Classification is the human's
        decision; everything after it is automatic.

        `due` is the date picker beside the capture buttons. It beats anything
        in the text and needs no confirmation — lj already chose it, and a
        picked date cannot be misread.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("empty capture")
        target = Bucket(bucket)
        note = Note.capture(text, target, title=title or extract.derive_title(text))

        # One read, reused by the planner and the calendar sync below.
        snapshot = self._snapshot()

        info: Dict[str, Any] = {}
        if target == Bucket.PROJECT:
            result = parser.apply_to_note(note, self.cfg)
            info["parser"] = {
                "provider": result.provider,
                "degraded": result.degraded,
                "note": result.note,
            }
            picked = _as_date(due)
            if picked and note.project:
                note.project.deadline = picked
                note.project.deadline_source = extract.MANUAL
                note.project.deadline_confirmed = True
                note.project.deadline_phrase = ""
            if note.project and note.project.learning:
                note.srs = SrsState(due=dt.date.today() + dt.timedelta(days=1))
            report = workflow.plan_project(
                note, self.cfg.planner, busy=self._busy(note.id, snapshot)
            )
            info["plan"] = {
                "scheduled": report.scheduled,
                "overflowed": report.overflowed,
                "message": report.message,
            }
            note.body = _project_body(note)
        elif target == Bucket.AREA:
            # An Area has no end, so it gets no deadline and no task — it gets
            # a recurring block of real time, editable at the weekly review.
            note.habit = HabitMeta()
            note.schedule = AreaSchedule(
                time=self.cfg.areas.default_time,
                duration_minutes=self.cfg.areas.default_duration_minutes,
            )
            note.body = _area_body(note, self.cfg)
        elif target == Bucket.RESOURCE:
            note.review = ReviewMeta(
                cycle_days=self.cfg.review.resource_cycle_days,
                next=dt.date.today()
                + dt.timedelta(days=self.cfg.review.resource_cycle_days),
            )

        path = self.vault.write(note)
        self.vault.log_line("capture", f"{note.bucket.value}  {note.id}  {note.title}")
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {"note": _note_dict(note), "path": str(path), **info}

    # -- read ---------------------------------------------------------------

    def notes(self, bucket: Optional[str] = None) -> List[Note]:
        b = Bucket(bucket) if bucket else None
        return [n for _, n in self.vault.notes(b)]

    def note(self, note_id: str) -> Note:
        _, note = self.vault.get(note_id)
        return note

    def dashboard(self) -> Dict[str, Any]:
        all_notes = self.notes()
        projects = [n for n in all_notes if n.bucket == Bucket.PROJECT]
        active = [
            n for n in projects if n.project and n.project.status != ProjectStatus.DONE
        ]
        active.sort(key=lambda n: -workflow.urgency(n))
        areas = sorted(
            (n for n in all_notes if n.bucket == Bucket.AREA), key=lambda n: n.title
        )
        return {
            "counts": {b.value: sum(1 for n in all_notes if n.bucket == b) for b in Bucket},
            "projects": [
                _note_dict(
                    n,
                    urgency=workflow.urgency(n),
                    category=taxonomy.categorize(n, self.cfg),
                )
                for n in active
            ],
            # Only Projects have due dates, so the task list is exactly the
            # active projects that carry one.
            "tasks": [
                {
                    "note_id": t.note_id,
                    "title": t.summary,
                    "due": t.due.isoformat(),
                    "category": t.category,
                    "priority": t.priority,
                    "percent": t.percent,
                    "overdue": t.overdue,
                }
                for t in calevents.tasks_for_vault(active, self.cfg)
            ],
            "areas": [self._area_dict(n) for n in areas],
            "next_actions": [a.as_dict for a in workflow.next_actions(active)],
            "pending_dates": self._pending_dates(active),
            "reviews": self._reviews_due(all_notes),
            "archive": self._archived(all_notes),
            "upcoming": workflow.upcoming(all_notes),
            "categories": taxonomy.as_dicts(self.cfg),
            "study": self._study_summary(all_notes),
            "vault": str(self.cfg.vault),
        }

    def _study_summary(self, all_notes: List[Note]) -> Dict[str, Any]:
        """The one-line version of the tutor for the dashboard, including the
        "ready to graduate?" prompt the blueprint asks for (§4)."""
        decks = self.decks.all()
        today = dt.date.today()
        by_id = {n.id: n for n in all_notes}
        graduation = []
        for deck in decks:
            if not tutor.is_ready_to_graduate(deck, self.cfg):
                continue
            note = by_id.get(deck.note_id)
            if not note or note.bucket != Bucket.PROJECT:
                continue
            if not note.project or not note.project.learning:
                continue
            progress = tutor.deck_progress(deck, self.cfg, today)
            graduation.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "mastery": progress["mastery"],
                    "mature": progress["mature"],
                    "active": progress["active"],
                }
            )
        reviewed, _ = tutor.counted_today(self.decks)
        return {
            "decks": len(decks),
            "due_today": sum(len(d.due(today)) for d in decks),
            "new_waiting": sum(len(d.new()) for d in decks),
            "drafts": sum(len(d.drafts) for d in decks),
            "reviewed_today": reviewed,
            "graduation": graduation,
        }

    def _area_dict(self, note: Note) -> Dict[str, Any]:
        sched = note.schedule or AreaSchedule(
            time=self.cfg.areas.default_time,
            duration_minutes=self.cfg.areas.default_duration_minutes,
        )
        cadence = note.habit.cadence if note.habit else Cadence.WEEKLY
        return {
            "id": note.id,
            "title": note.title,
            "category": taxonomy.categorize(note, self.cfg),
            "cadence": cadence.value,
            "target_count": note.habit.target_count if note.habit else 1,
            "schedule": sched.model_dump(mode="json"),
            "days": sched.effective_days(note.habit),
            # Ticking a day pins the series. Once pinned, changing the target
            # count no longer re-spreads the days, and the UI has to say so —
            # otherwise "3× a week" looks broken when it stays on Tue/Thu.
            "pinned": bool(sched.days),
            "rrule": calevents.rrule_for(sched, cadence, note.habit),
            "next": (
                lambda first: first.isoformat() if first else None
            )(calevents.first_occurrence(sched, cadence, note.habit)),
        }

    # -- mutate -------------------------------------------------------------

    def reparse(self, note_id: str) -> Dict[str, Any]:
        note = self.note(note_id)
        snapshot = self._snapshot()
        result = parser.apply_to_note(note, self.cfg)
        report = workflow.plan_project(
            note, self.cfg.planner, busy=self._busy(note.id, snapshot)
        )
        note.body = _project_body(note)
        self.vault.save(note)
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {
            "note": _note_dict(note),
            "parser": {"provider": result.provider, "degraded": result.degraded, "note": result.note},
            "plan": {"scheduled": report.scheduled, "message": report.message},
        }

    def toggle_step(self, note_id: str, step_id: str) -> Dict[str, Any]:
        note = self.note(note_id)
        if not note.project:
            raise ValueError("note has no project metadata")
        for step in note.project.steps:
            if step.id == step_id:
                step.done = not step.done
                step.done_at = dt.datetime.now().astimezone() if step.done else None
                note.log("step", f"{step_id} {'done' if step.done else 'reopened'}")
                break
        else:
            raise ValueError(f"no step {step_id!r}")

        if note.project.steps and all(s.done for s in note.project.steps):
            if note.project.learning:
                note.project.status = ProjectStatus.ACTIVE  # graduation is SR-driven (§4)
            else:
                note.project.status = ProjectStatus.DONE
                note.log("completed")
        note.body = _project_body(note)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return _note_dict(note)

    # -- one read per mutation ----------------------------------------------
    #
    # Writing a note used to walk the vault three separate times: once for the
    # planner's Project blocks, once for its Area blocks, and once more for the
    # calendar sync. They all want the same thing — every note, as it stands
    # right now — so they now share a single read. `_replacing` swaps in the
    # note being written, which is not on disk yet (capture) or is about to
    # change (toggle), so the calendar still sees the new version.

    def _snapshot(self) -> List[Note]:
        return self.notes()

    @staticmethod
    def _replacing(snapshot: List[Note], note: Note) -> List[Note]:
        out = [n for n in snapshot if n.id != note.id]
        out.append(note)
        return out

    def _busy(
        self, exclude_id: str = "", snapshot: Optional[List[Note]] = None
    ) -> List[tuple]:
        """Every work block already committed elsewhere in the vault, so the
        planner schedules around them instead of double-booking."""
        notes = self._snapshot() if snapshot is None else snapshot
        blocks = []
        for n in notes:
            if n.bucket is not Bucket.PROJECT or n.id == exclude_id or not n.project:
                continue
            for s in n.project.steps:
                if s.scheduled and not s.done:
                    blocks.append((s.scheduled, s.minutes))
        if self.cfg.planner.respect_area_blocks:
            start = dt.datetime.now().astimezone()
            end = start + dt.timedelta(days=120)
            for n in notes:
                if n.bucket is Bucket.AREA:
                    blocks += calevents.occurrences(n, self.cfg, start, end)
        return blocks

    def replan(self, note_id: str, force: bool = True) -> Dict[str, Any]:
        note = self.note(note_id)
        snapshot = self._snapshot()
        report = workflow.plan_project(
            note, self.cfg.planner, force=force, busy=self._busy(note_id, snapshot)
        )
        note.body = _project_body(note)
        self.vault.save(note)
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {"note": _note_dict(note), "message": report.message}

    def move(self, note_id: str, bucket: str) -> Dict[str, Any]:
        note = self.note(note_id)
        target = Bucket(bucket)
        event = {
            (Bucket.PROJECT, Bucket.RESOURCE): "graduated",
            (Bucket.RESOURCE, Bucket.ARCHIVE): "archived",
            (Bucket.ARCHIVE, Bucket.RESOURCE): "restored",
        }.get((note.bucket, target), "moved")
        if target == Bucket.RESOURCE and not note.review:
            note.review = ReviewMeta(
                cycle_days=self.cfg.review.resource_cycle_days,
                next=dt.date.today() + dt.timedelta(days=self.cfg.review.resource_cycle_days),
            )
        self.vault.move(note, target, event)
        self._sync_calendar_quiet()
        return _note_dict(note)

    # -- resource reviews (blueprint §2) -------------------------------------

    def _reviews_due(self, all_notes: List[Note]) -> List[Dict[str, Any]]:
        """Resources whose "still needed?" date has arrived.

        The calendar event for this has existed since Phase 1; until now there
        was nowhere to answer it. A prompt you cannot answer is worse than no
        prompt — you learn to scroll past it, and then you scroll past the ones
        that matter too.
        """
        today = dt.date.today()
        out = []
        for note in all_notes:
            if note.bucket != Bucket.RESOURCE or not note.review or not note.review.next:
                continue
            if note.review.next > today:
                continue
            out.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "category": taxonomy.categorize(note, self.cfg),
                    "due": note.review.next,
                    "overdue_days": (today - note.review.next).days,
                    "last": note.review.last,
                    "filed": note.created.date(),
                    "cycle_days": note.review.cycle_days,
                    # The active card count, not merely "a deck file exists" —
                    # generating and rejecting everything leaves an empty deck
                    # behind, and "has flashcards" would then be a lie.
                    "cards": len(deck.active) if (deck := self.decks.get(note.id)) else 0,
                }
            )
        out.sort(key=lambda r: r["due"])
        return out

    def _archived(self, all_notes: List[Note], limit: int = 25) -> Dict[str, Any]:
        """What is in Archive, so restoring is a click rather than a file move.

        §2's Resource↔Archive is bidirectional, and a one-way door in the UI
        would quietly make it one-way in practice.
        """
        rows = [n for n in all_notes if n.bucket == Bucket.ARCHIVE]
        rows.sort(key=lambda n: n.updated, reverse=True)
        return {
            "total": len(rows),
            "items": [
                {
                    "note_id": n.id,
                    "title": n.title,
                    "category": taxonomy.categorize(n, self.cfg),
                    "archived": n.updated.date(),
                }
                for n in rows[:limit]
            ],
        }

    def answer_review(self, note_id: str, keep: bool = True) -> Dict[str, Any]:
        """The answer to "still needed?" — keep it, or send it to Archive.

        Keeping is not a no-op: it stamps the review and pushes the next one a
        full cycle out, so saying yes today does not mean being asked again
        tomorrow. Archiving is a move, never a delete (§2), and everything
        about the note survives it — including its flashcards, which are keyed
        by note id rather than by folder.
        """
        note = self.note(note_id)
        if note.bucket != Bucket.RESOURCE:
            raise ValueError("only Resources are reviewed")
        today = dt.date.today()
        cycle = note.review.cycle_days if note.review else self.cfg.review.resource_cycle_days
        note.review = ReviewMeta(
            cycle_days=cycle,
            last=today,
            next=today + dt.timedelta(days=cycle),
        )
        if keep:
            note.log("reviewed", f"kept · next {note.review.next.isoformat()}")
            self.vault.save(note)
        else:
            # The review stamp goes on before the move so the audit trail says
            # *why* it was archived, not merely that it was.
            note.log("reviewed", "not needed")
            self.vault.move(note, Bucket.ARCHIVE, "archived")
        self._sync_calendar_quiet()
        return {
            "note": _note_dict(note),
            "kept": keep,
            "next": note.review.next if keep else None,
        }

    def restore_resource(self, note_id: str) -> Dict[str, Any]:
        """Archive → Resource, with the review clock restarted.

        Without the reset a restored note arrives with a review date already in
        the past and lands straight back in the queue, which reads as the
        system arguing with you about a decision you just made.
        """
        note = self.note(note_id)
        if note.bucket != Bucket.ARCHIVE:
            raise ValueError("only archived notes are restored")
        today = dt.date.today()
        cycle = note.review.cycle_days if note.review else self.cfg.review.resource_cycle_days
        note.review = ReviewMeta(
            cycle_days=cycle, last=today, next=today + dt.timedelta(days=cycle)
        )
        self.vault.move(note, Bucket.RESOURCE, "restored")
        self._sync_calendar_quiet()
        return {"note": _note_dict(note), "next": note.review.next}

    def snooze_review(self, note_id: str, days: int = 30) -> Dict[str, Any]:
        """Not now. Distinct from "keep": it pushes the question a short way
        out instead of resetting the full cycle, so an undecided answer is not
        recorded as a decision."""
        note = self.note(note_id)
        if note.bucket != Bucket.RESOURCE or not note.review:
            raise ValueError("only Resources are reviewed")
        days = max(1, min(365, int(days)))
        note.review.next = dt.date.today() + dt.timedelta(days=days)
        note.log("reviewed", f"snoozed {days}d")
        self.vault.save(note)
        self._sync_calendar_quiet()
        return {"note": _note_dict(note), "next": note.review.next}

    def set_habit(self, note_id: str, cadence: str, target_count: int) -> Dict[str, Any]:
        note = self.note(note_id)
        note.habit = note.habit or HabitMeta()
        note.habit.cadence = Cadence(cadence)
        note.habit.target_count = int(target_count)
        if note.bucket == Bucket.AREA and not note.schedule:
            note.schedule = AreaSchedule(
                time=self.cfg.areas.default_time,
                duration_minutes=self.cfg.areas.default_duration_minutes,
            )
        note.log("habit", f"{cadence} x{target_count}")
        note.body = _area_body(note, self.cfg)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return _note_dict(note)

    def set_schedule(self, note_id: str, **fields: Any) -> Dict[str, Any]:
        """The "option to change" behind the weekly schedule review: move an
        Area's recurring block, resize it, pin its days, or pause the series
        without deleting the Area."""
        note = self.note(note_id)
        if note.bucket != Bucket.AREA:
            raise ValueError("only Areas carry a recurring schedule")
        current = (note.schedule or AreaSchedule(
            time=self.cfg.areas.default_time,
            duration_minutes=self.cfg.areas.default_duration_minutes,
        )).model_dump()
        for key, value in fields.items():
            if value is None or key not in current:
                continue
            if key == "days":
                current[key] = sorted({int(d) % 7 for d in value})
            elif key == "enabled":
                current[key] = bool(value)
            elif key in ("duration_minutes", "monthday"):
                current[key] = int(value)
            else:
                current[key] = value
        note.schedule = AreaSchedule(**current)
        note.log(
            "schedule",
            f"{note.schedule.time} · {note.schedule.duration_minutes}m · "
            f"{'on' if note.schedule.enabled else 'paused'}",
        )
        note.body = _area_body(note, self.cfg)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return _note_dict(note, category=taxonomy.categorize(note, self.cfg))

    # -- deadlines ----------------------------------------------------------

    def _pending_dates(self, active: List[Note]) -> List[Dict[str, Any]]:
        """Guessed dates waiting for a yes.

        Only unconfirmed ones, and only on live projects. The queue quotes the
        words each date was read from, because "is 2026-08-28 right?" is
        unanswerable without knowing that it came from "end of the week".
        """
        out = []
        for note in active:
            p = note.project
            if not p or not p.deadline or p.deadline_confirmed:
                continue
            out.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "deadline": p.deadline,
                    "source": p.deadline_source,
                    "phrase": p.deadline_phrase,
                    "why": _why_asking(p.deadline_source, p.deadline_phrase),
                    "category": taxonomy.categorize(note, self.cfg),
                }
            )
        out.sort(key=lambda d: d["deadline"])
        return out

    def set_deadline(
        self, note_id: str, date: Optional[str] = None, confirm: bool = False
    ) -> Dict[str, Any]:
        """Approve, correct or clear a due date.

        Three calls in one, because they are three answers to the same
        question:

          * `confirm=True` — "looks right". Keeps the date, keeps its
            provenance, marks it settled.
          * a `date` — "no, this one". Becomes `manual`, which is confirmed by
            definition and survives future re-parses.
          * neither — clear it. A project with no deadline is a legitimate
            answer, and better than a wrong one.

        Any change re-plans the project's work blocks around the new date and
        re-syncs the calendar, so the blocks never point at a date that moved.
        """
        note = self.note(note_id)
        if not note.project:
            raise ValueError("only projects carry a deadline")
        p = note.project

        if confirm and date is None:
            if not p.deadline:
                raise ValueError("nothing to confirm — this project has no deadline")
            p.deadline_confirmed = True
            note.log("deadline", f"confirmed {p.deadline.isoformat()}")
        else:
            picked = _as_date(date)
            if date and not picked:
                raise ValueError(f"could not read {date!r} as a date (want YYYY-MM-DD)")
            p.deadline = picked
            p.deadline_confirmed = bool(picked)
            p.deadline_source = extract.MANUAL if picked else ""
            p.deadline_phrase = ""
            note.log("deadline", picked.isoformat() if picked else "cleared")

        report = workflow.plan_project(
            note, self.cfg.planner, force=True, busy=self._busy(note_id)
        )
        note.body = _project_body(note)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return {
            "note": _note_dict(note),
            "message": report.message,
            "pending": len(self._pending_dates([n for n in self.notes(Bucket.PROJECT.value)
                                                if n.project
                                                and n.project.status != ProjectStatus.DONE])),
        }

    def set_category(self, note_id: str, category: Optional[str]) -> Dict[str, Any]:
        """Pin a colour keyword by hand. Passing nothing clears the override
        and hands the note back to keyword detection."""
        note = self.note(note_id)
        if category and category not in taxonomy.index(self.cfg):
            raise ValueError(f"unknown category {category!r}")
        note.category = category or None
        note.log("category", category or "auto")
        self.vault.save(note)
        self._sync_calendar_quiet()
        return _note_dict(note, category=taxonomy.categorize(note, self.cfg))

    def categories(self) -> List[Dict[str, Any]]:
        return taxonomy.as_dicts(self.cfg)

    # -- calendar -----------------------------------------------------------

    def sync_calendar(self, snapshot: Optional[List[Note]] = None) -> Dict[str, Any]:
        sink = get_sink(self.cfg)
        notes = self._snapshot() if snapshot is None else snapshot
        result = sink.sync(notes, self.cfg, len(self.decks.all()))
        self.vault.log_line("calendar", f"{sink.name}: {result}")
        return {"sink": sink.name, "result": result}

    def _sync_calendar_quiet(self, snapshot: Optional[List[Note]] = None) -> None:
        """Best-effort resync after a mutation; never fails a write because a
        calendar was unreachable."""
        try:
            self.sync_calendar(snapshot)
        except Exception as exc:
            self.vault.log_line("calendar", f"sync failed: {type(exc).__name__}: {exc}")

    def ics_path(self) -> Path:
        return self.cfg.ics_path

    def _google_status(self) -> Optional[Dict[str, Any]]:
        """Why Google sync will or won't work, without opening a browser or
        touching the network. A calendar that silently stops syncing should be
        answerable from `doctor`, not from reading a stack trace in a log."""
        if "google" not in (self.cfg.calendar.sink, self.cfg.resolved_task_sink()) and \
                self.cfg.calendar.sink != "both" and self.cfg.resolved_task_sink() != "both":
            return None
        from .calsync import _google_auth

        return _google_auth.status(self.cfg)

    # -- tutor: decks and cards ---------------------------------------------

    def deck(self, note_id: str, create: bool = False) -> Deck:
        """A note's deck, optionally creating an empty one.

        A deck is keyed by note id, not by file path, so graduating a Project
        to a Resource — which moves the note between folders — carries its
        cards and its whole review history with it untouched.
        """
        existing = self.decks.get(note_id)
        if existing:
            return existing
        note = self.note(note_id)  # raises if the note is gone
        if not create:
            raise ValueError(f"no deck for {note.title!r} yet — generate cards first")
        return Deck(
            note_id=note.id,
            subject=note.title,
            bucket=note.bucket.value,
            category=taxonomy.categorize(note, self.cfg),
        )

    def deck_dict(self, note_id: str) -> Dict[str, Any]:
        deck = self.deck(note_id)
        return self._deck_payload(deck)

    def _deck_payload(self, deck: Deck) -> Dict[str, Any]:
        progress = tutor.deck_progress(deck, self.cfg)
        return {
            **progress,
            "source_fingerprint": deck.source_fingerprint,
            "cards": [self._card_payload(deck, c) for c in deck.cards],
            "counts": {
                "total": progress["cards"],
                "active": progress["active"],
                "drafts": progress["drafts"],
            },
        }

    def _card_payload(self, deck: Deck, card: Card, *, reveal: bool = True) -> Dict[str, Any]:
        """`reveal=False` is what a session queue gets.

        Everything that contains the answer is withheld, not merely hidden by
        CSS — including `front`, because a cloze card's raw front is the
        sentence with the blank still filled in. An answer sitting in the page
        while you try to recall it is not a test.
        """
        return {
            "id": card.id,
            "note_id": deck.note_id,
            "subject": deck.subject,
            "category": deck.category,
            "kind": card.kind,
            "front": card.front if reveal else "",
            "back": card.back if reveal else "",
            "question": card.question(),
            "answer": card.answer() if reveal else "",
            "hint": card.hint,
            "source": card.source if reveal else "",
            "status": card.status,
            "reps": card.reps,
            "lapses": card.lapses,
            "stability": round(card.stability, 2),
            "difficulty": round(card.difficulty, 2),
            "due": card.due,
            "is_new": card.is_new,
            "retrievability": round(card.retrievability(), 3),
            "intervals": tutor.button_intervals(card, self.cfg),
        }

    def _link_resolver(self):
        """Build a `[[Some Note]]` → body lookup.

        One directory walk, no parsing, and then only the notes actually
        linked get opened — see `Vault.title_index`. The first version of this
        read and parsed the whole vault to build a title→body map, which made
        generating a single deck cost every file in the vault. A Quiz linking
        thirty atomic notes should read thirty notes, not two hundred.
        """
        index = self.vault.title_index()
        cache: Dict[str, Optional[str]] = {}

        def resolve(title: str) -> Optional[str]:
            key = (title or "").strip().lower()
            if key not in cache:
                other = self.vault.resolve_title(title, index)
                cache[key] = other.body if other else None
            return cache[key]

        return resolve

    def generate_cards(
        self, note_id: str, *, max_cards: Optional[int] = None, source: str = ""
    ) -> Dict[str, Any]:
        """Draft cards from a note (or from pasted source text).

        Everything lands as a draft. Nothing reaches the scheduler until it
        has been read — see the note at the top of sb/generate.py for why.
        """
        note = self.note(note_id)
        deck = self.deck(note_id, create=True)
        deck.subject = note.title
        deck.bucket = note.bucket.value
        deck.category = taxonomy.categorize(note, self.cfg)

        material = (source or "").strip() or note.body
        material = generate.expand_links(material, self._link_resolver())
        result = generate.add_to_deck(
            deck,
            material,
            self.cfg,
            max_cards=max_cards or self.cfg.study.generate_max_cards,
        )
        self.decks.save(deck)
        self.vault.log_line(
            "study", f"generated {len(result.cards)} cards for {note.id} ({result.provider})"
        )
        self._sync_calendar_quiet()
        return {
            "deck": self._deck_payload(deck),
            "generated": len(result.cards),
            "rejected": result.rejected,
            "passages": result.chunks,
            "provider": result.provider,
            "degraded": result.degraded,
            "note": result.note,
        }

    def add_card(self, note_id: str, front: str, back: str, **fields: Any) -> Dict[str, Any]:
        if not (front or "").strip() or not (back or "").strip():
            raise ValueError("a card needs both a question and an answer")
        deck = self.deck(note_id, create=True)
        deck.add(
            front=front.strip(),
            back=back.strip(),
            hint=str(fields.get("hint") or "").strip(),
            source=str(fields.get("source") or "").strip(),
            status="active",  # you typed it, you meant it
        )
        self.decks.save(deck)
        self._sync_calendar_quiet()
        return self._deck_payload(deck)

    def update_card(self, note_id: str, card_id: str, **fields: Any) -> Dict[str, Any]:
        """Edit, approve, suspend or delete one card.

        Editing the text does not reset the schedule. Fixing a typo in a
        question you have known for six months should not cost you those six
        months; if the meaning changed enough to matter, delete it and write a
        new one.
        """
        deck = self.deck(note_id)
        if fields.get("delete"):
            deck.cards = [c for c in deck.cards if c.id != card_id]
            self.decks.save(deck)
            return self._deck_payload(deck)

        card = deck.card(card_id)
        for key in ("front", "back", "hint", "source"):
            if fields.get(key) is not None:
                setattr(card, key, str(fields[key]).strip())
        status = fields.get("status")
        if status:
            if status not in ("draft", "active", "suspended"):
                raise ValueError(f"unknown card status {status!r}")
            card.status = status
        self.decks.save(deck)
        self._sync_calendar_quiet()
        return self._deck_payload(deck)

    def approve_drafts(self, note_id: str, card_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        deck = self.deck(note_id)
        wanted = set(card_ids or [])
        for card in deck.drafts:
            if not wanted or card.id in wanted:
                card.status = "active"
        self.decks.save(deck)
        self._sync_calendar_quiet()
        return self._deck_payload(deck)

    # -- tutor: studying ----------------------------------------------------

    def study_overview(self) -> Dict[str, Any]:
        """The study home screen: subjects, what is due, how it is going."""
        decks = self.decks.all()
        today = dt.date.today()
        reviewed, introduced = tutor.counted_today(self.decks)
        return {
            "decks": [tutor.deck_progress(d, self.cfg, today) for d in decks],
            "stats": tutor.stats(self.decks, decks, self.cfg),
            "today": {"reviewed": reviewed, "introduced": introduced},
            "limits": {
                "new_per_day": self.cfg.study.new_cards_per_day,
                "max_reviews": self.cfg.study.max_reviews_per_day,
                "session_size": self.cfg.study.session_size,
                "retention": self.cfg.study.desired_retention,
            },
            "graduation": self.graduation_candidates(),
            "candidates": self._deckable_notes(decks),
            "categories": taxonomy.as_dicts(self.cfg),
        }

    def _deckable_notes(self, decks: List[Deck]) -> List[Dict[str, Any]]:
        """Notes worth making cards from that do not have any yet."""
        have = {d.note_id for d in decks}
        out = []
        for note in self.notes():
            if note.id in have or note.bucket not in (Bucket.PROJECT, Bucket.RESOURCE):
                continue
            if note.bucket == Bucket.PROJECT and note.project and \
                    note.project.status == ProjectStatus.DONE:
                continue
            out.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "bucket": note.bucket.value,
                    "learning": bool(note.project and note.project.learning),
                    "category": taxonomy.categorize(note, self.cfg),
                    "words": len(note.body.split()),
                }
            )
        out.sort(key=lambda n: (not n["learning"], n["title"]))
        return out

    def study_session(
        self, subjects: Optional[List[str]] = None, limit: Optional[int] = None
    ) -> Dict[str, Any]:
        """Build a mixed-subject queue. Answers are posted one at a time, so
        this holds no server-side session state — close the tab mid-session
        and nothing is lost or double-counted."""
        decks = self.decks.all()
        reviewed, introduced = tutor.counted_today(self.decks)
        session = tutor.build_session(
            decks,
            self.cfg,
            subjects=subjects,
            limit=limit,
            reviewed_today=reviewed,
            introduced_today=introduced,
        )
        return {
            "queue": [
                {
                    **self._card_payload(q.deck, q.card, reveal=False),
                    "reason": q.reason,
                    "overdue_days": q.overdue_days,
                }
                for q in session.queue
            ],
            "due_available": session.due_available,
            "new_available": session.new_available,
            "capped": session.capped,
            "message": session.message,
        }

    def study_reveal(self, note_id: str, card_id: str) -> Dict[str, Any]:
        """The answer side, fetched only when asked for — so the answer is
        never sitting in the page while you are trying to recall it."""
        deck = self.deck(note_id)
        return self._card_payload(deck, deck.card(card_id), reveal=True)

    def study_answer(
        self,
        note_id: str,
        card_id: str,
        *,
        grade: Optional[int] = None,
        mode: str = "self",
        typed: str = "",
        seconds: float = 0.0,
    ) -> Dict[str, Any]:
        """Grade one card. `mode="recall"` marks a typed answer first."""
        deck = self.deck(note_id)
        card = deck.card(card_id)

        # A typed answer is marked here only when the caller has not already
        # settled on a grade. The UI marks first (see `study_mark`) and shows
        # you the verdict *before* it counts, so a model that marks you wrong
        # costs you a click rather than a card.
        grading = None
        if mode == "recall" and grade is None:
            grading = tutor.grade_recall(card.question(), card.answer(), typed, self.cfg)
            grade = grading.grade
        if grade is None:
            raise ValueError("no grade given")

        result = tutor.answer(
            self.decks,
            deck,
            card,
            int(grade),
            self.cfg,
            mode=mode,
            typed=typed,
            seconds=seconds,
            feedback=grading.feedback if grading else "",
            score=grading.score if grading else None,
        )
        graduation = self._check_graduation(deck)
        return {
            "card": self._card_payload(deck, card),
            "grade": result.grade,
            "interval_days": result.interval_days,
            "due": result.due,
            "again": result.again,
            "retrievability_before": round(result.retrievability_before, 3),
            "intervals": result.intervals,
            "marking": (
                {
                    "score": grading.score,
                    "feedback": grading.feedback,
                    "missed": grading.missed,
                    "graded_by": grading.graded_by,
                    "correct": grading.score >= 0.6,
                }
                if grading
                else None
            ),
            "deck": tutor.deck_progress(deck, self.cfg),
            "graduation": graduation,
        }

    def study_mark(self, note_id: str, card_id: str, typed: str) -> Dict[str, Any]:
        """Mark a typed answer without scheduling anything.

        Separating marking from grading is the whole reason free recall is
        safe to use: the model proposes, you dispose. Nothing is written to
        the deck or the review log until you accept a grade.
        """
        deck = self.deck(note_id)
        card = deck.card(card_id)
        grading = tutor.grade_recall(card.question(), card.answer(), typed, self.cfg)
        return {
            "score": grading.score,
            "grade": grading.grade,
            "feedback": grading.feedback,
            "missed": grading.missed,
            "graded_by": grading.graded_by,
            "correct": grading.score >= 0.6,
            "answer": card.answer(),
            "source": card.source,
        }

    def study_explain(self, note_id: str, card_id: str, question: str = "") -> Dict[str, Any]:
        deck = self.deck(note_id)
        card = deck.card(card_id)
        note = self.note(note_id)
        return {"answer": tutor.explain(card, note.body, question, self.cfg)}

    def study_stats(self) -> Dict[str, Any]:
        decks = self.decks.all()
        return tutor.stats(self.decks, decks, self.cfg)

    # -- the info manager (blueprint §7) -------------------------------------

    def ask(self, question: str, include_archive: bool = False, k: int = 6) -> Dict[str, Any]:
        """Answer a question from the vault, with citations.

        Builds the index on first use rather than making lj find a button:
        asking a question is a clear enough statement of intent that the
        system should just be ready.
        """
        if not self.index.exists():
            self.reindex()
        answer = askmod.ask(
            question, self.cfg, index=self.index, include_archive=include_archive, k=k
        )
        by_id = {}
        for source in answer.sources:
            nid = source["note_id"]
            if nid not in by_id:
                try:
                    by_id[nid] = taxonomy.categorize(self.note(nid), self.cfg)
                except ValueError:
                    by_id[nid] = taxonomy.FALLBACK
            source["category"] = by_id[nid]
        self.vault.log_line(
            "ask", f"{question[:120]!r} -> {len(answer.sources)} sources"
        )
        return {
            "question": question,
            "answer": answer.text,
            "sources": answer.sources,
            "used": answer.used,
            "semantic": answer.semantic,
            "searched_archive": answer.searched_archive,
            "grounded": answer.grounded,
            "provider": answer.provider,
            "note": answer.note,
        }

    # -- smart connections ---------------------------------------------------
    #
    # On demand only, and deliberately so. Relatedness costs an embedding
    # search plus a model call per note, and it is not information that goes
    # stale minute to minute — running it on every capture would tax every
    # write with work almost no capture needs. Nothing here touches the
    # calendar either: a link changes the graph, not the schedule.

    def connect(
        self,
        note_id: str,
        *,
        max_links: int = connectmod.MAX_LINKS,
        include_archive: bool = False,
        write: bool = True,
        allow_model: bool = True,
    ) -> Dict[str, Any]:
        """Find and link the notes related to one note.

        Reads the vault once, because the free tiers need every other note's
        title and links to work at all. That is the cost of an explicit,
        user-initiated action — not something on the capture path.
        """
        snapshot = self._snapshot()
        note = next((n for n in snapshot if n.id == note_id), None)
        if note is None:
            note = self.note(note_id)
        others = [n for n in connectmod.connectable(snapshot) if n.id != note.id]

        result = connectmod.connect_note(
            note,
            self.index,
            self.cfg,
            others=others,
            max_links=max_links,
            include_archive=include_archive,
            write=write,
            allow_model=allow_model,
        )
        if write and result.changed:
            self.vault.save(note)
            state = connectmod.ConnectState(self.cfg)
            data = state.load()
            data[note.id] = connectmod.fingerprint(note)
            state.save(data)
            self.vault.log_line(
                "connect", f"{note.id}  {len(result.links)} links  {note.title}"
            )
        return result.as_dict

    def connect_all(
        self,
        *,
        bucket: Optional[str] = None,
        max_links: int = connectmod.MAX_LINKS,
        include_archive: bool = False,
        reindex: bool = True,
        write: bool = True,
        changed_only: bool = True,
        allow_model: bool = True,
    ) -> Dict[str, Any]:
        """Connect every eligible note in one pass.

        Three things keep this cheap. The vault is read **once** and the same
        list feeds every note's free tiers. The title matcher is compiled
        **once** rather than per note. And `changed_only` skips notes whose
        content has not moved since they were last connected, so a second run
        over an untouched vault costs one hash each.

        `reindex` first by default: this is the one operation whose whole job
        is to be current, and the index build is incremental, so an unchanged
        vault costs a fingerprint check rather than a re-embed.
        """
        snapshot = self._snapshot()
        if reindex:
            self.index.build(snapshot)

        pool = connectmod.connectable(snapshot)
        targets = pool
        if bucket:
            want = Bucket(bucket)
            targets = [n for n in targets if n.bucket is want]

        # Compiled once for the whole pass — this is the tier that replaces
        # most of the model calls, and rebuilding it per note would hand the
        # saving straight back.
        matcher = connectmod.title_matcher(pool)

        state = connectmod.ConnectState(self.cfg)
        seen = state.load() if changed_only else {}

        results, changed, skipped, model_calls = [], 0, 0, 0
        for note in targets:
            mark = connectmod.fingerprint(note)
            if changed_only and seen.get(note.id) == mark:
                skipped += 1
                results.append(
                    connectmod.ConnectResult(
                        note_id=note.id, title=note.title, skipped=True
                    ).as_dict
                )
                continue

            others = [n for n in pool if n.id != note.id]
            result = connectmod.connect_note(
                note,
                self.index,
                self.cfg,
                others=others,
                matcher=matcher,
                max_links=max_links,
                include_archive=include_archive,
                write=write,
                allow_model=allow_model,
            )
            model_calls += 1 if result.used_model else 0
            if write and result.changed:
                self.vault.save(note)
                changed += 1
            if write:
                seen[note.id] = connectmod.fingerprint(note)
            results.append(result.as_dict)

        if write and changed_only:
            state.save(seen)

        self.vault.log_line(
            "connect",
            f"pass over {len(targets)} notes: {changed} changed, "
            f"{skipped} skipped, {model_calls} model calls",
        )
        return {
            "scanned": len(targets),
            "changed": changed,
            "skipped": skipped,
            "model_calls": model_calls,
            "linked": sum(len(r["links"]) for r in results),
            "by_source": _link_sources(results),
            "results": results,
        }

    def reindex(self, force: bool = False) -> Dict[str, Any]:
        """Bring the retrieval index in line with the vault.

        Incremental unless forced — editing one note re-embeds one note. See
        the module docstring in sb/index.py for why the index is allowed to
        live under `_system/`.
        """
        # Free to do here — reindex already walks every note, and a filename
        # out of step with its title silently breaks the `[[links]]` pointing
        # at it. Never run on the capture path.
        repaired = self.vault.repair_filenames()
        if repaired:
            self.vault.log_line("vault", f"renamed {len(repaired)}: {'; '.join(repaired)}")

        stats = self.index.build(self.notes(), force=force)
        self.vault.log_line("index", str(stats))
        return {**stats, "renamed": repaired, "status": self.index.status()}

    def index_status(self) -> Dict[str, Any]:
        """What the index knows, and whether it is stale.

        Staleness is computed from the notes rather than trusted from a flag,
        because the vault can be edited in Obsidian while the app is closed —
        which is the whole point of storing everything as files.
        """
        status = self.index.status()
        from .index import fingerprint as note_fingerprint

        indexed = {}
        chunks, _, _ = self.index.load()
        for chunk in chunks:
            indexed.setdefault(chunk.get("note_id"), chunk.get("fingerprint"))

        stale, missing = 0, 0
        live = [
            n for n in self.notes()
            if n.bucket in (Bucket.RESOURCE, Bucket.ARCHIVE)
        ]
        for note in live:
            if note.id not in indexed:
                missing += 1
            elif indexed[note.id] != note_fingerprint(note):
                stale += 1
        gone = len(set(indexed) - {n.id for n in live})
        return {
            **status,
            "indexable": len(live),
            "missing": missing,
            "stale": stale,
            "removed": gone,
            "current": not (missing or stale or gone),
        }

    # -- tutor: graduation (blueprint §4) -----------------------------------

    def _check_graduation(self, deck: Deck) -> Optional[Dict[str, Any]]:
        """Mark a learning Project as awaiting confirmation once its deck says
        the material has stuck. The move itself stays lj's — the system's job
        is to notice, not to decide (§4)."""
        if not tutor.is_ready_to_graduate(deck, self.cfg):
            return None
        try:
            note = self.note(deck.note_id)
        except ValueError:
            return None
        if note.bucket != Bucket.PROJECT or not note.project or not note.project.learning:
            return None
        progress = tutor.deck_progress(deck, self.cfg)
        if note.project.status != ProjectStatus.GRADUATING:
            note.project.status = ProjectStatus.GRADUATING
            note.log("mastered", f"mastery={progress['mastery']:.0%}")
            self._roll_up_srs(note, deck, progress)
            self.vault.save(note)
        return {
            "note_id": note.id,
            "title": note.title,
            "mastery": progress["mastery"],
            "mature": progress["mature"],
            "active": progress["active"],
        }

    def _roll_up_srs(self, note: Note, deck: Deck, progress: Dict[str, Any]) -> None:
        """Mirror the deck's headline numbers into the note's `srs:` block, so
        the frontmatter you see in Obsidian tells you where a Project stands
        without opening the app."""
        active = deck.active
        note.srs = SrsState(
            reps=sum(c.reps for c in active),
            lapses=sum(c.lapses for c in active),
            interval_days=round(
                sum(c.stability for c in active) / len(active), 2
            ) if active else 0.0,
            due=progress["next_due"],
            last_review=max((c.last_review for c in active if c.last_review), default=None),
            mastery=progress["mastery"],
        )

    def graduation_candidates(self) -> List[Dict[str, Any]]:
        """Learning Projects whose decks say they are done. The dashboard's
        "ready to graduate?" prompt reads this."""
        # One vault read for the whole sweep: self.note() rescans every file,
        # and the dashboard calls this on every load.
        by_id = {n.id: n for n in self.notes()}
        out = []
        for deck in self.decks.all():
            if not tutor.is_ready_to_graduate(deck, self.cfg):
                continue
            note = by_id.get(deck.note_id)
            if not note or note.bucket != Bucket.PROJECT:
                continue
            if not note.project or not note.project.learning:
                continue
            progress = tutor.deck_progress(deck, self.cfg)
            out.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "mastery": progress["mastery"],
                    "mature": progress["mature"],
                    "active": progress["active"],
                    "retention": progress["retention"],
                }
            )
        return out

    # -- health -------------------------------------------------------------

    def health(self) -> Dict[str, Any]:
        from .llm import get_provider, lane_report

        provider = get_provider(self.cfg.llm)
        available = provider.available()
        models = provider.models() if available and hasattr(provider, "models") else []
        lanes = lane_report(self.cfg.llm) if available else None
        return {
            "vault": str(self.cfg.vault),
            "vault_exists": self.cfg.vault.exists(),
            "counts": self.vault.counts(),
            "llm": {
                "provider": provider.name,
                "model": self.cfg.llm.model if getattr(provider, "is_llm", False) else "rule-based",
                "available": available,
                "installed_models": models,
                "fallback": self.cfg.llm.fallback_to_heuristic,
                # Which model does which job, and whether each is really on
                # disk — a study model named but not pulled degrades silently
                # otherwise, and "answers got worse" is a miserable thing to
                # debug from the outside.
                "lanes": lanes,
            },
            "index": self.index.status(),
            "study": {
                "decks": len(self.decks.all()),
                "retention": self.cfg.study.desired_retention,
                "path": str(self.cfg.deck_dir),
            },
            "calendar": {
                "sink": self.cfg.calendar.sink,
                "task_sink": self.cfg.resolved_task_sink(),
                "ics": str(self.cfg.ics_path),
                "categories": len(taxonomy.table(self.cfg)),
                "google": self._google_status(),
            },
        }


# --------------------------------------------------------------------------
# note body rendering — the human-readable half of the file
# --------------------------------------------------------------------------


def _as_date(value: Any) -> Optional[dt.date]:
    """Read a date the UI sent. A picker only ever sends YYYY-MM-DD, but the
    same endpoint is reachable from the CLI and from curl."""
    if isinstance(value, dt.date):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError:
        return None


def _why_asking(source: str, phrase: str) -> str:
    """One line explaining why a date is in the approval queue."""
    quoted = f"\u201c{phrase}\u201d" if phrase else ""
    if source == extract.AMBIGUOUS and quoted:
        return f"read from {quoted} — people read that two ways"
    if source == extract.LLM:
        return f"the model suggested this, not the date rules{f' ({quoted})' if quoted else ''}"
    if not source:
        # A project captured before dates carried provenance. There is nothing
        # to quote, and the "next Friday" semantics changed underneath it, so
        # one look is exactly what it deserves.
        return "set before dates were checked — worth one look"
    return f"read from {quoted}" if quoted else "the parser had to interpret this"


def _link_sources(results: List[Dict[str, Any]]) -> Dict[str, int]:
    """How many links each tier produced. The point of the tiering is that
    `judged` should be the smallest number here, so it is worth reporting."""
    counts: Dict[str, int] = {}
    for r in results:
        for link in r.get("links", []):
            key = link.get("source") or "unknown"
            counts[key] = counts.get(key, 0) + 1
    return counts


def _note_dict(note: Note, **extra) -> Dict[str, Any]:
    data = note.model_dump(mode="json")
    data.update(extra)
    return data


#: The `##` headings `_project_body` writes itself and may therefore rewrite.
#: Everything else in a Project body belongs to lj and is carried through
#: untouched — see `_preserved_sections`.
OWNED_PROJECT_HEADINGS = {"steps", "materials", "skills", "capture"}

#: Headings absorbed into `project.materials` rather than preserved, so the
#: old hand-written Hardware/Software sections migrate into tracked state the
#: first time a note is re-rendered instead of being duplicated forever.
ABSORBED_HEADINGS = {
    "materials": MaterialKind.MATERIAL,
    "hardware": MaterialKind.HARDWARE,
    "software": MaterialKind.SOFTWARE,
}

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.M)
_BULLET = re.compile(r"^\s*[-*]\s+(?:\[( |x|X)\]\s+)?(.*?)\s*$")


def _split_sections(body: str) -> Tuple[str, List[Tuple[str, str]]]:
    """Split a note body into (preamble, [(heading, content), ...]).

    Only `##`-and-deeper headings split; the `# Title` line stays in the
    preamble because the renderer always rewrites it.
    """
    matches = list(_HEADING.finditer(body or ""))
    if not matches:
        return body or "", []
    preamble = body[: matches[0].start()]
    sections: List[Tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(2).strip(), body[m.end() : end]))
    return preamble, sections


def _preserved_sections(sections: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """Sections `_project_body` did not write and must not throw away.

    This is the fix for the whole class of "the dashboard ate my notes" bugs:
    a Project body used to be regenerated wholesale from frontmatter, so any
    heading the renderer did not know about — an Assignment's `## Answers`,
    a Quiz's `## Key Concepts` — silently vanished the next time a step was
    ticked. Now the renderer owns its own sections and nothing else.
    """
    out: List[Tuple[str, str]] = []
    for heading, content in sections:
        key = heading.lower().rstrip(":")
        if key in OWNED_PROJECT_HEADINGS or key in ABSORBED_HEADINGS:
            continue
        if content.strip():
            out.append((heading, content))
    return out


def _absorb_materials(note: Note, sections: List[Tuple[str, str]]) -> None:
    """Fold the body's Materials/Hardware/Software bullets into frontmatter.

    Runs before rendering, and does two jobs at once. It migrates the old
    body-only Hardware and Software sections into `project.materials` the
    first time a note is touched, and it reads back the checkbox state so
    ticking a material off in Obsidian survives instead of being overwritten
    by the frontmatter's copy on the next render.
    """
    p = note.project
    if not p:
        return
    by_text = {m.text.strip().lower(): m for m in p.materials}
    for heading, content in sections:
        kind = ABSORBED_HEADINGS.get(heading.lower().rstrip(":"))
        if kind is None:
            continue
        for line in content.splitlines():
            bullet = _BULLET.match(line)
            if not bullet:
                continue
            text = bullet.group(2).strip()
            if not text or text.startswith("<!--"):
                continue
            done = (bullet.group(1) or "").lower() == "x"
            existing = by_text.get(text.lower())
            if existing is None:
                material = Material(text=text, kind=kind, done=done)
                p.materials.append(material)
                by_text[text.lower()] = material
            else:
                existing.done = done
                # A bullet found under Hardware/Software is better evidence of
                # its kind than a default that was never chosen.
                if existing.kind == MaterialKind.MATERIAL:
                    existing.kind = kind


def _project_body(note: Note) -> str:
    """Render the Project note body: a checklist Obsidian can display and you
    can tick by hand. Frontmatter stays the machine's copy of the truth; this
    is the view a human reads.

    Only the sections listed in `OWNED_PROJECT_HEADINGS` are regenerated.
    Anything else lj wrote is preserved verbatim, ahead of `## Capture`.
    """
    p = note.project
    _, sections = _split_sections(note.body)
    _absorb_materials(note, sections)
    preserved = _preserved_sections(sections)
    original = _capture_section(note.body, sections)

    lines = [f"# {note.title}", ""]
    if p:
        if p.ideal_end:
            lines += [f"**Done means:** {p.ideal_end}", ""]
        bits = [f"Level {p.level}/5", f"~{p.estimate_minutes} min"]
        if p.deadline:
            bits.append(f"due {p.deadline.isoformat()}")
        lines += ["*" + " · ".join(bits) + "*", ""]
        if p.steps:
            lines += ["## Steps", ""]
            for s in p.steps:
                mark = "x" if s.done else " "
                when = f" — {s.scheduled:%a %d %b %H:%M}" if s.scheduled and not s.done else ""
                lines.append(f"- [{mark}] {s.text} ({s.minutes}m){when}")
            lines.append("")
        if p.materials:
            lines += ["## Materials", ""]
            plain = p.materials_of(MaterialKind.MATERIAL)
            lines += [f"- [{'x' if m.done else ' '}] {m.text}" for m in plain]
            if plain:
                lines.append("")
            for kind, label in (
                (MaterialKind.HARDWARE, "Hardware"),
                (MaterialKind.SOFTWARE, "Software"),
            ):
                group = p.materials_of(kind)
                if group:
                    lines += [f"### {label}", ""]
                    lines += [f"- [{'x' if m.done else ' '}] {m.text}" for m in group]
                    lines.append("")
        if p.skills:
            lines += ["## Skills", "", " ".join(f"#{_tag(s)}" for s in p.skills), ""]
    for heading, content in preserved:
        lines += [f"## {heading}", "", content.strip(), ""]
    lines += ["## Capture", "", original.strip(), ""]
    return "\n".join(lines)


def _area_body(note: Note, cfg: Optional[Config] = None) -> str:
    """Render the Area note body. An Area has no deadline and no task — the
    line that matters here is when it recurs."""
    original = _original_capture(note.body)
    sched = note.schedule
    cadence = note.habit.cadence if note.habit else Cadence.WEEKLY
    lines = [
        f"# {note.title}",
        "",
        "*Area — ongoing. No due date: it recurs on the calendar instead.*",
        "",
    ]
    if sched:
        when = calevents._days_label(sched, note)
        state = "" if sched.enabled else "  ·  **paused**"
        target = note.habit.target_count if note.habit else 1
        lines += [
            f"**Recurring:** {when} · {sched.duration_minutes} min{state}",
            f"*Target {target}× per {cadence.value} — change it at the weekly schedule review.*",
            "",
        ]
    capture, log = _split_checkin_log(original)
    lines += ["## Capture", "", capture.strip(), "", "## Check-in log", ""]
    if log.strip():
        lines += [log.strip(), ""]
    return "\n".join(lines)


CHECKIN_HEADING = "## Check-in log"


def _split_checkin_log(text: str) -> tuple:
    """An Area body is re-rendered whenever its schedule changes, so the two
    human-owned parts — the original capture and the check-in log lj writes
    into — have to survive the round trip intact, and the headings must not
    stack up."""
    marker = "\n" + CHECKIN_HEADING
    if marker in text:
        capture, log = text.split(marker, 1)
        return capture, log
    if text.lstrip().startswith(CHECKIN_HEADING):
        return "", text.lstrip()[len(CHECKIN_HEADING):]
    return text, ""


def _capture_section(body: str, sections: List[Tuple[str, str]]) -> str:
    """The raw capture, and only the raw capture.

    `_original_capture` takes everything after the `## Capture` heading, which
    is right for an Area (its check-in log lives down there) but wrong for a
    Project now that other sections are preserved: a heading lj added below
    the capture would be both preserved *and* swallowed into the capture text,
    appearing twice. Stopping at the next heading is what keeps re-rendering
    idempotent.
    """
    for heading, content in sections:
        if heading.lower().rstrip(":") == "capture":
            return content
    return body


def _original_capture(body: str) -> str:
    """Recover the raw capture from a previously rendered body so re-rendering
    is idempotent and never nests '## Capture' sections."""
    marker = "\n## Capture\n"
    if marker in body:
        return body.split(marker, 1)[1]
    return body


def _tag(text: str) -> str:
    import re

    return re.sub(r"[^\w/-]", "-", text.strip()).strip("-").lower() or "skill"
