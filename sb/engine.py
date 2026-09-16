"""The application service — one object the UI, the CLI and (later) the
scheduler all talk to.

Keeping this layer separate from the HTTP layer means the same operations are
scriptable, testable without a server, and reusable by the background jobs
that will drive habit check-ins and Resource reviews.
"""

from __future__ import annotations

import datetime as dt
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import (
    ask as askmod,
    calibration,
    collected,
    connect as connectmod,
    digest,
    extract,
    fit as fitmod,
    forecasting,
    frontmatter as frontmattermod,
    generate,
    guide as guidemod,
    habits as habitsmod,
    incidents as incidentsmod,
    intake as intakemod,
    jobqueue,
    lint,
    parser,
    quality,
    reminders as remindersmod,
    render as rendermod,
    retention,
    segment,
    session as sessionmod,
    tasksplit,
    threshold,
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
    HabitEvent,
    HabitMeta,
    Material,
    MaterialKind,
    Note,
    ProjectStatus,
    ReviewMeta,
    SrsState,
)
from .models import now as _now
from .labels import LabelStore
from .vault import Vault


class Engine:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.vault = Vault(cfg.vault)
        self.vault.ensure_structure()
        self.decks = DeckStore(cfg.vault)
        self.index = Index(cfg)
        # Labelled examples, in `_labels/` rather than `_system/`, because
        # unlike a cache nothing here can be regenerated. See sb/labels.py.
        self.labels = LabelStore(cfg.vault)
        #: The folder watcher and the dashboard button call `intake()` on
        #: different threads, and both start by listing the same folder. Two
        #: runs overlapping would read one dropped file twice and file it as
        #: two notes, so the second caller is told the first is already on it
        #: rather than made to wait for a duplicate.
        self._intake_lock = threading.Lock()
        #: Problems that happened on a background thread, so the dashboard can
        #: say so instead of leaving them in a log file. See sb/incidents.py.
        self.incidents = incidentsmod.IncidentStore(cfg.system_dir / "logs")
        #: Card generation requests made while no model was reachable.
        #: See sb/jobqueue.py.
        self.card_queue = jobqueue.CardQueue(cfg.system_dir)
        #: The open note-taking session, if there is one — "which folder am I
        #: filing into right now". Disposable by design; see sb/session.py.
        self.sessions = sessionmod.SessionStore(cfg.system_dir)
        #: The shape every note takes. Read from `_templates/` and re-read
        #: when lj edits one, so a template change lands on the next capture
        #: rather than the next restart. See sb/render.py.
        self.templates = rendermod.TemplateStore(cfg.vault)
        self.ensure_drop_folder()

    # -- capture ------------------------------------------------------------

    def capture(
        self,
        text: str,
        bucket: str,
        title: str = "",
        due: Optional[str] = None,
        folder: str = "",
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

        # A session says which folder, never which bucket. An essay mentioned
        # in a lecture is still a Project; it just belongs under that class
        # rather than loose in 20-Projects, and `path_in` anchors the folder
        # to whichever bucket the note actually landed in.
        where, session = self._session_folder(folder)

        # One read, reused by the planner and the calendar sync below.
        snapshot = self._snapshot()

        info = self._apply_bucket(note, target, snapshot, due=due)

        path = self.vault.write(
            note, self.vault.path_in(note, where) if where else None
        )
        self._session_record(session, note.id)
        self.vault.log_line("capture", f"{note.bucket.value}  {note.id}  {note.title}")
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {"note": _note_dict(note), "path": str(path), **info}

    def _shape(
        self,
        note: Note,
        objective: str,
        filled: Optional[Dict[str, str]] = None,
        *,
        extra: Optional[List[Tuple[str, str]]] = None,
    ) -> None:
        """Give a note the shape its template says it has, and the defaults the
        template says go with it.

        Called on the paths where the system *composes* a note. It sets the
        body and, for anything the note has not already been given explicitly,
        the tags, category and review cycle the template carries — which is
        what makes "a Term is tagged `term` and reviewed every 90 days" a fact
        about `_templates/Term.md` rather than a constant in this file.

        Never raises. A template being edited is not a reason to lose a
        capture, so a failure here leaves the note with the body it already
        had — see sb/render.py.
        """
        filled = dict(filled or {})
        # A section named `__preamble__` is the line that sits under the title,
        # above the first heading — a session's date and counts, say. It is not
        # a heading of its own, so it cannot be passed as one.
        preamble = filled.pop("__preamble__", "")
        try:
            template = self.templates.for_objective(objective)
            note.body = self.templates.body_for(
                objective, note.title, filled, preamble=preamble, extra=extra
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.vault.log_line("template", f"{objective} render failed: {exc!r}")
            return
        defaults = template.defaults()
        if not note.tags and defaults.get("tags"):
            note.tags = [str(t) for t in defaults["tags"]]
        if not note.category and defaults.get("category"):
            note.category = str(defaults["category"])
        review = defaults.get("review")
        if isinstance(review, dict) and review.get("cycle_days") and not note.review:
            note.review = ReviewMeta(
                cycle_days=int(review["cycle_days"]),
                next=dt.date.today() + dt.timedelta(days=int(review["cycle_days"])),
            )

    # -- note-taking sessions (housekeeping 2) ------------------------------

    def _bucket_folders(self, bucket: Bucket) -> List[str]:
        """Top-level subfolders lj has made under one bucket.

        The candidate list `resolve_folder` matches against. Directory names
        only — no note is read, so offering the list costs one `iterdir`.
        """
        root = self.vault.dir_for(bucket)
        try:
            return sorted(p.name for p in root.iterdir()
                          if p.is_dir() and not p.name.startswith((".", "_")))
        except OSError:
            return []

    def session_start(self, course: str, chapter: str = "") -> Dict[str, Any]:
        """Open a session. Every capture until it closes lands in its folder.

        Starting one while another is open closes the first rather than
        refusing: the realistic reason it happens is that lj walked out of one
        lecture into the next and never pressed anything in between, and
        losing the second session's notes to a modal error would be the worst
        possible answer to that.
        """
        course = sessionmod.clean_folder_name(course)
        if not course:
            raise ValueError("a session needs a class")

        closed = None
        if self.sessions.open() is not None:
            closed = self.session_end()

        # Resources are where class notes live, so that is the folder list
        # worth matching against — see `capture` for what happens to a
        # Project captured mid-lecture.
        course_folder, how = sessionmod.resolve_folder(
            self._bucket_folders(Bucket.RESOURCE), course
        )
        chapters = []
        base = self.vault.dir_for(Bucket.RESOURCE) / course_folder
        if base.is_dir():
            chapters = sorted(p.name for p in base.iterdir()
                              if p.is_dir() and not p.name.startswith((".", "_")))
        chapter_folder, chapter_how = sessionmod.resolve_folder(chapters, chapter)

        session = sessionmod.Session(
            id=sessionmod.new_id(course_folder, chapter_folder),
            course=course_folder,
            chapter=chapter_folder,
            started=_now().isoformat(timespec="seconds"),
        )
        # Made now rather than on the first capture, so the folder is there to
        # be opened in Obsidian while the lecture is still happening.
        (self.vault.dir_for(Bucket.RESOURCE) / session.folder).mkdir(
            parents=True, exist_ok=True
        )
        self.sessions.save(session)
        self.vault.log_line("session", f"start  {session.id}  {session.folder}")
        return {
            "session": session.as_dict(),
            "matched": how,
            "chapter_matched": chapter_how,
            "closed": closed,
        }

    def session_status(self) -> Dict[str, Any]:
        session = self.sessions.open()
        return {"session": session.as_dict() if session else None}

    def session_end(self) -> Dict[str, Any]:
        """Close the session and write its summary.

        The summary is built from the session's own list of ids, so it can
        only ever claim notes that were actually taken. A note lj deleted or
        moved in Obsidian mid-lecture is simply absent from it rather than
        appearing as a dead link — the ids are looked up, and one that no
        longer resolves is dropped.
        """
        session = self.sessions.open()
        if session is None:
            raise ValueError("no session is open")

        notes: List[Dict[str, Any]] = []
        terms: List[Dict[str, Any]] = []
        term_ids = set(session.term_ids)
        for note_id in session.note_ids:
            try:
                note = self.note(note_id)
            except ValueError:
                # Moved or deleted since (VaultError subclasses ValueError).
                # Absent from the summary beats a dead link in it.
                continue
            row = {
                "id": note.id,
                "title": note.title,
                "needs_definition": self.UNDEFINED_TAG in (note.tags or []),
            }
            notes.append(row)
            if note.id in term_ids:
                terms.append(row)

        ended = _now()
        summary = Note.capture(
            "", Bucket.RESOURCE,
            title=f"Session — {session.course}"
                  f"{' · ' + session.chapter if session.chapter else ''}"
                  f" — {ended.strftime('%Y-%m-%d')}",
        )
        self._shape(summary, "session", sessionmod.summary_sections(
            session, notes, terms, ended
        ))
        snapshot = self._snapshot()
        info = self._apply_bucket(summary, Bucket.RESOURCE, snapshot, shape=False)
        path = self.vault.write(summary, self.vault.path_in(summary, session.folder))

        session.ended = ended.isoformat(timespec="seconds")
        session.summary_id = summary.id
        self.sessions.clear()
        self.vault.log_line(
            "session",
            f"end  {session.id}  {len(notes)} notes  {len(terms)} terms",
        )
        return {
            "session": session.as_dict(),
            "summary": _note_dict(summary),
            "path": str(path),
            "notes": notes,
            "terms": terms,
            "undefined": [t for t in terms if t["needs_definition"]],
            **info,
        }

    def _session_folder(self, explicit: str = "") -> Tuple[str, Any]:
        """(folder to file into, the open session or None).

        An explicit folder always wins. Saying where a note goes is a
        decision, and a session is a default — a default that overrode a
        decision would make the session something to fight rather than
        something to forget about.
        """
        session = self.sessions.open()
        if explicit:
            return explicit, session
        return (session.folder if session else ""), session

    def _session_record(self, session, note_id: str, *, term: bool = False) -> None:
        if session is None:
            return
        session.add(note_id, term=term)
        self.sessions.save(session)

    # -- terms (housekeeping 1) ---------------------------------------------

    #: A term with no definition yet. A tag rather than a frontmatter field on
    #: purpose: it shows up in Obsidian's search and tag pane, which is where
    #: lj is when they go to fill one in, and it disappears by being edited
    #: out rather than by a button only this app knows about.
    UNDEFINED_TAG = "needs-definition"

    def capture_term(
        self,
        term: str,
        definition: str = "",
        *,
        source: str = "",
        folder: str = "",
    ) -> Dict[str, Any]:
        """One term, one note, and — if it has a definition — one card.

        Highlighting a word and pressing the capture hotkey is the single most
        common thing that happens while reading, and until now it produced an
        Inbox line reading "elasticity" that had to be opened, retyped and
        classified later. A term is a known shape: it has a name, it has a
        definition, and the only thing anyone ever wants to do with it
        afterwards is be asked it again.

        **The card is written here rather than generated.** A term note is
        twelve words long, and `generate_folder` skips anything under forty as
        too thin — so the notes lj most wants to be quizzed on were exactly
        the ones the deck machinery would never touch. Front and back are the
        term and the definition lj typed, so there is nothing for a model to
        add and nothing for it to get wrong: this is the one study path that
        works with Ollama closed.

        It lands `active` rather than `draft` for the same reason
        `add_card` does — the draft rule exists to stop *generated* cards
        reaching the scheduler unread, and lj wrote both sides of this one.

        A term captured without a definition is still worth keeping — it is
        the thing you did not know, which is the whole point — so it is filed
        and tagged `needs-definition` rather than refused.
        """
        term = (term or "").strip()
        definition = (definition or "").strip()
        if not term:
            raise ValueError("a term needs a name")
        if len(term) > 120:
            raise ValueError("that is a passage, not a term — capture it as a note")

        note = Note.capture(definition or term, Bucket.RESOURCE, title=term)
        # The template supplies the shape, the headings and the tags; the
        # `needs-definition` marker is a fact about *this* term rather than
        # about terms in general, so it is added here and not in the file.
        self._shape(note, "term", {
            "Definition": definition,
            "Seen in": f"- {source}" if source else "",
        })
        if not definition:
            note.tags = list(note.tags) + [self.UNDEFINED_TAG]

        where, session = self._session_folder(folder)
        snapshot = self._snapshot()
        info = self._apply_bucket(note, Bucket.RESOURCE, snapshot, shape=False)
        path = self.vault.write(
            note, self.vault.path_in(note, where) if where else None
        )
        self._session_record(session, note.id, term=True)
        self.vault.log_line("capture", f"term  {note.id}  {note.title}")

        carded = False
        if definition:
            deck = self.deck(note.id, create=True)
            deck.add(
                front=term,
                back=definition,
                source=source,
                status="active",
            )
            self.decks.save(deck)
            carded = True

        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {
            "note": _note_dict(note),
            "path": str(path),
            "carded": carded,
            "needs_definition": not definition,
            **info,
        }

    # -- screenshots (housekeeping 7) ---------------------------------------

    #: Images live beside the notes that embed them, in a folder Obsidian
    #: already treats as attachments. Not `_system/`: this is lj's material,
    #: it cannot be regenerated, and `_system/` is the one place the doctor is
    #: allowed to suggest deleting.
    ATTACH_DIR = "_attachments"

    def attach_image(
        self,
        data: bytes,
        *,
        caption: str = "",
        source: str = "",
        suffix: str = ".png",
    ) -> Dict[str, Any]:
        """A screenshot becomes a note with the image in it.

        Not a loose file in an attachments folder — a note. Everything else
        in this system is a note, which is what makes everything else
        searchable, linkable, reviewable and countable, and an image filed as
        an exception to that is an image nobody finds again. A diagram off a
        lecture slide is a Resource that happens to be a picture.

        During a session it lands in the session's folder and joins the
        summary, which is the case this exists for: the slide you snipped at
        11:15 is in the chapter it was shown in, not in Downloads.

        The embed is `![[name.png]]` — Obsidian resolves an embed by filename
        from anywhere in the vault, so the link survives the note being moved
        later by the group-file box, which a relative path would not.
        """
        if not data:
            raise ValueError("no image data")
        title = (caption or "").strip() or f"Screenshot — {_now().strftime('%d %b %Y, %H:%M')}"

        note = Note.capture(caption or "", Bucket.RESOURCE, title=title)
        where, session = self._session_folder()

        stamp = _now().strftime("%Y%m%dT%H%M%S")
        image_dir = self.vault.dir_for(Bucket.RESOURCE)
        for part in (where or "").split("/"):
            if part.strip() and part not in (".", ".."):
                image_dir = image_dir / part.strip()
        image_dir = image_dir / self.ATTACH_DIR
        image_dir.mkdir(parents=True, exist_ok=True)

        image_path = image_dir / f"screenshot-{stamp}{suffix}"
        n = 2
        while image_path.exists():
            image_path = image_dir / f"screenshot-{stamp}-{n}{suffix}"
            n += 1
        image_path.write_bytes(data)

        embed = f"![[{image_path.name}]]"
        if caption.strip():
            embed += "\n\n" + caption.strip()
        self._shape(note, "screenshot", {
            "Image": embed,
            "Seen in": f"- {source}" if source else "",
        })

        snapshot = self._snapshot()
        info = self._apply_bucket(note, Bucket.RESOURCE, snapshot, shape=False)
        path = self.vault.write(
            note, self.vault.path_in(note, where) if where else None
        )
        self._session_record(session, note.id)
        self.vault.log_line("capture", f"screenshot  {note.id}  {image_path.name}")
        return {
            "note": _note_dict(note),
            "path": str(path),
            "image": str(image_path),
            "image_name": image_path.name,
            "folder": where,
            **info,
        }

    # -- the collector's fallacy (housekeeping 6) ---------------------------

    def collected_debt(self, limit: int = 40) -> Dict[str, Any]:
        """Which notes were kept and never used. See sb/collected.py.

        One vault walk and one deck read for the whole report — the same
        pattern every other whole-vault view here uses, because a per-note
        deck lookup over six hundred notes is the arithmetic that quietly
        turns a panel into a page that takes four seconds to open.

        Sorted oldest first. The oldest untouched note is the one most likely
        to be genuinely dead, and a list that starts with last week's is a
        list that never reaches it.
        """
        decks = {d.note_id: d for d in self.decks.all()}
        where = self.vault.folders_by_id()
        rows = [
            collected.assess(note, decks.get(note.id))
            for note in self.notes()
        ]
        stats = collected.summarise(rows)
        items = [r for r in rows if r["eligible"] and not r["used"]]
        items.sort(key=lambda r: -(r["age_days"] or 0))
        for row in items:
            row["folder"] = where.get(row["note_id"], "")
        return {
            **stats,
            "sentence": collected.sentence(stats),
            "items": items[: max(0, int(limit))] if limit else items,
            "shown": min(len(items), limit) if limit else len(items),
            # The sharper special case: a term met and never understood. It
            # has its own list because it is one action away from being fixed
            # and the general list is not.
            "undefined_terms": self.undefined_terms()[:20],
        }

    # -- filing several notes at once (housekeeping 5) -----------------------

    def folders(self) -> Dict[str, List[str]]:
        """Every folder lj has made, by bucket, plus the union.

        Names only, from `iterdir` — no note is read. The union is what the
        group-file box matches against, because a folder is a subject and a
        subject is not the property of one bucket: `Federal Taxation` exists
        under Projects and under Resources and is the same thing both times.
        """
        by_bucket = {b.value: self._bucket_folders(b) for b in Bucket}
        every = sorted({f for names in by_bucket.values() for f in names})
        return {"by_bucket": by_bucket, "all": every}

    def move_to_folder(
        self, note_ids: List[str], folder: str, *, create: bool = True
    ) -> Dict[str, Any]:
        """File several notes into one folder, in one action.

        The thing this replaces is opening Obsidian, finding each note, and
        dragging it — which is fine for one note and is why forty notes stay
        where they landed. Nothing about the notes changes: a Project stays a
        Project and keeps its deadline, its steps and its calendar blocks.
        Only the folder under its own bucket changes, so `20-Projects/Federal
        Taxation` and `30-Resources/Federal Taxation` both end up holding the
        right half of the same subject.

        The folder name is matched the same way a session's is — "fed tax"
        finds `Federal Taxation` — because the two features would otherwise
        disagree about what a folder is called, and the whole point of
        matching is that there is one folder per subject.

        **One bad id is reported and the rest still move.** The same rule the
        bulk capture commit follows: losing the other thirty-nine because the
        seventh id was stale is what makes a batch operation untrustworthy.
        """
        ids = [str(i).strip() for i in (note_ids or []) if str(i).strip()]
        if not ids:
            raise ValueError("no notes were selected")

        resolved, how = sessionmod.resolve_folder(self.folders()["all"], folder)
        if not resolved:
            raise ValueError("a folder needs a name")
        if how == "new" and not create:
            raise ValueError(f"no folder matching {folder!r} — create it explicitly")

        moved: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for note_id in ids:
            try:
                path, note = self.vault.get(note_id)
                target = self.vault.relocate(path, self.vault.path_in(note, resolved))
            except Exception as exc:  # noqa: BLE001 — see the docstring
                failed.append({"note_id": note_id, "error": f"{type(exc).__name__}: {exc}"})
                continue
            moved.append({
                "note_id": note.id,
                "title": note.title,
                "bucket": note.bucket.value,
                "path": str(target),
            })
        if moved:
            self.vault.log_line(
                "move", f"{len(moved)} notes -> {resolved}  ({how})"
            )
        return {"folder": resolved, "matched": how, "moved": moved, "failed": failed}

    def retire_note(self, note_id: str, reason: str = "") -> Dict[str, Any]:
        """Get a note out of the way. There is no delete, by design.

        The sanctioned answer to "I do not want this note any more", and the
        reason there is no other one: every button that destroys a note is a
        button that will eventually destroy the wrong note, and the cost is
        not symmetrical. This moves it to `40-Archive/_retired/`, which the
        system searches only on request and Obsidian still finds.
        """
        path, note = self.vault.get(note_id)
        target = self.vault.retire(path, reason or f"retired: {note.title}")
        return {"note_id": note.id, "title": note.title, "path": str(target)}

    def undefined_terms(self) -> List[Dict[str, Any]]:
        """Terms captured without a definition, oldest first.

        The backlog of "I did not know this word" is the most useful list in
        the vault and the easiest one to never look at again.
        """
        out = [
            _note_dict(n)
            for n in self.notes()
            if self.UNDEFINED_TAG in (n.tags or [])
        ]
        out.sort(key=lambda n: n.get("created") or "")
        return out

    def _apply_bucket(
        self,
        note: Note,
        target: Bucket,
        snapshot: List[Note],
        due: Optional[str] = None,
        shape: bool = True,
    ) -> Dict[str, Any]:
        """Everything that happens to a note *because* of which bucket it is
        in: parse and plan a Project, give an Area a habit and a block, put a
        Resource on the review cycle.

        Three entry points now reach a bucket — the capture buttons (§2), the
        Drop folder, and confirming an Inbox suggestion — and a note filed by
        any of them must come out identical. That is only true if there is one
        copy of this, which is why it left `capture()`.
        """
        note.bucket = target
        info: Dict[str, Any] = {}
        if target == Bucket.PROJECT:
            result = parser.apply_to_note(note, self.cfg)
            info["parser"] = {
                "provider": result.provider,
                "degraded": result.degraded,
                "note": result.note,
            }
            if result.degraded:
                # The capture still lands — that is the point of the fallback,
                # and it is verified in `test_degradation_is_honest`. But a
                # week of thinner plans because Ollama has been closed since
                # Tuesday is worth one banner.
                self._note_degradation("planning this project", result.note)
            info["forecast"] = self._apply_outside_view(note, snapshot)
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
            note.habit = note.habit or HabitMeta()
            note.schedule = note.schedule or AreaSchedule(
                time=self.cfg.areas.default_time,
                duration_minutes=self.cfg.areas.default_duration_minutes,
            )
            note.body = _area_body(note, self.cfg)
        elif target == Bucket.RESOURCE:
            # The shape of a plain capture, which is the Atomic Note template
            # unless config says otherwise. Two guards on it, and both matter:
            #
            #   `shape=False` is passed by the paths that already gave the
            #   note a shape of their own — a term, a screenshot, a session
            #   summary — so this cannot overwrite a more specific template
            #   with the general one.
            #
            #   `wants_shape` refuses text that already has its own headings.
            #   A line typed into the capture box has no structure and is
            #   exactly what a template is for; a nine-thousand-word document
            #   dropped into Drop/ has an author's structure already, and
            #   folding it under our `## Definition` would bury it.
            if shape and rendermod.wants_shape(note.body):
                self._shape(
                    note,
                    self.cfg.capture.default_template,
                    {"Definition": note.body.strip()},
                )
            note.review = note.review or ReviewMeta(
                cycle_days=self.cfg.review.resource_cycle_days,
                next=dt.date.today()
                + dt.timedelta(days=self.cfg.review.resource_cycle_days),
            )
        # Last, because it appends to the body the branches above just built.
        info["links"] = self._link_on_write(note, snapshot)
        return info

    def _link_on_write(self, note: Note, snapshot: List[Note]) -> Dict[str, Any]:
        """Roadmap Tier 1.1 — connect the note while it is being written.

        `connect.py` used to be the only way a note got a `## Related`
        section, which meant every note was born unlinked and stayed that way
        until someone pressed a button. The free tiers need one thing:
        everything else's titles. `snapshot` is already that, read once by the
        caller — so this adds no file read, no model call and no network.

        Passing `index=None` and `allow_model=False` is the whole safety
        argument: `connect_note` skips its embedding and model tiers
        structurally rather than by a flag someone can flip, so a capture can
        never block on Ollama or on an index that does not contain this note
        yet. The periodic pass (`connect_all`) still visits the note — this
        does not write the fingerprint that would gate it out — and adds what
        the index finds later.

        Failure here is never allowed to lose the capture: a note that is
        filed but unlinked is a note; an exception on the write path is a lost
        thought.
        """
        blank = {"linked": 0, "titles": []}
        if not self.cfg.connect.link_on_write:
            return blank
        if note.bucket not in connectmod.CONNECTABLE:
            return blank
        others = [n for n in connectmod.connectable(snapshot) if n.id != note.id]
        if not others:
            return blank
        try:
            result = connectmod.connect_note(
                note,
                None,                      # no index: the free tiers only
                self.cfg,
                others=others,
                max_links=self.cfg.connect.max_links_on_write,
                write=True,
                allow_model=False,
            )
        except Exception as exc:  # noqa: BLE001 - see the docstring
            self.vault.log_line("connect", f"link-on-write failed  {note.id}  {exc!r}")
            return dict(blank, error=type(exc).__name__)
        return {"linked": len(result.links), "titles": [l.title for l in result.links]}

    # -- capture in bulk (sb/segment.py, sb/tasksplit.py) --------------------

    def capture_plan(
        self,
        text: str,
        bucket: str = "project",
        *,
        due: Optional[str] = None,
        mode: str = "auto",
    ) -> Dict[str, Any]:
        """What one paste would become — proposed, not written.

        The three capture buttons make exactly one note each, which is the
        right shape for one thought and the wrong shape for the two ways real
        material arrives: a page of lecture notes covering nine topics, and a
        sentence holding three separate jobs. Both used to become one
        unlinkable, unquizzable note.

        This is the half that decides; `capture_commit` is the half that
        writes. They are separate on purpose — a splitter that files straight
        to the vault is a splitter whose mistakes you clean up afterwards,
        and a boundary is far cheaper to move on screen than a note is to
        merge back.

        `mode`:
          * `tasks` — several Projects out of one prompt (sb/tasksplit.py)
          * `notes` — several atomic notes out of long text (sb/segment.py)
          * `one`   — no split; what the buttons already did
          * `auto`  — `tasks` for a Project or an Area, `notes` otherwise
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("empty capture")
        target = Bucket(bucket)
        shape = mode if mode in ("tasks", "notes", "one") else (
            "tasks" if target in (Bucket.PROJECT, Bucket.AREA) else "notes"
        )
        picked = _as_date(due)
        # One probe for the whole plan. Both splitters degrade to their rule
        # tiers without a model, so this decides how loud the answer is, not
        # whether there is one.
        outage = self._model_outage("parse" if shape == "tasks" else "generate")

        if shape == "one":
            return {
                "mode": "one",
                "bucket": target.value,
                "strategy": "whole",
                "provider": "",
                "degraded": False,
                "note": "",
                "words": len(text.split()),
                "lossless": True,
                "items": [self._plan_item(text, target, "", picked, 1, "whole")],
            }

        if shape == "tasks":
            split = tasksplit.split_tasks(text, self.cfg, allow_model=not outage)
            items = []
            for task in split.tasks:
                items.append(
                    self._plan_item(
                        task.text,
                        target,
                        task.title,
                        picked,
                        task.ordinal,
                        task.boundary,
                        inherited=task.inherited_date,
                    )
                )
            return {
                "mode": "tasks",
                "bucket": target.value,
                "strategy": split.strategy,
                "provider": split.provider,
                "degraded": split.degraded or bool(outage),
                "note": split.note or (outage and f"No model — rules only. {outage}") or "",
                "preamble": split.preamble,
                "words": len(text.split()),
                "lossless": True,
                "items": items,
            }

        result = segment.segment(text, self.cfg, allow_model=not outage)
        missing = segment.check_lossless(text, result.segments)
        items = []
        for seg in result.segments:
            body = seg.body
            if seg.heading_path:
                # The breadcrumb is why a section under a title that has no
                # prose of its own is not a lost line: the title is carried
                # onto every child. `check_lossless` counts it for the same
                # reason.
                body = f"*From: {' › '.join(seg.heading_path)}*\n\n{body}"
            items.append(
                self._plan_item(
                    body,
                    target,
                    seg.title,
                    picked,
                    seg.ordinal,
                    seg.boundary,
                    heading_path=seg.heading_path,
                )
            )
        return {
            "mode": "notes",
            "bucket": target.value,
            "strategy": result.strategy,
            "provider": result.provider,
            "degraded": result.degraded or bool(outage),
            "note": result.note or (outage and f"No model — rules only. {outage}") or "",
            "words": result.words,
            # The one claim worth making loudly, and the one worth checking in
            # the response rather than only in a test: every line you typed is
            # in exactly one of these.
            "lossless": not missing,
            "lost_lines": missing[:10],
            "items": items,
        }

    def _plan_item(
        self,
        body: str,
        target: Bucket,
        title: str,
        picked: Optional[dt.date],
        ordinal: int,
        boundary: str,
        *,
        inherited: str = "",
        heading_path: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """One proposed note, with the deadline it would get.

        The date is resolved here rather than at write time so the preview
        shows the same date the note will actually carry — a preview that
        says "Friday" and files "next Friday" is worse than no preview.
        """
        guess = extract.parse_deadline_guess(body)
        if not guess and inherited:
            guess = extract.parse_deadline_guess(inherited)
        due = picked or (guess.date if guess else None)
        return {
            "title": title or extract.derive_title(body),
            "body": body,
            "bucket": target.value,
            "ordinal": ordinal,
            "boundary": boundary,
            "words": len(body.split()),
            "heading_path": list(heading_path or []),
            "due": due.isoformat() if due else "",
            "due_phrase": "" if picked else (guess.phrase if guess else ""),
            "due_source": "picked" if picked else (guess.kind if guess else ""),
            "due_confirmed": bool(picked) or bool(guess and guess.confirmed),
            "inherited_date": inherited,
        }

    def capture_commit(
        self,
        items: List[Dict[str, Any]],
        *,
        bucket: str = "",
        due: Optional[str] = None,
    ) -> Dict[str, Any]:
        """File a reviewed plan. Each item becomes a real note.

        One vault read for the whole batch, with the snapshot updated after
        every write — the same pattern `intake()` uses, and for the same two
        reasons: the planner schedules the second Project around the first
        rather than double-booking it, and `_link_on_write` links each note to
        the siblings already filed, so a chapter split into nine notes comes
        out connected instead of as nine orphans.

        A failure on one item is recorded and the rest still land. Losing four
        captures because the third had a bad date is exactly the behaviour
        that makes a capture box untrustworthy.
        """
        rows = [r for r in (items or []) if str(r.get("body") or "").strip()]
        if not rows:
            raise ValueError("nothing to file")

        snapshot = self._snapshot()
        created: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []
        for row in rows:
            title = str(row.get("title") or "").strip()
            try:
                target = Bucket(str(row.get("bucket") or bucket or "inbox"))
                body = str(row.get("body") or "").strip()
                note = Note.capture(
                    body, target, title=title or extract.derive_title(body)
                )
                info = self._apply_bucket(
                    note, target, snapshot, due=(row.get("due") or due or None)
                )
                path = self.vault.write(note)
                self.vault.log_line(
                    "capture", f"{note.bucket.value}  {note.id}  {note.title}"
                )
                snapshot = self._replacing(snapshot, note)
                created.append(
                    {
                        **_note_dict(note),
                        "path": str(path),
                        "linked": (info.get("links") or {}).get("linked", 0),
                        "scheduled": (info.get("plan") or {}).get("scheduled", 0),
                    }
                )
            except Exception as exc:  # noqa: BLE001 - see the docstring
                self.vault.log_line("capture", f"batch item failed  {title}  {exc!r}")
                failed.append(
                    {"title": title, "error": f"{type(exc).__name__}: {exc}"}
                )
        if created:
            self._sync_calendar_quiet(snapshot)
        return {
            "created": created,
            "failed": failed,
            "count": len(created),
            "note": (
                f"Filed {len(created)}."
                + (f" {len(failed)} failed and were not written." if failed else "")
            ),
        }

    # -- the Drop folder (sb/intake.py) --------------------------------------

    def accept_uploads(
        self, files: List[Tuple[str, bytes]], *, run: bool = True
    ) -> Dict[str, Any]:
        """Files handed over by the browser, written into Drop/ and filed.

        The Drop folder was always the interface and stays the interface —
        this only removes the requirement that lj be standing in front of
        *this* machine's Explorer window to use it. A file that arrives here
        lands in the same folder, gets read by the same reader and classified
        by the same rules; nothing downstream can tell the difference, which
        is the point.

        `settle_seconds=0` on the run afterwards is safe here and nowhere
        else: the settle wait exists because a file Word is still writing is
        not a file yet, and these bytes arrived whole in one request. Waiting
        five seconds to file something lj just dropped into the page would
        read as the feature not working.

        One bad file is reported and the rest still land — the same rule the
        bulk capture commit follows, for the same reason.
        """
        drop = self.ensure_drop_folder()
        saved: List[Dict[str, Any]] = []
        rejected: List[Dict[str, Any]] = []
        if len(files) > intakemod.MAX_UPLOAD_FILES:
            raise ValueError(
                f"{len(files)} files at once — the limit is "
                f"{intakemod.MAX_UPLOAD_FILES}. Drop them in a few batches."
            )
        for name, data in files:
            try:
                path = intakemod.accept_upload(drop, name, data)
            except ValueError as exc:
                rejected.append({"file": str(name), "error": str(exc)})
                continue
            except Exception as exc:  # noqa: BLE001 — a bad file is a report
                rejected.append({"file": str(name), "error": f"{type(exc).__name__}: {exc}"})
                continue
            saved.append({"file": path.name, "bytes": len(data)})
            self.vault.log_line("intake", f"uploaded  {path.name}  {len(data)} bytes")
        out: Dict[str, Any] = {
            "saved": saved,
            "rejected": rejected,
            "folder": str(drop),
        }
        if run and saved:
            out["intake"] = self.intake(settle_seconds=0.0)
        return out

    def intake(
        self,
        dry_run: bool = False,
        limit: Optional[int] = None,
        settle_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        """Read everything in Drop/, classify it, and file what it is sure of.

        One vault read for the whole run rather than one per file: the planner
        needs to know what time is already committed, and re-walking the vault
        for every dropped note would turn a folder of twenty into twenty
        whole-vault scans. `_replacing` keeps the snapshot current as notes are
        added, so the second file of a run schedules around the first.
        """
        if not self._intake_lock.acquire(blocking=False):
            return {
                "scanned": 0, "waiting": 0, "filed": 0, "asking": 0,
                "unreadable": 0, "model_calls": 0, "dry_run": dry_run,
                "busy": True, "folder": str(self.cfg.drop_dir), "results": [],
            }
        try:
            return self._intake(dry_run, limit, settle_seconds)
        finally:
            self._intake_lock.release()

    def _intake(
        self,
        dry_run: bool,
        limit: Optional[int],
        settle_seconds: Optional[float] = None,
    ) -> Dict[str, Any]:
        drop = self.cfg.drop_dir
        self.ensure_drop_folder()
        settle = (
            self.cfg.intake.settle_seconds if settle_seconds is None else settle_seconds
        )
        found = intakemod.candidates(drop, settle)
        cap = limit if limit is not None else self.cfg.intake.max_per_run
        pending = found[: max(0, int(cap))]

        snapshot = self._snapshot()
        results: List[Dict[str, Any]] = []
        filed = held = failed = model_calls = 0

        for path in pending:
            dropped = intakemod.read_file(path)
            if not dropped.ok:
                failed += 1
                reason = dropped.error or "the file had no text in it"
                if not dry_run:
                    intakemod.file_away(path, drop, intakemod.PROBLEM_DIR)
                self.vault.log_line("intake", f"unreadable  {path.name}  {reason}")
                results.append(
                    {"file": path.name, "status": "unreadable", "error": reason}
                )
                continue

            verdict = intakemod.classify(
                dropped.text,
                dropped.title,
                self.cfg,
                use_model=self.cfg.intake.use_model,
            )
            model_calls += verdict.model_calls
            auto = verdict.confidence >= self.cfg.intake.auto_floor

            if dry_run:
                results.append(
                    {
                        **verdict.as_dict(),
                        "file": path.name,
                        "title": dropped.title,
                        "bucket": verdict.bucket if auto else "inbox",
                        "status": "would file" if auto else "would ask",
                    }
                )
                continue

            note = Note.capture(
                dropped.text,
                Bucket(verdict.bucket) if auto else Bucket.INBOX,
                title=dropped.title,
            )
            info: Dict[str, Any] = {}
            if auto:
                info = self._apply_bucket(note, Bucket(verdict.bucket), snapshot)
            intakemod.stamp_note(note, dropped, verdict, filed=auto)

            written = self.vault.write(note)
            snapshot = self._replacing(snapshot, note)
            intakemod.file_away(path, drop, intakemod.FILED_DIR)
            self.vault.log_line(
                "intake",
                f"{path.name}  ->  {note.bucket.value}  "
                f"{verdict.confidence:.2f} {verdict.decided_by}  {note.id}",
            )
            filed, held = (filed + 1, held) if auto else (filed, held + 1)
            results.append(
                {
                    **verdict.as_dict(),
                    "file": path.name,
                    "note_id": note.id,
                    "title": note.title,
                    # Where it actually went, which is `inbox` for anything
                    # held back — `suggested` still says what was proposed.
                    "bucket": note.bucket.value,
                    "status": "filed" if auto else "asking",
                    "path": str(written),
                    "category": taxonomy.categorize(note, self.cfg),
                    "degraded": bool(info.get("parser", {}).get("degraded")),
                }
            )

        if filed and not dry_run:
            self._sync_calendar_quiet(snapshot)
        return {
            "scanned": len(pending),
            "waiting": max(0, len(found) - len(pending)),
            "filed": filed,
            "asking": held,
            "unreadable": failed,
            "model_calls": model_calls,
            "dry_run": dry_run,
            "folder": str(drop),
            "results": results,
        }

    def ensure_drop_folder(self) -> Path:
        """Create Drop/ (and its two subfolders) with the note explaining it.

        Called on startup as well as before every run: a folder lj is meant to
        drop files into has to exist before they think to look for it, and a
        folder they deleted by accident should come back rather than turning
        the feature off silently.
        """
        drop = self.cfg.drop_dir
        for sub in ("", intakemod.FILED_DIR, intakemod.PROBLEM_DIR):
            (drop / sub if sub else drop).mkdir(parents=True, exist_ok=True)
        readme = drop / "README.md"
        if not readme.exists():
            readme.write_text(intakemod.README, encoding="utf-8")
        return drop

    def classify_note(self, note_id: str, bucket: str) -> Dict[str, Any]:
        """Answer the Inbox's question: this one is an Area/Project/Resource.

        The note gets the full bucket treatment now rather than just a move,
        which is the difference between this and `move()`: a dropped note that
        waited in the Inbox and is then confirmed as a Project must end up
        parsed, planned and on the calendar exactly as if it had been filed
        automatically. `move()` deliberately does none of that — it is for a
        note that already has its metadata and is changing shelves.
        """
        note = self.note(note_id)
        target = Bucket(bucket)
        if target not in (Bucket.AREA, Bucket.PROJECT, Bucket.RESOURCE):
            raise ValueError(f"cannot file a note as {bucket!r}")
        snapshot = self._snapshot()
        info = self._apply_bucket(note, target, snapshot)
        if note.intake:
            # This click is a labelled example: the rules scored the note at
            # `confidence` and suggested a bucket, and lj has just said what
            # the answer actually was. Sixty of these turn AUTO_FLOOR from a
            # preference into a parameter (roadmap 1.2, and Manning et al.
            # ch.8). Recorded before the fields below are overwritten.
            try:
                # A second answer about the same note — story G2's undo, then
                # a re-file — is scored against what the *rules* originally
                # guessed, not against the note's own rewritten metadata. See
                # LabelStore.original_intake for why the note cannot be
                # trusted for this by the time it is asked.
                first = self.labels.original_intake(note.id)
                self.labels.record_intake(
                    note.id,
                    suggested=str((first or {}).get("suggested") or note.intake.suggested),
                    chosen=target.value,
                    confidence=float(
                        (first or {}).get("confidence", note.intake.confidence) or 0.0
                    ),
                    decided_by=str(
                        (first or {}).get("decided_by") or note.intake.decided_by
                    ),
                    corrected=bool(first),
                )
            except OSError as exc:
                self.vault.log_line("intake", f"label not recorded: {exc}")
            note.intake.filed_automatically = False
            note.intake.decided_by = "manual"
            note.intake.suggested = target.value
            note.intake.confidence = 1.0
        note.log("classified", f"-> {target.value} (confirmed)")
        path = self.vault.save(note)
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {"note": _note_dict(note), "path": str(path), **info}

    def _inbox(
        self, all_notes: List[Note], limit: Optional[int] = 25
    ) -> List[Dict[str, Any]]:
        """What the Drop folder could not call, waiting for one click.

        Everything in the Inbox appears here, not only dropped files — an
        Inbox with something in it is a question either way, and the blueprint
        says it should stay near-empty.

        `limit` is a panel concern, not a data one. The dashboard shows the
        most recent 25 because it is one section among twelve; the triage
        screen (story G2) passes `None`, because a list that stops at 25 is a
        list you cannot reach the end of, and reaching the end is the whole
        point of that screen.
        """
        items = [n for n in all_notes if n.bucket == Bucket.INBOX]
        items.sort(key=lambda n: n.created, reverse=True)
        out: List[Dict[str, Any]] = []
        for note in (items[:limit] if limit else items):
            meta = note.intake
            out.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "created": note.created,
                    "category": taxonomy.categorize(note, self.cfg),
                    "excerpt": _excerpt(note.body),
                    "file": meta.file if meta else "",
                    "suggested": meta.suggested if meta else "",
                    "confidence": round(meta.confidence, 2) if meta else 0.0,
                    "reason": meta.reason if meta else "",
                    "signals": list(meta.signals) if meta else [],
                    "decided_by": meta.decided_by if meta else "",
                }
            )
        return out

    def inbox(self, limit: Optional[int] = None) -> Dict[str, Any]:
        """The whole Inbox, in the order it will be triaged (story G2).

        One vault walk, the same rows the dashboard panel renders, and no cap.
        It is a separate endpoint from `/api/dashboard` because the triage
        screen wants only this — asking for the dashboard would build twelve
        other panels the screen does not draw, on every reload after a filing.
        """
        rows = self._inbox(self.notes(), limit=limit)
        return {"count": len(rows), "items": rows}

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
            "inbox": self._inbox(all_notes),
            "pending_dates": self._pending_dates(active),
            "reviews": self._reviews_due(all_notes),
            "archive": self._archived(all_notes),
            "upcoming": workflow.upcoming(all_notes),
            "categories": taxonomy.as_dicts(self.cfg),
            "study": self._study_summary(all_notes),
            "vault": str(self.cfg.vault),
        }

    # -- the weekly review (roadmap Tier 3) ---------------------------------

    def weekly_review(self, days: int = 7) -> Dict[str, Any]:
        """One page that closes the week and opens the next.

        The system already had three separate prompts — the habit check-in,
        the Resource review, the "ready to graduate?" question — each firing
        on its own timer, each answerable in isolation, and none of them ever
        asking *how the week went*. David Allen's argument for the weekly
        review is that it is the load-bearing habit of the entire method: not
        because reviewing is valuable in itself, but because without one place
        where everything outstanding is looked at together, trust in the system
        decays and you go back to keeping it in your head.

        Barry Zimmerman's self-regulated-learning cycle gives the shape:
        forethought → performance → **self-reflection**, and the reflection
        phase is the one that feeds the next forethought. So the page is three
        columns and they are in that order: what closed, what slipped, what is
        next. Not three lists of things to click.

        Read-only. A review that changes things while you are reading it is a
        review you cannot trust, and every item here already has its own
        endpoint to act through.
        """
        since = dt.date.today() - dt.timedelta(days=max(1, int(days)))
        notes = self.notes()
        decks = self.decks.all()
        reviews = list(self.decks.reviews(since=since))

        return {
            "since": since.isoformat(),
            "days": days,
            "closed": self._week_closed(notes, decks, reviews, since),
            "slipped": self._week_slipped(notes, since),
            "next": self._week_next(notes, decks),
            # The reflection half: how good the estimates were, and how well
            # lj knew what they knew. Both are week-scale questions that no
            # single prompt was ever going to ask.
            "estimates": forecasting.summary(notes),
            "calibration": calibration.curve(self.decks.reviews()).as_dict(),
            "atomicity": lint.check(notes).as_dict(),
            # Whether the vault is being used or only fed. A week is the
            # right scale for it: the ratio does not move in a day, and the
            # review is the one moment set aside for looking at the whole
            # thing rather than at the next thing. See sb/collected.py.
            "collected": self._week_collected(notes, decks),
            # The week is also where a health check that stopped running gets
            # noticed. `doctor --write` leaves a dated report; this says when
            # the last one landed, or that none ever did.
            "doctor": self.latest_doctor_report(),
            # And what broke while nobody was looking. The banner shows these
            # live; the review is where they get read on purpose.
            "problems": self.incidents.open(),
        }

    def _week_collected(self, notes: List[Note], decks: List) -> Dict[str, Any]:
        """The collected-vs-used figure, from the walk the review already did.

        No extra read: `weekly_review` has the notes and the decks in hand,
        and this is the one place the number is worth a whole section rather
        than a panel.
        """
        by_note = {d.note_id: d for d in decks}
        rows = [collected.assess(n, by_note.get(n.id)) for n in notes]
        stats = collected.summarise(rows)
        worst = sorted(
            (r for r in rows if r["eligible"] and not r["used"]),
            key=lambda r: -(r["age_days"] or 0),
        )[:5]
        return {**stats, "sentence": collected.sentence(stats), "oldest": worst}

    def _week_closed(
        self, notes: List[Note], decks: List, reviews: List[Dict[str, Any]], since: dt.date
    ) -> Dict[str, Any]:
        steps = []
        for note in notes:
            if not note.project:
                continue
            for step in note.project.steps:
                if step.done and step.done_at and step.done_at.date() >= since:
                    steps.append({
                        "note_id": note.id,
                        "title": note.title,
                        "step": step.text,
                        "estimated": step.minutes,
                        "actual": step.actual_minutes,
                        "on": step.done_at.date().isoformat(),
                    })
        finished = [
            {"note_id": n.id, "title": n.title}
            for n in notes
            if n.project
            and n.project.status == ProjectStatus.DONE
            and any(h.event == "completed" and h.at.date() >= since for h in n.history)
        ]
        habits = []
        for note in notes:
            if note.bucket != Bucket.AREA or not note.habit:
                continue
            hits = [e for e in note.habit.log if e.on >= since]
            if hits:
                habits.append({
                    "note_id": note.id, "title": note.title,
                    "times": len(hits), "target": note.habit.target_count,
                })
        return {
            "steps": steps,
            "projects": finished,
            "habits": habits,
            "reviews": len(reviews),
            "cards_learned": sum(1 for r in reviews if (r.get("before") or {}).get("reps") == 0),
            "minutes_studied": round(sum(float(r.get("seconds") or 0) for r in reviews) / 60.0),
        }

    def _week_slipped(self, notes: List[Note], since: dt.date) -> Dict[str, Any]:
        today = dt.date.today()
        overdue_steps = []
        overdue_projects = []
        for note in notes:
            project = note.project
            if not project or project.status == ProjectStatus.DONE:
                continue
            if project.deadline and project.deadline < today:
                overdue_projects.append({
                    "note_id": note.id, "title": note.title,
                    "deadline": project.deadline.isoformat(),
                    "days": (today - project.deadline).days,
                    "progress": round(project.progress, 2),
                })
            for step in project.steps:
                if step.done or not step.scheduled:
                    continue
                if step.scheduled.date() < today:
                    overdue_steps.append({
                        "note_id": note.id, "title": note.title, "step": step.text,
                        "was": step.scheduled.date().isoformat(),
                        "days": (today - step.scheduled.date()).days,
                    })
        overdue_steps.sort(key=lambda s: -s["days"])

        habits = []
        for note in notes:
            if note.bucket != Bucket.AREA or not note.habit:
                continue
            report = habitsmod.report(note.habit, title=note.title)
            miss = report["misses"]
            # Never miss twice: one miss is reported, two is the alert. A
            # streak counter would have called the first one a failure.
            if miss["consecutive_misses"] or report["intention_missing"]:
                habits.append({
                    "note_id": note.id, "title": note.title,
                    "consecutive_misses": miss["consecutive_misses"],
                    "alert": miss["alert"],
                    "message": miss["message"],
                    "missing": report["intention_missing"],
                    "suggestions": report["suggestions"][:2],
                })
        habits.sort(key=lambda h: (not h["alert"], -h["consecutive_misses"]))
        return {
            "steps": overdue_steps[:12],
            "projects": overdue_projects,
            "habits": habits,
            "unconfirmed_dates": self._pending_dates(
                [n for n in notes if n.bucket == Bucket.PROJECT]
            ),
        }

    def _week_next(self, notes: List[Note], decks: List) -> Dict[str, Any]:
        active = [
            n for n in notes
            if n.bucket == Bucket.PROJECT and n.project
            and n.project.status != ProjectStatus.DONE
        ]
        due_reviews = self._reviews_due(notes)
        checkins = []
        today = dt.date.today()
        for note in notes:
            if note.bucket != Bucket.AREA or not note.habit:
                continue
            last = note.habit.last_checkin
            if last is None or (today - last).days >= 7:
                checkins.append({"note_id": note.id, "title": note.title,
                                 "last": last.isoformat() if last else None})
        return {
            "actions": [a.as_dict for a in workflow.next_actions(active, limit=8)],
            "upcoming": workflow.upcoming(notes, days=7),
            "graduation": self.graduation_candidates(),
            "resource_reviews": due_reviews,
            "habit_checkins": checkins,
        }

    # -- the daily digest (F3': "what's going on today") --------------------

    def today_digest(self, on: Optional[dt.date] = None) -> Dict[str, Any]:
        """Everything the system knows about today, in one payload.

        Sprint 2 story F3' replaced "phone capture" with the opposite
        direction: lj texts (or is texted) "what's going on today" and gets
        one answer back. The transport is undecided — see the spike's
        decision doc — so this only builds the part that is useful
        regardless of it. Two renderers sit over the same payload in
        sb/digest.py: `render_text` for an SMS body, `render_long` for email
        or a dashboard panel.
        """
        all_notes = self.notes()
        active = [
            n for n in all_notes
            if n.bucket == Bucket.PROJECT and n.project
            and n.project.status != ProjectStatus.DONE
        ]
        inbox_count = sum(1 for n in all_notes if n.bucket == Bucket.INBOX)
        pending = len(self._pending_dates(active))
        return digest.build(
            all_notes,
            active,
            self.decks.all(),
            self.cfg,
            inbox_count=inbox_count,
            pending_dates=pending,
            on=on,
        ).as_dict()

    def retention_dial(self) -> Dict[str, Any]:
        """What the current retention target costs, and what it would cost
        elsewhere. Roadmap Tier 3 — see sb/retention.py."""
        return retention.curve(
            self.decks.all(),
            current=self.cfg.study.desired_retention,
            seconds=retention.seconds_per_review(self.decks.reviews()),
        )

    def atomicity(self) -> Dict[str, Any]:
        """Lint the vault for notes that hold more than one idea.

        Roadmap Tier 3. Phase 6's free linking tier works because notes are
        atomic and precisely named; nothing has ever checked that it is still
        true. See sb/lint.py.
        """
        return lint.check(self.notes()).as_dict()


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
            # The causal half of the habit model — the implementation
            # intention, the anchor, the friction, consecutive misses and
            # context stability. See sb/habits.py for why each is here.
            "habit": habitsmod.report(note.habit, title=note.title),
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
        if result.degraded:
            self._note_degradation("re-parsing this project", result.note)
        self.vault.save(note)
        self._sync_calendar_quiet(self._replacing(snapshot, note))
        return {
            "note": _note_dict(note),
            "parser": {"provider": result.provider, "degraded": result.degraded, "note": result.note},
            "plan": {"scheduled": report.scheduled, "message": report.message},
        }

    def start_step(self, note_id: str, step_id: str) -> Dict[str, Any]:
        """Start the clock on one step.

        The only way to score an estimate is to know when the work began, and
        the only way to know that without lying is to ask. One tap, and the
        alternative — inferring elapsed time from when the step was ticked —
        would record "three days" for a step done in twenty minutes on
        Thursday afternoon.
        """
        note = self.note(note_id)
        if not note.project:
            raise ValueError("note has no project metadata")
        step = next((s for s in note.project.steps if s.id == step_id), None)
        if step is None:
            raise ValueError(f"no step {step_id!r}")
        step.started_at = _now()
        step.done = False
        step.done_at = None
        note.log("step", f"{step_id} started")
        note.body = _project_body(note)
        self.vault.save(note)
        return _note_dict(note)

    def toggle_step(
        self, note_id: str, step_id: str, minutes: Optional[int] = None
    ) -> Dict[str, Any]:
        """Tick or untick a step, recording how long it really took.

        `minutes` given wins; otherwise the elapsed time since `start_step` is
        used, if the clock was ever started. Nothing is invented: a step
        finished without either stays untimed and simply never reaches
        `sb/forecasting.py`, which is correct — an estimate scored against a
        made-up duration is worse than an estimate never scored.
        """
        note = self.note(note_id)
        if not note.project:
            raise ValueError("note has no project metadata")
        for step in note.project.steps:
            if step.id == step_id:
                step.done = not step.done
                step.done_at = _now() if step.done else None
                if step.done:
                    step.actual_minutes = self._elapsed_minutes(step, minutes)
                else:
                    # Reopening throws the measurement away rather than keeping
                    # a duration for work that is evidently not finished.
                    step.actual_minutes = None
                    step.started_at = None
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
        return {**_note_dict(note), "accuracy": forecasting.project_accuracy(note)}

    def label_link(self, note_id: str, target: str, kept: bool, score: float = 0.0) -> Dict[str, Any]:
        """Record that a suggested link was right or wrong.

        The other half of roadmap 1.2. A link lj keeps and a link lj deletes
        are both labelled examples, and they are the only source of ground
        truth `connect.AUTO_FLOOR` will ever have.
        """
        self.labels.record_link(note_id, target, score, bool(kept))
        return self.labels.counts()

    def thresholds(self) -> Dict[str, Any]:
        """Are the two auto-accept floors set right, on lj's own examples?

        Roadmap 1.2. Manning, Raghavan & Schütze ch.8: a retrieval threshold
        without a labelled evaluation set is a preference, not a parameter.
        See sb/threshold.py for why precision is the constraint and not F1.
        """
        return threshold.report({
            "intake": (self.labels.intake_pairs(), self.cfg.intake.auto_floor),
            "connect": (self.labels.link_pairs(), connectmod.AUTO_FLOOR),
        })

    def _apply_outside_view(self, note: Note, snapshot: List[Note]) -> Dict[str, Any]:
        """Scale a fresh estimate by how long lj's own steps actually take.

        Buehler, Griffin & Ross: the inside view underestimates, reliably, and
        knowing that does not fix it — only substituting the observed
        distribution does. `snapshot` is already every note, so the reference
        class costs nothing to build here.

        Scaling the *steps* rather than only the total is deliberate: the
        planner books calendar time per step, so adjusting the headline number
        alone would leave the blocks the wrong size. And because the ratio is
        measured against whatever estimate was in force, the correction
        converges rather than compounding — once the scaled estimates are
        right, the multiplier walks back to 1.
        """
        project = note.project
        if project is None or not self.cfg.planner.apply_personal_multiplier:
            return {"applied": False, "reason": "off"}
        table = forecasting.classes(forecasting.observations(snapshot))
        before = project.estimate_minutes
        forecast = forecasting.adjust(before, project.level, table)
        if forecast.multiplier == 1.0:
            return {"applied": False, **forecast.as_dict()}
        for step in project.steps:
            step.minutes = max(5, int(round(step.minutes * forecast.multiplier)))
        project.estimate_minutes = (
            sum(s.minutes for s in project.steps) if project.steps else forecast.minutes
        )
        note.log("forecast", f"x{forecast.multiplier} from {forecast.basis}")
        return {"applied": True, "before": before, **forecast.as_dict()}

    @staticmethod
    def _elapsed_minutes(step, given: Optional[int]) -> Optional[int]:
        """Minutes for one finished step: what was typed, else what elapsed.

        A step left running overnight is capped at a working day rather than
        recorded as 900 minutes — an obvious mis-log should not become the
        datapoint that skews every future estimate. `forecasting.py` also
        drops extreme ratios, so this is the second of two guards.
        """
        if given is not None:
            try:
                return max(1, min(24 * 60, int(given)))
            except (TypeError, ValueError):
                return None
        if not step.started_at:
            return None
        elapsed = (_now() - step.started_at).total_seconds() / 60.0
        if elapsed <= 0:
            return None
        return max(1, min(12 * 60, int(round(elapsed))))

    def estimates(self) -> Dict[str, Any]:
        """How well the estimates have held up, and the multiplier that follows.

        Roadmap 2.5. Buehler, Griffin & Ross on the planning fallacy;
        Kahneman & Tversky's outside view; Flyvbjerg's reference-class
        correction as the working method. See sb/forecasting.py.
        """
        return forecasting.summary(self.notes())

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

    HABIT_TEXT_FIELDS = ("cue", "behaviour", "place", "anchor", "easier", "harder")

    def set_habit(
        self,
        note_id: str,
        cadence: Optional[str] = None,
        target_count: Optional[int] = None,
        **fields: Any,
    ) -> Dict[str, Any]:
        """Set the cadence, the target, and — the part that actually matters —
        the implementation intention, the anchor and the friction.

        Cadence and target are optional now. Filling in only the cue must not
        force a caller to restate a schedule it is not changing, because the
        whole point of roadmap 2.4 is that the count is the least important
        field on this model.
        """
        note = self.note(note_id)
        note.habit = note.habit or HabitMeta()
        if cadence:
            note.habit.cadence = Cadence(cadence)
        if target_count is not None:
            note.habit.target_count = max(1, min(7, int(target_count)))
        for key in self.HABIT_TEXT_FIELDS:
            if fields.get(key) is not None:
                setattr(note.habit, key, str(fields[key]).strip()[:300])
        if note.bucket == Bucket.AREA and not note.schedule:
            note.schedule = AreaSchedule(
                time=self.cfg.areas.default_time,
                duration_minutes=self.cfg.areas.default_duration_minutes,
            )
        note.log("habit", f"{note.habit.cadence.value} x{note.habit.target_count}")
        note.body = _area_body(note, self.cfg)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return {**_note_dict(note), "habit": habitsmod.report(note.habit, title=note.title)}

    def log_habit(
        self,
        note_id: str,
        on: Optional[str] = None,
        at: str = "",
        place: str = "",
    ) -> Dict[str, Any]:
        """Record one occurrence, with the context that makes it measurable.

        Time and place are optional and default to *now* and the habit's
        planned place, because an occurrence logged with one tap is worth more
        than a form nobody fills in — but when they are there, they are what
        `habits.stability()` reads. Wood's finding is that same-time-same-place
        is what automates a behaviour; a bare date cannot express it.

        Idempotent per day per time: tapping twice for the same slot does not
        inflate the count.
        """
        note = self.note(note_id)
        if note.bucket != Bucket.AREA:
            raise ValueError("only Areas carry a habit")
        note.habit = note.habit or HabitMeta()
        when = _as_date(on) or dt.date.today()
        stamp = (at or "").strip() or _now().strftime("%H:%M")
        where = (place or "").strip() or (note.habit.place or "").strip()
        if not any(e.on == when and e.at == stamp for e in note.habit.log):
            note.habit.log.append(HabitEvent(on=when, at=stamp, place=where))
            note.habit.log.sort(key=lambda e: (e.on, e.at))
        note.log("habit-done", when.isoformat())
        note.body = _area_body(note, self.cfg)
        self.vault.save(note)
        return {
            **_note_dict(note),
            "habit": habitsmod.report(note.habit, title=note.title),
        }

    def habit_checkin(
        self, note_id: str, decision: str = "continue", target_count: Optional[int] = None
    ) -> Dict[str, Any]:
        """The weekly check-in, answering the question the evidence asks.

        Phase 3 asked "continue or change the count?". That is the weakest
        question available. This still accepts it — a count change is
        sometimes the right answer, especially after two misses — but the
        payload it returns leads with consecutive misses and the blank fields
        of the implementation intention, so the prompt asks the strongest
        question first.
        """
        note = self.note(note_id)
        if note.bucket != Bucket.AREA:
            raise ValueError("only Areas carry a habit")
        note.habit = note.habit or HabitMeta()
        decision = (decision or "continue").strip().lower()
        if decision not in ("continue", "change", "pause"):
            raise ValueError(f"unknown check-in decision {decision!r}")
        report = habitsmod.report(note.habit, title=note.title)
        if decision == "change" and target_count is not None:
            note.habit.target_count = max(1, min(7, int(target_count)))
        if decision == "pause" and note.schedule:
            note.schedule.enabled = False
        note.habit.last_checkin = dt.date.today()
        note.habit.checkins.append(
            {
                "on": dt.date.today().isoformat(),
                "decision": decision,
                "target_count": note.habit.target_count,
                "consecutive_misses": report["misses"]["consecutive_misses"],
            }
        )
        note.log("checkin", f"{decision} x{note.habit.target_count}")
        note.body = _area_body(note, self.cfg)
        self.vault.save(note)
        self._sync_calendar_quiet()
        return {
            **_note_dict(note),
            "habit": habitsmod.report(note.habit, title=note.title),
            "decision": decision,
        }

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
        """Push the vault out. Reads ticks back in first, if that is on.

        Order is load-bearing. The push deletes the Google task for any
        project the vault considers finished; if the read came second, a tick
        made on the phone would be deleted before it was ever seen. Read, then
        write, is the only ordering under which the tick survives.
        """
        sink = get_sink(self.cfg)
        notes = self._snapshot() if snapshot is None else snapshot
        pulled = self.pull_task_completions(notes)
        if pulled.get("applied"):
            notes = self._snapshot()   # completions changed what gets pushed
        result = sink.sync(notes, self.cfg, len(self.decks.all()))
        self.vault.log_line("calendar", f"{sink.name}: {result}")
        return {"sink": sink.name, "result": result, "pulled": pulled}

    def pull_task_completions(self, snapshot: Optional[List[Note]] = None) -> Dict[str, Any]:
        """Roadmap 1.3 — the one field Google is allowed to win.

        The vault wins on content and Google wins on completion, because
        ticking is the only thing lj can do to one of these tasks on a phone.
        See the comment in sb/calsync/gtasks.py for why that is a per-field
        merge rather than last-writer-wins.

        Never raises: a phone that cannot be reached is a sync that did not
        happen, not a write that failed. The same rule the whole calendar
        layer already follows.
        """
        blank = {"applied": 0, "checked": 0, "enabled": False}
        if self.cfg.resolved_task_sink() not in ("google", "both"):
            return blank
        if not self.cfg.calendar.read_back_completions:
            return dict(blank, reason="off")
        try:
            from .calsync import gtasks
            ticked = gtasks.read_back(self.cfg)
        except Exception as exc:  # noqa: BLE001 — see the docstring
            self.vault.log_line("calendar", f"read-back failed: {type(exc).__name__}: {exc}")
            # A push that fails is loud: nothing new appears on the phone. A
            # *read* that fails is silent in exactly the wrong way — ticking
            # keeps working, the vault simply stops hearing about it, and the
            # first evidence is a project lj finished a fortnight ago still
            # sitting open. So it files an incident like any other background
            # failure, and clears it the moment a read succeeds.
            try:
                self.incidents.record(
                    incidentsmod.CALENDAR,
                    "Ticks made on the phone are not reaching the vault "
                    f"({type(exc).__name__}).",
                    hint="Run `python run.py sync` to see the whole error.",
                    key="read-back",
                    detail=str(exc)[:400],
                )
            except Exception:  # an incident must never become the failure
                pass
            return dict(blank, enabled=True, error=type(exc).__name__)
        try:
            self.incidents.clear(incidentsmod.CALENDAR, key="read-back")
        except Exception:
            pass

        notes = self._snapshot() if snapshot is None else snapshot
        by_id = {n.id: n for n in notes}
        applied: List[str] = []
        for uid in ticked:
            note = by_id.get(gtasks.note_id_of(uid))
            if note is None or not note.project:
                continue
            if note.project.status == ProjectStatus.DONE:
                continue
            if self._apply_remote_completion(note):
                applied.append(note.id)
        return {
            "applied": len(applied),
            "checked": len(ticked),
            "enabled": True,
            "notes": applied,
        }

    def _apply_remote_completion(self, note: Note) -> bool:
        """A tick that arrived from the phone.

        A learning Project is *not* marked DONE by this. Blueprint §4 puts
        graduation behind a confirmed prompt driven by review history, and a
        tick on a due-date reminder is not that evidence — it means the work
        is finished, not that the material is known. So its steps close and
        its status stays ACTIVE, which is exactly what happens when the last
        step is ticked in the app.

        Returns whether anything actually changed, and that answer is
        load-bearing. A learning Project keeps status ACTIVE, so it is still
        `wanted` by the task sink and its Google task keeps existing —
        completed, now that the sink no longer patches `status` back (see
        gtasks.to_google). Every subsequent sync therefore reads the same tick
        back again. Returning True unconditionally made each of those a fresh
        vault write and a fresh "completed on Google Tasks" history line,
        forever, on a note nobody had touched. Nothing changed, so nothing is
        saved and nothing is counted as applied.
        """
        project = note.project
        if project is None:
            return False
        changed = False
        for step in project.steps:
            if not step.done:
                step.done = True
                step.done_at = _now()
                changed = True
        if project.learning:
            if not changed:
                return False
            project.status = ProjectStatus.ACTIVE
            note.log("step", "completed on Google Tasks")
        else:
            project.status = ProjectStatus.DONE
            note.log("completed", "ticked on Google Tasks")
        note.body = _project_body(note)
        self.vault.save(note)
        return True

    def _sync_calendar_quiet(self, snapshot: Optional[List[Note]] = None) -> None:
        """Best-effort resync after a mutation; never fails a write because a
        calendar was unreachable.

        Swallowing the exception is right -- losing a captured note because
        Google was down would be far worse -- but swallowing it *silently* is
        how a calendar stops syncing in March and is noticed in June. The log
        line stays for the detail; the incident is what makes it visible
        without opening a log.
        """
        try:
            self.sync_calendar(snapshot)
        except Exception as exc:
            self.vault.log_line("calendar", f"sync failed: {type(exc).__name__}: {exc}")
            try:
                self.incidents.record(
                    incidentsmod.CALENDAR,
                    f"Calendar sync failed ({type(exc).__name__}).",
                    hint="Run `python run.py sync` to see the whole error.",
                    key="sync",
                    detail=str(exc)[:400],
                )
            except Exception:  # an incident must never become the failure
                pass
        else:
            try:
                self.incidents.clear(incidentsmod.CALENDAR, key="sync")
            except Exception:
                pass

    def ics_path(self) -> Path:
        return self.cfg.ics_path

    # -- doctor reports -----------------------------------------------------

    #: How many dated `doctor` reports to keep. A weekly task and a two-month
    #: window: long enough to see "this started in July", short enough that a
    #: log folder lj never opens does not grow forever.
    DOCTOR_REPORTS_KEPT = 8

    def doctor_report_path(self, on: Optional[dt.date] = None) -> Path:
        on = on or dt.date.today()
        return self.cfg.system_dir / "logs" / f"doctor-{on.strftime('%Y%m%d')}.txt"

    def doctor_reports(self) -> List[Path]:
        """Dated reports on disk, newest last. The filename carries the date,
        so sorting the names sorts by date."""
        d = self.cfg.system_dir / "logs"
        if not d.is_dir():
            return []
        return sorted(d.glob("doctor-*.txt"))

    def write_doctor_report(
        self, text: str, *, on: Optional[dt.date] = None, keep: Optional[int] = None
    ) -> Path:
        """Save a `doctor` run under today's date and prune the old ones.

        One file per day, overwritten if `doctor --write` runs twice in a day:
        the point of the dated report is a weekly trail of what the system
        looked like, not an archive of every invocation.
        """
        path = self.doctor_report_path(on)
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        path.write_text(
            f"second-brain doctor  ·  {stamp}\n"
            f"vault: {self.cfg.vault}\n"
            + "-" * 68 + "\n"
            + (text or "").rstrip() + "\n",
            encoding="utf-8",
        )
        limit = self.DOCTOR_REPORTS_KEPT if keep is None else keep
        on_disk = self.doctor_reports()
        for stale in on_disk[: max(0, len(on_disk) - limit)]:
            try:
                stale.unlink()
            except OSError:
                pass
        return path

    def latest_doctor_report(self, *, stale_after_days: int = 8) -> Dict[str, Any]:
        """What the weekly review says about the health check itself.

        A check that is supposed to run weekly and has not run in a month is
        its own finding, so the absence is reported as loudly as the presence.
        """
        reports = self.doctor_reports()
        if not reports:
            return {
                "path": "", "date": "", "age_days": None, "stale": True,
                "kept": 0,
                "message": "doctor has never written a report — "
                           "run `python run.py doctor --write` "
                           "(install-doctor-task.bat schedules it weekly)",
            }
        newest = reports[-1]
        try:
            on = dt.datetime.strptime(newest.stem.split("-", 1)[1], "%Y%m%d").date()
        except (ValueError, IndexError):
            on = dt.date.fromtimestamp(newest.stat().st_mtime)
        age = (dt.date.today() - on).days
        stale = age >= stale_after_days
        return {
            "path": str(newest),
            "date": on.isoformat(),
            "age_days": age,
            "stale": stale,
            "kept": len(reports),
            "message": (
                f"doctor last ran {age} day(s) ago ({on.isoformat()}) — "
                "the weekly task has not fired"
                if stale
                else f"doctor ran {on.isoformat()} ({age} day(s) ago) — {newest}"
            ),
        }

    def _progress_report(self) -> Dict[str, Any]:
        """How close each self-measuring part is to having something to say.

        Four numbers in this system start as guesses and are supposed to
        become measurements: the FSRS weights, the two auto-accept floors, the
        estimate multiplier, and the calibration curve. Each needs data lj has
        not produced yet. Reporting the distance is the difference between "not
        working" and "not yet" — and it is the only honest way to ship a
        feature whose whole design is that it waits.

        Cheap by construction: one vault read, one pass over each log. It runs
        from `doctor`, never on a request path.
        """
        notes = self.notes()
        reviews = list(self.decks.reviews())
        predictions = calibration.judged(reviews)
        labels = self.labels.counts()
        timed = forecasting.observations(notes)
        index = self.index.status()

        areas = [n for n in notes if n.bucket == Bucket.AREA]
        planless = [
            n for n in areas
            if habitsmod.intention_missing(n.habit, fallback=n.title)
        ]
        untimed_steps = sum(
            1
            for n in notes
            if n.project
            for s in n.project.steps
            if s.done and not s.actual_minutes
        )
        plugin_status = self._obsidian_plugin_status()
        return {
            "fsrs": {
                "have": len(fitmod.samples(reviews)),
                "need": fitmod.MIN_REVIEWS,
                "fitted": bool(fitmod.load(self.cfg.deck_dir)),
                "using_fitted": (
                    self.cfg.study.use_fitted_weights
                    and not self.cfg.study.weights
                    and bool(fitmod.load(self.cfg.deck_dir))
                ),
            },
            "calibration": {"have": len(predictions), "need": calibration.MIN_FOR_SCORE},
            "thresholds": {
                "intake": labels["intake"],
                "connect": labels["link"],
                "need": threshold.MIN_LABELS,
            },
            "estimates": {
                "have": len(timed),
                "need": forecasting.MIN_CLASS_SAMPLES,
                "untimed": untimed_steps,
            },
            "habits": {"areas": len(areas), "without_plan": len(planless)},
            "atomicity": lint.check(notes).by_rule,
            "numpy": {
                "chunks": index.get("chunks", 0),
                "at": index.get("numpy_at"),
                "available": index.get("numpy_available", False),
                "accelerated": index.get("accelerated", False),
            },
            "plugin": plugin_status["installed"],
            "plugin_enabled": plugin_status["enabled"],
            #: False when the enabled/disabled answer could not be read at
            #: all. `doctor` says "cannot tell" rather than "not enabled".
            "plugin_enabled_known": plugin_status["known"],
        }

    def _obsidian_plugin_status(self) -> Dict[str, bool]:
        """Installed: the plugin's main.js is on disk. Enabled: Obsidian's
        own community-plugins.json (the list it writes when a plugin is
        toggled on in Settings) names it. `doctor` needs both -- a plugin
        that is present but never turned on captures nothing.

        Three states, not two. The first version of this collapsed "the file
        says it is off" and "I could not read the file" into `enabled=False`,
        and `doctor` printed "installed but not enabled -- turn it on in
        Settings" at lj over a plugin that was enabled the whole time. An
        unreadable answer is not a negative answer, and a check that reports
        it as one sends you to fix something that is not broken. `known` is
        the flag that keeps those apart.
        """
        obsidian_dir = self.cfg.vault / ".obsidian"
        installed = (obsidian_dir / "plugins" / "second-brain-capture" / "main.js").exists()
        enabled = False
        known = True
        if installed:
            listing = obsidian_dir / "community-plugins.json"
            try:
                enabled = "second-brain-capture" in json.loads(
                    listing.read_text(encoding="utf-8")
                )
            except FileNotFoundError:
                # Obsidian writes this file the first time any community
                # plugin is enabled. Absent means none ever was, which is a
                # real "off" -- the vault has been opened and nothing is on.
                enabled = False
            except Exception:
                # Unreadable, malformed, or locked by Obsidian mid-write.
                known = False
        return {"installed": installed, "enabled": enabled, "known": known}

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

    def _card_payload(
        self, deck: Deck, card: Card, *, reveal: bool = True,
        deadline: Optional[dt.date] = None,
    ) -> Dict[str, Any]:
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
            "mode": card.mode,
            # Options are safe before the reveal — showing them *is* the
            # question. `Card.options()` fixes the order on the card id, so
            # the list here and the list after the reveal are the same list.
            "options": card.options(),
            "topic": card.topic,
            # Sweller: a scaffold for the first attempts, withdrawn as
            # competence rises. Available before the reveal on purpose — that
            # is the whole point of a worked example.
            "worked_example": tutor.worked_example_for(card, deck, self.cfg),
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
            "intervals": tutor.button_intervals(card, self.cfg, deadline),
            "leech": tutor.is_leech(card, self.cfg),
            "exam": deadline.isoformat() if deadline else None,
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
        self, note_id: str, *, max_cards: Optional[int] = None, source: str = "",
        fill_gaps: bool = False,
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

        outage = self._model_outage("generate")
        if outage:
            # Queue rather than quietly hand back cloze deletions. A card is
            # permanent and spaced repetition will drill whatever it says into
            # you, so the offline generator is a stopgap and not something to
            # accumulate twenty of; the honest move is to keep the *request*
            # and replay it against a real model. See sb/jobqueue.py.
            entry = self.card_queue.add(
                note_id,
                title=note.title,
                max_cards=max_cards,
                source=source,
                reason=outage,
            )
            self.incidents.record(
                incidentsmod.OLLAMA,
                f"Card generation is waiting on a model — {outage}.",
                hint="Start Ollama (`ollama serve`). Queued notes generate "
                     "themselves as soon as it answers.",
                key="cards",
            )
            self.vault.log_line(
                "study", f"queued card generation for {note.id} ({outage})"
            )
            return {
                "deck": self._deck_payload(deck),
                "generated": 0,
                "rejected": 0,
                "repaired": 0,
                "rejections": {},
                "passages": 0,
                "provider": "",
                "degraded": True,
                "queued": True,
                "queued_at": entry["queued_at"],
                "pending": self.card_queue.count(),
                "note": (
                    "Queued, waiting on Ollama — nothing was generated. "
                    f"{outage}. This note will generate itself when a model answers."
                ),
            }

        material = (source or "").strip() or note.body
        material = generate.expand_links(material, self._link_resolver())
        result = generate.add_to_deck(
            deck,
            material,
            self.cfg,
            max_cards=max_cards or self.cfg.study.generate_max_cards,
            fill_gaps=fill_gaps,
        )
        self.decks.save(deck)
        self.vault.log_line(
            "study", f"generated {len(result.cards)} cards for {note.id} ({result.provider})"
        )
        # It answered, so whatever was queued on its absence is no longer true.
        self.card_queue.remove(note_id)
        self.incidents.clear(incidentsmod.OLLAMA, key="cards")
        self._sync_calendar_quiet()
        return {
            "deck": self._deck_payload(deck),
            "generated": len(result.cards),
            "rejected": result.rejected,
            "repaired": result.repaired,
            "rejections": result.rejections,
            "passages": result.chunks,
            "provider": result.provider,
            "degraded": result.degraded,
            "queued": False,
            "pending": self.card_queue.count(),
            "note": result.note,
            "grounding": result.grounding,
            "concepts": result.concepts,
            "coverage": result.coverage,
            "visited": len(result.visited),
            "fill_gaps": fill_gaps,
        }

    def _model_outage(self, role: str = "") -> str:
        """Empty when a model is reachable, otherwise one clause saying why not.

        Two different things look identical from inside `resolve_provider` and
        must not be confused here. `llm.provider: heuristic` is a *decision* —
        this machine has no model and the rule-based paths are the product, so
        nothing is wrong and nothing should be queued or reported. A configured
        Ollama that does not answer is an *outage*. Only the second one is a
        problem, and only the second one gets an incident.
        """
        from .llm import get_provider

        provider = get_provider(self.cfg.llm, role)
        if not getattr(provider, "is_llm", False):
            return ""
        if provider.available():
            return ""
        where = getattr(self.cfg.llm, "ollama_url", "") if provider.name == "ollama" else ""
        return f"{provider.name} is not answering" + (f" at {where}" if where else "")

    def _note_degradation(self, what: str, detail: str = "") -> None:
        """File the one incident that means "a model was needed and there was
        none, so you got the smaller answer".

        Deliberately unkeyed. Six degraded paths must not become six banners;
        `since` says how long this has been going on, `count` says how many
        times it has bitten, and the message names the most recent thing that
        had to go without. `health()` retires it the moment the model answers.

        Separate from the `ollama/cards` incident because the two ask for
        different things. This one is information — that answer was worse than
        it should have been, and re-running it later will improve it. That one
        is a promise: nothing was produced, and it will run by itself when the
        model comes back.

        Silent when `llm.provider` is heuristic: a machine with no model is a
        configuration, not an outage, and the rule-based paths are the product
        there rather than a fallback.
        """
        if (self.cfg.llm.provider or "").lower() in ("heuristic", "none", "off"):
            return
        try:
            self.incidents.record(
                incidentsmod.OLLAMA,
                f"No model answered — {what} used its offline fallback.",
                hint="Start Ollama (`ollama serve`). Nothing was lost; the "
                     "offline path gives a smaller answer, not a missing one.",
                detail=detail,
            )
        except Exception:  # an incident must never become the failure
            pass

    def drain_card_queue(self, limit: int = 0) -> Dict[str, Any]:
        """Run the generation requests that were made while the model was down.

        Called from the Drop watcher's tick and from `/api/queue/drain`, which
        between them mean a queued note generates itself without lj having to
        remember it was queued. Each entry is taken off the queue *before* it
        runs, so a request that now fails for an unrelated reason — the note
        was deleted in the meantime — cannot wedge the queue in a retry loop.
        """
        pending = self.card_queue.pending()
        if not pending:
            return {"waiting": False, "drained": 0, "pending": 0, "results": [],
                    "note": "nothing queued"}
        outage = self._model_outage("generate")
        if outage:
            return {
                "waiting": True,
                "drained": 0,
                "pending": len(pending),
                "results": [],
                "note": f"still waiting on a model — {outage}",
            }

        results: List[Dict[str, Any]] = []
        for entry in (pending[:limit] if limit else pending):
            note_id = entry["note_id"]
            self.card_queue.take(note_id)
            try:
                made = self.generate_cards(
                    note_id,
                    max_cards=entry.get("max_cards"),
                    source=entry.get("source") or "",
                )
            except Exception as exc:
                results.append({
                    "note_id": note_id,
                    "title": entry.get("title", ""),
                    "error": f"{type(exc).__name__}: {exc}",
                })
                self.vault.log_line("study", f"queued generation failed for {note_id}: {exc!r}")
                continue
            results.append({
                "note_id": note_id,
                "title": entry.get("title", ""),
                "generated": made.get("generated", 0),
                "queued_at": entry.get("queued_at", ""),
            })
        self.incidents.clear(incidentsmod.OLLAMA, key="cards")
        return {
            "waiting": False,
            "drained": sum(1 for r in results if "error" not in r),
            "failed": sum(1 for r in results if "error" in r),
            "pending": self.card_queue.count(),
            "results": results,
            "note": f"{len(results)} queued request(s) replayed",
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
        payload = self._deck_payload(deck)
        # Advisory, never a refusal: a card lj typed is a decision, not a
        # draft. The generator is held to sb/quality.py; a person is told.
        verdict = quality.assess(front, back, max_words=self.cfg.study.max_answer_words)
        if not verdict.ok:
            payload["warning"] = verdict.reason
        return payload

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

    def generate_folder(
        self,
        folder: str = "",
        *,
        limit: Optional[int] = None,
        max_cards: Optional[int] = None,
        include_existing: bool = False,
        min_words: int = 40,
        dry_run: bool = False,
        budget_seconds: float = 240.0,
    ) -> Dict[str, Any]:
        """Make cards for every note under `folder`, in one run.

        The deck machinery has been finished for weeks and the vault has zero
        cards in it, and the reason is arithmetic rather than design: cards
        are generated one note at a time from a dropdown, and a subject folder
        holds sixty notes. Sixty clicks is not a workflow anyone starts.

        Three things keep this honest rather than merely fast:

          * **One model probe for the whole run.** If nothing answers, every
            candidate is queued in one pass and says so, instead of sixty
            separate timeouts and sixty separate incidents.
          * **A wall-clock budget.** This is reachable from a web request, and
            a request that runs for eleven minutes is a hung app. When the
            budget is spent it stops on a note boundary and reports what is
            left, so running it again picks up exactly where it stopped.
          * **Everything still lands as a draft.** Bulk generation changes how
            many cards get *drafted*; it changes nothing about the rule that
            no card reaches the scheduler unread.

        `folder` is vault-relative and matches its subtree, so "30-Resources"
        means everything filed under it. Empty means the whole vault.
        """
        started = _now()
        scope = (folder or "").strip().strip("/")
        where = self.vault.folders_by_id()
        have = {d.note_id: d for d in self.decks.all()}

        candidates: List[Note] = []
        skipped: List[Dict[str, Any]] = []
        for note in self.notes():
            if note.bucket not in (Bucket.PROJECT, Bucket.RESOURCE):
                continue
            if scope and not tutor.in_folders(where.get(note.id, ""), [scope]):
                continue
            if note.bucket == Bucket.PROJECT and note.project and \
                    note.project.status == ProjectStatus.DONE:
                skipped.append(self._skip_row(note, "project already done"))
                continue
            words = len(note.body.split())
            if words < min_words:
                skipped.append(self._skip_row(note, f"only {words} words"))
                continue
            deck = have.get(note.id)
            if deck is not None and deck.cards and not include_existing:
                skipped.append(
                    self._skip_row(note, f"already has {len(deck.cards)} cards")
                )
                continue
            candidates.append(note)

        candidates.sort(key=lambda n: (where.get(n.id, ""), n.title))
        selected = candidates[: limit] if limit else candidates

        if dry_run:
            return {
                "folder": scope,
                "dry_run": True,
                "candidates": len(candidates),
                "attempted": 0,
                "generated": 0,
                "queued": 0,
                "notes": [
                    {
                        "note_id": n.id,
                        "title": n.title,
                        "folder": where.get(n.id, ""),
                        "words": len(n.body.split()),
                    }
                    for n in selected
                ],
                "skipped": skipped,
                "stopped_early": False,
                "seconds": 0.0,
                "note": (
                    f"{len(candidates)} note(s) would get cards"
                    + (f", {len(selected)} in this run" if limit else "")
                    + f". {len(skipped)} skipped."
                ),
            }

        outage = self._model_outage("generate")
        if outage:
            for note in selected:
                self.card_queue.add(
                    note.id, title=note.title, max_cards=max_cards, reason=outage
                )
            if selected:
                self.incidents.record(
                    incidentsmod.OLLAMA,
                    f"Queued {len(selected)} note(s) for card generation — {outage}.",
                    hint="Start Ollama (`ollama serve`). The queue drains itself.",
                    key="cards",
                )
                self.vault.log_line(
                    "study", f"queued {len(selected)} notes from {scope or 'the vault'}"
                )
            return {
                "folder": scope,
                "dry_run": False,
                "candidates": len(candidates),
                "attempted": 0,
                "generated": 0,
                "queued": len(selected),
                "notes": [],
                "skipped": skipped,
                "stopped_early": False,
                "degraded": True,
                "seconds": (_now() - started).total_seconds(),
                "note": (
                    f"Nothing generated — {outage}. {len(selected)} note(s) are "
                    f"queued and will generate themselves when a model answers."
                ),
            }

        rows: List[Dict[str, Any]] = []
        total = 0
        stopped_early = False
        for note in selected:
            if (_now() - started).total_seconds() > budget_seconds:
                stopped_early = True
                break
            try:
                out = self.generate_cards(note.id, max_cards=max_cards)
            except Exception as exc:  # noqa: BLE001 - one bad note is not a failed run
                self.vault.log_line("study", f"bulk generate failed  {note.id}  {exc!r}")
                rows.append(
                    {
                        "note_id": note.id,
                        "title": note.title,
                        "folder": where.get(note.id, ""),
                        "cards": 0,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                continue
            total += int(out.get("generated") or 0)
            rows.append(
                {
                    "note_id": note.id,
                    "title": note.title,
                    "folder": where.get(note.id, ""),
                    "cards": int(out.get("generated") or 0),
                    "rejected": int(out.get("rejected") or 0),
                    "rejections": out.get("rejections") or {},
                    "provider": out.get("provider") or "",
                    "degraded": bool(out.get("degraded")),
                    "queued": bool(out.get("queued")),
                }
            )

        done = len(rows)
        remaining = len(candidates) - done
        return {
            "folder": scope,
            "dry_run": False,
            "candidates": len(candidates),
            "attempted": done,
            "generated": total,
            "queued": sum(1 for r in rows if r.get("queued")),
            "notes": rows,
            "skipped": skipped,
            "stopped_early": stopped_early,
            "remaining": max(0, remaining),
            "seconds": round((_now() - started).total_seconds(), 1),
            "note": (
                f"{total} card(s) drafted from {done} note(s)."
                + (
                    f" Stopped at the {int(budget_seconds)}s budget with "
                    f"{max(0, remaining)} note(s) left — run it again to continue."
                    if stopped_early
                    else ""
                )
                + " Everything is a draft until you approve it."
            ),
        }

    def _skip_row(self, note: Note, reason: str) -> Dict[str, Any]:
        return {"note_id": note.id, "title": note.title, "reason": reason}

    def study_overview(self) -> Dict[str, Any]:
        """The study home screen: subjects, what is due, how it is going."""
        decks = self.decks.all()
        today = dt.date.today()
        reviewed, introduced = tutor.counted_today(self.decks)
        where = self.vault.folders_by_id()
        return {
            "decks": [
                {**tutor.deck_progress(d, self.cfg, today),
                 "folder": where.get(d.note_id, "")}
                for d in decks
            ],
            "folders": self._folder_summary(decks, where, today),
            "stats": tutor.stats(self.decks, decks, self.cfg),
            "today": {"reviewed": reviewed, "introduced": introduced},
            "limits": {
                "new_per_day": self.cfg.study.new_cards_per_day,
                "max_reviews": self.cfg.study.max_reviews_per_day,
                "session_size": self.cfg.study.session_size,
                "retention": self.cfg.study.desired_retention,
            },
            "graduation": self.graduation_candidates(),
            "leeches": [
                {"note_id": d.note_id, "subject": d.subject, "card_id": c.id,
                 "question": c.question(), "lapses": c.lapses, "status": c.status}
                for d in decks for c in d.cards if tutor.is_leech(c, self.cfg)
            ][:30],
            "candidates": self._deckable_notes(decks),
            "categories": taxonomy.as_dicts(self.cfg),
        }

    def _folder_summary(
        self, decks: List[Deck], where: Dict[str, str], today: dt.date
    ) -> List[Dict[str, Any]]:
        """Every folder that holds cards, with what is waiting in it.

        Counts roll up into ancestors, because selecting a folder selects
        everything under it: if 30-Resources/Statics has four cards due, then
        30-Resources shows four due too. Without that the number on the chip
        would contradict the session you get when you click it.
        """
        agg: Dict[str, Dict[str, int]] = {}
        with_drafts = bool(getattr(self.cfg.study, "drafts_in_session", False))
        for deck in decks:
            due, new, active = len(deck.due(today)), len(deck.new()), len(deck.active)
            drafts = len(deck.drafts) if with_drafts else 0
            if not active and not drafts:
                continue  # nothing here can come up in a session
            folder = tutor.normalize_folder(where.get(deck.note_id, ""))
            parts = folder.split("/") if folder else []
            # "" is the vault root, and every note is under it
            for depth in range(len(parts) + 1):
                key = "/".join(parts[:depth])
                row = agg.setdefault(
                    key, {"decks": 0, "due": 0, "new": 0, "cards": 0, "drafts": 0}
                )
                row["decks"] += 1
                row["due"] += due
                row["new"] += new
                row["cards"] += active
                row["drafts"] += drafts
        out = [
            {
                "path": path,
                "label": path.rsplit("/", 1)[-1] if path else "All notes",
                "depth": path.count("/") + 1 if path else 0,
                **counts,
            }
            for path, counts in agg.items()
        ]
        out.sort(key=lambda f: (f["depth"], f["path"]))
        return out

    def _deckable_notes(self, decks: List[Deck]) -> List[Dict[str, Any]]:
        """Notes worth making cards from that do not have any yet."""
        have = {d.note_id for d in decks}
        where = self.vault.folders_by_id()
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
                    "folder": where.get(note.id, ""),
                    "learning": bool(note.project and note.project.learning),
                    "category": taxonomy.categorize(note, self.cfg),
                    "words": len(note.body.split()),
                }
            )
        out.sort(key=lambda n: (not n["learning"], n["title"]))
        return out

    def study_session(
        self,
        subjects: Optional[List[str]] = None,
        limit: Optional[int] = None,
        folders: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Build a mixed-subject queue. Answers are posted one at a time, so
        this holds no server-side session state — close the tab mid-session
        and nothing is lost or double-counted."""
        decks = self.decks.all()
        reviewed, introduced = tutor.counted_today(self.decks)
        # Koriat & Bjork's illusion of competence, made actionable: the cards
        # lj predicted they knew and then missed go to the front. Scoped to a
        # recent window, because a miss from four months ago is a card that
        # has since been relearned, not a belief that is still wrong.
        since = dt.date.today() - dt.timedelta(days=self.cfg.study.overconfidence_window_days)
        priority = calibration.priority_card_ids(self.decks.reviews(since=since), since=since)
        session = tutor.build_session(
            decks,
            self.cfg,
            subjects=subjects,
            folders=folders,
            folder_of=self.vault.folders_by_id() if folders else None,
            limit=limit,
            reviewed_today=reviewed,
            introduced_today=introduced,
            priority=priority,
        )
        exams = {nid: self._exam_deadline(nid) for nid in {q.deck.note_id for q in session.queue}}
        return {
            "queue": [
                {
                    **self._card_payload(
                        q.deck, q.card, reveal=False, deadline=exams.get(q.deck.note_id)
                    ),
                    "reason": q.reason,
                    "overdue_days": q.overdue_days,
                }
                for q in session.queue
            ],
            "due_available": session.due_available,
            "new_available": session.new_available,
            "drafts_available": session.drafts_available,
            "buried": session.buried,
            "capped": session.capped,
            "message": session.message,
            "started_at": _now().isoformat(),
        }

    def study_reveal(self, note_id: str, card_id: str) -> Dict[str, Any]:
        """The answer side, fetched only when asked for — so the answer is
        never sitting in the page while you are trying to recall it."""
        deck = self.deck(note_id)
        return self._card_payload(
            deck, deck.card(card_id), reveal=True, deadline=self._exam_deadline(note_id)
        )

    def study_answer(
        self,
        note_id: str,
        card_id: str,
        *,
        grade: Optional[int] = None,
        mode: str = "self",
        typed: str = "",
        seconds: float = 0.0,
        confidence: Any = None,
    ) -> Dict[str, Any]:
        """Grade one card. `mode="recall"` marks a typed answer first.

        `confidence` is lj's own prediction, tapped before the answer was
        revealed. Optional at every layer — a session where it is never sent
        behaves exactly as it did before — and coerced rather than validated,
        because a malformed prediction should cost the prediction, not the
        answer. See sb/calibration.py.
        """
        deck = self.deck(note_id)
        card = deck.card(card_id)

        # A typed answer is marked here only when the caller has not already
        # settled on a grade. The UI marks first (see `study_mark`) and shows
        # you the verdict *before* it counts, so a model that marks you wrong
        # costs you a click rather than a card.
        grading = None
        if mode == "choice" and grade is None:
            # No model is consulted for a choice card, so there is nothing to
            # degrade and nothing to warn about.
            grading = tutor.grade_choice(card, typed)
            grade = grading.grade
        elif mode == "recall" and grade is None:
            grading = tutor.grade_recall(card.question(), card.answer(), typed, self.cfg)
            grade = grading.grade
        if grade is None:
            raise ValueError("no grade given")
        if grading is not None and grading.graded_by == "rule" and (typed or "").strip():
            # Word overlap cannot recognise a right answer phrased differently,
            # so a session marked this way is a session marked harshly. The
            # response already carries `graded_by`; this is so it is visible
            # without reading a field.
            self._note_degradation("marking your typed answer", grading.feedback)

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
            confidence=calibration.clamp(confidence),
            deadline=self._exam_deadline(note_id),
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
            "confidence": result.confidence,
            "overconfident": result.overconfident,
            # Chi et al.: the prompt is the intervention. Asking only on
            # Again/Hard keeps it from becoming the thing that ends sessions.
            "ask_why": tutor.wants_self_explanation(result.grade),
            "leech": result.leech,
            "suspended": card.status == "suspended",
            "capped_to": result.capped_to,
        }

    def study_mark(self, note_id: str, card_id: str, typed: str) -> Dict[str, Any]:
        """Mark a typed answer without scheduling anything.

        Separating marking from grading is the whole reason free recall is
        safe to use: the model proposes, you dispose. Nothing is written to
        the deck or the review log until you accept a grade.
        """
        deck = self.deck(note_id)
        card = deck.card(card_id)
        grading = (
            tutor.grade_choice(card, typed)
            if card.kind == "mcq"
            else tutor.grade_recall(card.question(), card.answer(), typed, self.cfg)
        )
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
        # `tutor.explain` says so in its prose when there is no model, which
        # the reader sees but the caller cannot branch on. One reachability
        # check on a button press buys a flag the UI can act on, and an
        # incident so a whole evening of "there is nothing to ask" is visible.
        outage = self._model_outage("explain")
        if outage:
            self._note_degradation("explaining a card", outage)
        return {
            "answer": tutor.explain(card, note.body, question, self.cfg),
            "degraded": bool(outage),
            "note": outage,
        }

    def study_self_explain(self, note_id: str, card_id: str, said: str) -> Dict[str, Any]:
        """lj explains first; the model marks it; nothing is scheduled.

        Roadmap 2.3. `explain()` runs the transfer in the weaker direction —
        the model explains to lj — and the literature is unambiguous that the
        stronger one is lj producing the account (Slamecka & Graf's generation
        effect; Chi et al. on self-explanation, and on *prompted* beating
        spontaneous). This is the prompt.

        Written to `_decks/_explanations.jsonl`, not to the review log: it is
        not a graded answer and must never be counted as one. Nothing here
        touches the card's schedule, so a bad marking costs a sentence.
        """
        deck = self.deck(note_id)
        card = deck.card(card_id)
        note = self.note(note_id)
        marked = tutor.mark_self_explanation(card, note.body, said, self.cfg)
        self.decks.log_explanation(
            {
                "at": _now().isoformat(),
                "note_id": note_id,
                "subject": deck.subject,
                "card": card_id,
                "said": marked.said[:1000],
                "verdict": marked.verdict,
                "score": marked.score,
                "graded_by": marked.graded_by,
            }
        )
        return {
            **marked.as_dict(),
            # The tutor's own explanation, shown *after* theirs. Order is the
            # whole point: reading it first is the passive path this replaces.
            "answer": tutor.explain(card, note.body, "", self.cfg),
        }

    # -- Anki x NotebookLM: the loop around a card ---------------------------

    def _exam_deadline(self, note_id: str) -> Optional[dt.date]:
        """The date a deck must be known by, or None.

        An open Project with a deadline still ahead. `exam_cap` in
        sb/tutor.py pulls reviews back to the eve of it.
        """
        if not getattr(self.cfg.study, "exam_cap", True):
            return None
        try:
            note = self.note(note_id)
        except Exception:
            return None
        project = note.project
        if note.bucket != Bucket.PROJECT or not project or not project.deadline:
            return None
        if project.status == ProjectStatus.DONE or project.deadline <= dt.date.today():
            return None
        return project.deadline

    def _obsidian_uri(self, path: Optional[Path]) -> str:
        if not path:
            return ""
        from urllib.parse import quote

        try:
            rel = Path(path).resolve().relative_to(Path(self.vault.root).resolve())
        except (ValueError, OSError):
            return ""
        name = Path(self.vault.root).resolve().name
        return (
            "obsidian://open?vault=" + quote(name, safe="")
            + "&file=" + quote(rel.as_posix(), safe="")
        )

    def _material(self, note_id: str, cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """A note's card material, split the way the generator split it."""
        if cache is not None and note_id in cache:
            return cache[note_id]
        path, note = self.vault.get(note_id)
        text = generate.expand_links(note.body, self._link_resolver())
        passages = generate.chunk(text)
        out = {
            "note": note,
            "path": path,
            "passages": passages,
            "guide": guidemod.Guide(
                passages=passages,
                headings=guidemod.headings_for(frontmattermod.strip(text), passages),
            ),
        }
        if cache is not None:
            cache[note_id] = out
        return out

    def _quoted_in(self, note: Note, path: Path, quote: str) -> Tuple[str, Optional[Path]]:
        """Which note a citation actually came from — the deck's own note, or
        one of the notes it links to (generation reads those too)."""
        needle = guidemod.norm(quote)
        if not needle or needle in guidemod.norm(note.body):
            return note.title, path
        index = self.vault.title_index()
        for match in generate.WIKILINK.finditer(note.body or ""):
            title = match.group(1).strip()
            try:
                other = self.vault.resolve_title(title, index)
            except Exception:
                other = None
            if other and needle in guidemod.norm(other.body):
                try:
                    other_path, _ = self.vault.get(other.id)
                except Exception:
                    other_path = None
                return other.title, other_path
        return note.title, path

    def _source_context(
        self, deck: Deck, card: Card, cache: Optional[Dict[str, Any]] = None
    ) -> Dict[str, Any]:
        mat = self._material(deck.note_id, cache)
        passages = mat["passages"]
        index = guidemod.passage_of(card, passages)
        title, where = self._quoted_in(mat["note"], mat["path"], card.source or "")
        out: Dict[str, Any] = {
            "note_id": deck.note_id,
            "card_id": card.id,
            "quote": card.source,
            "found": index is not None,
            "index": index,
            "passages": len(passages),
            "note_title": title,
            "obsidian_uri": self._obsidian_uri(where),
            "passage": "",
            "heading": "",
            "before": "",
            "after": "",
        }
        if index is not None:
            flat = lambda t: re.sub(r"\s+", " ", t or "").strip()
            out["passage"] = passages[index].strip()
            out["heading"] = mat["guide"].heading(index)
            if index > 0:
                out["before"] = flat(passages[index - 1])[-160:]
            if index + 1 < len(passages):
                out["after"] = flat(passages[index + 1])[:160]
        return out

    def study_source(self, note_id: str, card_id: str) -> Dict[str, Any]:
        """NotebookLM's citation click: the card's line, in its passage, with
        a link that opens the note in Obsidian."""
        deck = self.deck(note_id)
        return self._source_context(deck, deck.card(card_id))

    def deck_coverage(self, note_id: str) -> Dict[str, Any]:
        """Which passages of the note have cards, and which key ideas none."""
        deck = self.deck(note_id)
        mat = self._material(note_id)
        g = guidemod.build(
            frontmattermod.strip(generate.expand_links(mat["note"].body, self._link_resolver())),
            mat["passages"],
        )
        return guidemod.coverage(deck.cards, g)

    def study_triage(
        self, note_id: str, card_id: str, action: str,
        front: Optional[str] = None, back: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Keep, fix, drop or suspend a card from inside a session.

        Every action is logged with the text before and after, to
        `_decks/_triage.jsonl`. A drop is the most useful label this system
        collects: a card the generator wrote that a human would not.
        """
        action = (action or "").strip().lower()
        if action not in ("keep", "fix", "drop", "suspend"):
            raise ValueError(f"unknown triage action {action!r}")
        deck = self.deck(note_id)
        card = deck.card(card_id)
        before = {"front": card.front, "back": card.back, "source": card.source,
                  "status": card.status, "kind": card.kind}
        warning = ""
        if action == "drop":
            deck.cards = [c for c in deck.cards if c.id != card_id]
        elif action == "keep":
            if card.status == "draft":
                card.status = "active"
        elif action == "suspend":
            card.status = "suspended"
        else:
            new_front = (front if front is not None else card.front).strip()
            new_back = (back if back is not None else card.back).strip()
            if not new_front or not new_back:
                raise ValueError("a card needs both a question and an answer")
            card.front, card.back = new_front, new_back
            if card.kind not in ("cloze", "mcq", "explain"):
                verdict = quality.assess(new_front, new_back,
                                         max_words=self.cfg.study.max_answer_words)
                warning = "" if verdict.ok else verdict.reason
        self.decks.save(deck)
        self.decks.log_triage({
            "at": _now().isoformat(),
            "note_id": note_id,
            "subject": deck.subject,
            "card": card_id,
            "action": action,
            "before": before,
            "after": None if action == "drop" else {
                "front": card.front, "back": card.back, "status": card.status,
            },
        })
        if action in ("keep", "drop", "suspend"):
            self._sync_calendar_quiet()
        out: Dict[str, Any] = {
            "action": action,
            "deck": tutor.deck_progress(deck, self.cfg),
            "card": None if action == "drop" else self._card_payload(
                deck, card, reveal=False, deadline=self._exam_deadline(note_id)
            ),
        }
        if warning:
            out["warning"] = warning
        return out

    def study_rewrite(self, note_id: str, card_id: str) -> Dict[str, Any]:
        """A leech, rewritten from its own passage.

        Anki's advice for a leech is to fix the card, not to keep failing it;
        NotebookLM's contribution is doing the fixing from the source. The
        replacements go through the same grounding and quality gates as any
        generated card and land as drafts, so they come up in the next
        session with Keep / Fix / Drop. The leech stays suspended with its
        history intact.
        """
        deck = self.deck(note_id)
        card = deck.card(card_id)
        ctx = self._source_context(deck, card)
        material = ctx["passage"] or card.source or self.note(note_id).body
        focus = (
            f'The learner keeps forgetting this card. Q: "{card.question()}" '
            f'A: "{card.back}". Write up to 3 better cards that test the same '
            "idea from this passage: split it into smaller facts, give the "
            "context that makes it memorable, or make it a cloze. Do not "
            "repeat the card as it is."
        )
        result = generate.generate(
            material, self.cfg, subject=deck.subject, max_cards=3, per_chunk=3,
            focus=focus,
        )
        taken = {generate._norm(c.front) for c in deck.cards}
        made: List[Card] = []
        for new in result.cards:
            if generate._norm(new.front) in taken:
                continue
            taken.add(generate._norm(new.front))
            made.append(deck.add(
                front=new.front, back=new.back, source=new.source, topic=new.topic,
                choices=list(new.choices), mode=new.mode, status="draft",
            ))
        if not made:
            clozed = quality.to_cloze(card.front, card.back, card.source, material)
            if clozed and generate._norm(clozed[0]) not in taken:
                made.append(deck.add(front=clozed[0], back=clozed[1], source=clozed[2],
                                     status="draft"))
        if card.status != "suspended":
            card.status = "suspended"
        self.decks.save(deck)
        self.decks.log_triage({
            "at": _now().isoformat(), "note_id": note_id, "subject": deck.subject,
            "card": card_id, "action": "rewrite",
            "before": {"front": card.front, "back": card.back, "lapses": card.lapses},
            "after": {"made": [c.id for c in made]},
        })
        if result.degraded:
            self._note_degradation("rewriting a leech", result.note)
        return {
            "made": len(made),
            "cards": [self._card_payload(deck, c) for c in made],
            "provider": result.provider,
            "degraded": result.degraded,
            "note": (
                f"{len(made)} replacement draft(s) — they come up in your next session."
                if made else
                "Nothing better came out of the source. Edit the card by hand in Decks & cards."
            ),
        }

    def study_debrief(self, since: str = "") -> Dict[str, Any]:
        """What a session showed, and what to re-read.

        The Anki half is the tally. The NotebookLM half is turning the misses
        back into source: the passages behind the cards that failed, grouped
        so one paragraph that three cards came from is read once.
        """
        started = _parse_moment(since) or dt.datetime.combine(
            dt.date.today(), dt.time.min
        ).astimezone()
        reviews = []
        for rec in self.decks.reviews(since=started.date()):
            at = _parse_moment(rec.get("at"))
            if at and at >= started:
                reviews.append(rec)
        decks = {d.note_id: d for d in self.decks.all()}

        by_subject: Dict[str, Dict[str, int]] = {}
        worst: Dict[Tuple[str, str], Dict[str, Any]] = {}
        seconds = 0.0
        correct = 0
        for rec in reviews:
            grade = int(rec.get("grade") or 0)
            ok = grade >= 3
            correct += ok
            seconds += float(rec.get("seconds") or 0)
            row = by_subject.setdefault(rec.get("subject") or rec.get("note_id") or "?",
                                        {"reviewed": 0, "correct": 0})
            row["reviewed"] += 1
            row["correct"] += ok
            conf = rec.get("confidence")
            over = conf is not None and float(conf) >= calibration.OVERCONFIDENT_AT and grade == 1
            if grade <= 2 or over:
                key = (str(rec.get("note_id")), str(rec.get("card")))
                have = worst.get(key)
                if not have or grade < have["grade"]:
                    worst[key] = {"grade": grade, "overconfident": over or bool(have and have["overconfident"]),
                                  "leech": bool(rec.get("leech"))}
                elif over:
                    have["overconfident"] = True

        cache: Dict[str, Any] = {}
        shaky: List[Dict[str, Any]] = []
        groups: Dict[Tuple[str, Any], Dict[str, Any]] = {}
        for (note_id, card_id), info in worst.items():
            deck = decks.get(note_id)
            if not deck:
                continue
            try:
                card = deck.card(card_id)
            except KeyError:
                continue
            shaky.append({
                "note_id": note_id, "card_id": card_id, "subject": deck.subject,
                "question": card.question(),
                # A cloze's full answer repeats its question; the blank is enough.
                "answer": card.back if card.kind == "cloze" else card.answer(),
                "source": card.source, **info,
                "status": card.status,
            })
            try:
                ctx = self._source_context(deck, card, cache)
            except Exception:
                continue
            gkey = (note_id, ctx["index"] if ctx["found"] else f"q:{card.source}")
            group = groups.setdefault(gkey, {
                "note_id": note_id, "subject": deck.subject,
                "note_title": ctx["note_title"], "heading": ctx["heading"],
                "passage": (ctx["passage"] or card.source)[:900],
                "quotes": [], "cards": 0, "obsidian_uri": ctx["obsidian_uri"],
            })
            group["cards"] += 1
            if card.source and card.source not in group["quotes"]:
                group["quotes"].append(card.source)
        shaky.sort(key=lambda r: (not r["overconfident"], r["grade"], r["subject"]))
        reread = sorted(groups.values(), key=lambda g: -g["cards"])[:6]

        triaged: Dict[str, int] = {}
        for rec in self.decks.triage():
            at = _parse_moment(rec.get("at"))
            if at and at >= started:
                action = str(rec.get("action"))
                triaged[action] = triaged.get(action, 0) + 1

        tomorrow = dt.date.today() + dt.timedelta(days=1)
        due_tomorrow = sum(
            1 for d in decks.values() for c in d.active
            if c.due is not None and c.due <= tomorrow and not c.is_new
        )
        n = len(reviews)
        if not n:
            message = "Nothing answered in this session yet."
        else:
            pct = round(100 * correct / n)
            message = f"{n} answered, {pct}% remembered"
            if shaky:
                message += f" — {len(reread) or len(shaky)} passage(s) worth re-reading"
        return {
            "since": started.isoformat(),
            "reviewed": n,
            "correct": correct,
            "accuracy": round(correct / n, 3) if n else None,
            "minutes": round(seconds / 60, 1),
            "by_subject": [{"subject": k, **v} for k, v in sorted(by_subject.items())],
            "shaky": shaky[:20],
            "reread": reread,
            "leeches": [r for r in shaky if r.get("leech") or r.get("status") == "suspended"],
            "triaged": triaged,
            "due_tomorrow": due_tomorrow,
            "leech_lapses": int(getattr(self.cfg.study, "leech_lapses", 8) or 0),
            "message": message,
        }

    def study_reminder(self, at: Optional[dt.datetime] = None) -> Dict[str, Any]:
        """The four facts a desktop reminder needs, plus the verdict.

        Story H3. Deliberately much smaller than `study_overview()`: the
        resident notifier polls this every minute, and overview walks the
        vault for folder rollups, graduation candidates and deckable notes —
        none of which anyone is going to read from a toast. This touches the
        deck store and nothing else.

        The verdict is computed here as well as in the notifier's offline
        fallback, from the one function in sb/reminders.py, so "why did it not
        fire?" has exactly one answer no matter which path produced it.
        """
        at = at or remindersmod.now()
        decks = self.decks.all()
        cards_due = sum(len(d.due(at.date())) for d in decks)
        reviewed, _ = tutor.counted_today(self.decks, at.date())
        state = remindersmod.read_state(remindersmod.state_path(self.cfg))
        starts_at = remindersmod.study_time(self.cfg)
        decision = remindersmod.decide(
            at=at,
            cards_due=cards_due,
            reviewed_today=reviewed,
            starts_at=starts_at,
            state=state,
        )
        return {
            "at": at.isoformat(),
            "cards_due": cards_due,
            "reviewed_today": reviewed,
            "decks": len(decks),
            "starts_at": starts_at.strftime("%H:%M"),
            "state": state.as_dict(),
            "decision": decision.as_dict(),
            "url": f"http://{self.cfg.host}:{self.cfg.port}/study",
        }

    def fit_weights(self, write: bool = False) -> Dict[str, Any]:
        """Fit FSRS to lj's own review history. Roadmap Tier 4.

        The trigger — roughly 1,000 reviews — is enforced inside `sb/fit.py`
        rather than checked here, and `write=True` is refused below it. Every
        review line has carried the pre-review state since phase 2 precisely
        so that this needed no migration when the trigger finally fired.
        """
        result = fitmod.fit(self.decks.reviews())
        if write and result["enough"] and result["improved"]:
            path = fitmod.save(self.cfg.deck_dir, result)
            result["written"] = str(path)
            self.vault.log_line(
                "study", f"fitted FSRS weights from {result['n']} reviews -> {path.name}"
            )
        elif write:
            result["written"] = ""
            result["refused"] = (
                "not enough reviews yet" if not result["enough"] else "no improvement over the defaults"
            )
        return result

    def study_stats(self) -> Dict[str, Any]:
        decks = self.decks.all()
        stats = tutor.stats(self.decks, decks, self.cfg)
        window = dt.date.today() - dt.timedelta(days=self.cfg.study.overconfidence_window_days)
        reviews = list(self.decks.reviews())
        stats["calibration"] = calibration.curve(reviews).as_dict()
        stats["overconfident_cards"] = calibration.overconfident_cards(reviews, since=window)
        return stats

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
        if answer.degraded:
            self._note_degradation("answering a question", answer.note)
        return {
            "question": question,
            "answer": answer.text,
            "degraded": answer.degraded,
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
        if result.degraded:
            # The uncertain band was dropped rather than guessed — the right
            # call, and a silent one: the note simply comes back with fewer
            # links and nothing says which ones were never judged.
            self._note_degradation("judging the uncertain links", result.note)
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

        results, changed, skipped, model_calls, degraded = [], 0, 0, 0, 0
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
            if result.degraded:
                degraded += 1
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
        if degraded:
            self._note_degradation(
                f"judging the uncertain links on {degraded} note(s)",
                "the confident tiers still ran; only the model-judged band was skipped",
            )
        return {
            "scanned": len(targets),
            "changed": changed,
            "skipped": skipped,
            "degraded": degraded,
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

        try:
            stats = self.index.build(self.notes(), force=force)
        except Exception as exc:
            # The index rebuilds itself the first time a question is asked, on
            # whatever thread asked it. A build that dies there used to leave
            # nothing but a traceback in a terminal lj had closed.
            self.vault.log_line("index", f"build failed: {type(exc).__name__}: {exc}")
            self.incidents.record(
                incidentsmod.INDEX,
                f"Rebuilding the search index failed ({type(exc).__name__}).",
                hint="Run `python run.py doctor` — and `_system/index/` can be "
                     "deleted safely, it rebuilds from the notes.",
                detail=str(exc)[:400],
            )
            raise
        self.incidents.clear(incidentsmod.INDEX)
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

    def health(self, progress: bool = False) -> Dict[str, Any]:
        """Wiring check. `progress` is off by default and that is deliberate.

        The dashboard calls `/api/health` on every load for its footer, and
        `_progress_report()` walks the whole vault, reads the whole review log
        and lints every Resource. Attaching that to a footer would undo phase
        9's read-path work on the one endpoint that runs most often. `doctor`
        asks for it; the footer does not.
        """
        from .llm import get_provider, lane_report

        provider = get_provider(self.cfg.llm)
        available = provider.available()
        models = provider.models() if available and hasattr(provider, "models") else []
        lanes = lane_report(self.cfg.llm) if available else None
        vault_exists = self.cfg.vault.exists()
        counts = self.vault.counts()

        # Reading is where a stale problem gets retired. A model that answers
        # again is the only evidence an outage is over, and this is the call
        # that has it — see sb/incidents.py on why record and clear must pair.
        # `clear` writes nothing when there was nothing open, so the normal
        # dashboard poll still touches no files.
        if available:
            self.incidents.clear(incidentsmod.OLLAMA)
        google = self._google_status()
        if google is not None:
            self._reconcile_calendar_auth(google)

        ics = self.cfg.ics_path
        decks = self.decks.all()
        # Counted once. It globs the deck folder, and it was being asked
        # twice on a call the dashboard makes on every load.
        deck_files = self._deck_file_count()
        return {
            "vault": str(self.cfg.vault),
            "vault_exists": vault_exists,
            # `vault_exists` alone lies: a wrong path that happens to resolve
            # to *some* directory (a relative Windows path re-read on Linux,
            # an empty scratch folder) still "exists" while holding nothing.
            # `doctor` renders on this instead — a vault only counts as OK
            # once it is both present and has a note in it somewhere.
            "vault_ok": vault_exists and any(counts.values()),
            "vault_note": self.cfg.vault_note,
            "counts": counts,
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
            # The templates are the shape of every note, and they are files lj
            # edits in Obsidian — which is how all nine of them came to be
            # silently corrupted for three weeks. Cheap: a stat and a parse
            # each, from the same cache the renderer uses.
            "templates": self._template_health(),
            "drop": {
                "path": str(self.cfg.drop_dir),
                # `intake.candidates` answers "[]" for a folder that is not
                # there, which read as "OK drop empty" over a folder that had
                # been deleted. A count of zero is only good news once the
                # thing being counted exists.
                "exists": self.cfg.drop_dir.is_dir(),
                "waiting": len(
                    intakemod.candidates(self.cfg.drop_dir, 0.0)
                ),
                "watch": self.cfg.intake.watch,
                "auto_floor": self.cfg.intake.auto_floor,
            },
            "study": {
                "decks": len(decks),
                # `DeckStore.all()` skips a deck file it cannot parse, which
                # makes a corrupted deck disappear from the count in silence —
                # the one number here that must not quietly shrink. Files on
                # disk minus decks read is exactly how many did that.
                "deck_files": deck_files,
                "unreadable": max(0, deck_files - len(decks)),
                "retention": self.cfg.study.desired_retention,
                "path": str(self.cfg.deck_dir),
            },
            # Problems background jobs hit while nobody was watching. Cheap:
            # one small file, empty in the normal case. See sb/incidents.py.
            "problems": self.incidents.open(),
            "queued_cards": self.card_queue.count(),
            # Everything that measures itself, and how far off it still is.
            # A feature that unlocks on a trigger is invisible until it fires,
            # which makes it feel broken rather than pending — so `doctor`
            # states the distance to each one. Opt-in: see the docstring.
            **({"progress": self._progress_report()} if progress else {}),
            "calendar": {
                "sink": self.cfg.calendar.sink,
                "task_sink": self.cfg.resolved_task_sink(),
                "ics": str(ics),
                # The sink names and the colour count are read back out of
                # config; they are not evidence that a calendar was ever
                # written. Whether the .ics file is on disk, and when, is.
                "ics_exists": ics.exists(),
                "ics_written": _mtime_iso(ics),
                "categories": len(taxonomy.table(self.cfg)),
                "google": google,
            },
        }

    def _template_health(self) -> Dict[str, Any]:
        """Which templates still describe a note this system can read.

        Not a style check. It exists for one specific failure: Obsidian's
        Properties editor rewrote every template in this vault into something
        that still *looked* right in the sidebar while producing notes with no
        id and no bucket, and nothing noticed for three weeks. So the test is
        the only one that would have caught it — build a real `Note` from each
        template and see what happens. See sb/render.py:check.
        """
        try:
            rows = rendermod.check(self.templates)
        except Exception as exc:  # noqa: BLE001 — health must not be the thing that breaks
            return {"ok": 0, "total": 0, "broken": [], "error": f"{type(exc).__name__}: {exc}"}
        broken = [r for r in rows if not r["ok"]]
        return {
            "ok": sum(1 for r in rows if r["ok"]),
            "total": len(rows),
            "broken": [
                {"template": r["template"], "problems": r["problems"]} for r in broken
            ],
        }

    def _deck_file_count(self) -> int:
        """Deck files on disk, counted the same way `DeckStore.all()` selects
        them — so the difference between the two is only ever a parse failure."""
        root = self.cfg.deck_dir
        if not root.is_dir():
            return 0
        return sum(
            1
            for p in root.glob("*.md")
            if not p.name.startswith("_") and p.name != "README.md"
        )

    def _reconcile_calendar_auth(self, google: Dict[str, Any]) -> None:
        """Google auth is a standing condition, so it belongs in the incident
        store rather than only in a `doctor` line lj has to go and run.

        `bump=False`: this runs from `health()`, which the dashboard polls.
        An unchanged condition must not cost a disk write every two minutes.
        """
        if google.get("ready"):
            self.incidents.clear(incidentsmod.CALENDAR, key="auth")
            return
        self.incidents.record(
            incidentsmod.CALENDAR,
            f"Google Calendar sync is not authorised — {google.get('reason') or 'no token'}.",
            hint="Run `python run.py sync` once; a browser will open to re-authorise.",
            key="auth",
            bump=False,
        )


# --------------------------------------------------------------------------
# note body rendering — the human-readable half of the file
# --------------------------------------------------------------------------


def _mtime_iso(path: Path) -> str:
    """When a file was last written, or "" if it is not there. Used wherever a
    `doctor` line would otherwise claim a produced artifact exists on the
    strength of the config that names it."""
    try:
        return dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(
            timespec="seconds"
        )
    except OSError:
        return ""


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


def _excerpt(body: str, limit: int = 180) -> str:
    """Enough of a note to recognise it by, on one line.

    Headings and list bullets are stripped rather than shown: the Inbox row is
    asking "what is this?", and `## Notes` answers that worse than the first
    real sentence under it does.
    """
    lines = []
    for line in (body or "").splitlines():
        line = re.sub(r"^\s*(?:[-*+]\s*(?:\[[ xX]\])?|#{1,6}|>\s*)\s*", "", line).strip()
        if line:
            lines.append(line)
        if sum(len(x) for x in lines) > limit:
            break
    text = " · ".join(lines)
    return text[: limit - 1] + "…" if len(text) > limit else text


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


def _term_body(term: str, definition: str, source: str = "") -> str:
    """The Term template, rendered.

    Three headings, in the order they get filled in: what it means, what it
    means in lj's own words, and where it was met. The middle one is not
    decoration — restating a definition in your own words is the difference
    between a term you have collected and a term you know (the generation
    effect; the same reason `explain` cards ask before they reveal), and a
    heading that is visibly empty is the cheapest possible prompt to do it.

    `## Seen in` holds the source rather than a frontmatter field because a
    term is usually met more than once, and the second and third sightings
    are what turn a definition into a meaning.
    """
    lines = [f"# {term}", "", "## Definition", ""]
    if definition:
        lines += [definition, ""]
    lines += ["## In my own words", "", "## Seen in", ""]
    if source:
        lines += [f"- {source}", ""]
    return "\n".join(lines)


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
    # The implementation intention goes first, above the schedule. It is the
    # largest effect in the habit literature (Gollwitzer 1999, d≈0.65) and the
    # target count is the smallest; the note should read in that order.
    intention = habitsmod.intention_sentence(note.habit, fallback=note.title)
    if intention:
        lines += [f"> **{intention}**", ""]
    if note.habit:
        extras = []
        if (note.habit.anchor or "").strip():
            extras.append(f"**Right after:** {note.habit.anchor.strip()}")
        if (note.habit.easier or "").strip():
            extras.append(f"**Made easier:** {note.habit.easier.strip()}")
        if (note.habit.harder or "").strip():
            extras.append(f"**Made harder:** {note.habit.harder.strip()}")
        if extras:
            lines += extras + [""]
    if sched:
        when = calevents._days_label(sched, note)
        state = "" if sched.enabled else "  ·  **paused**"
        target = note.habit.target_count if note.habit else 1
        lines += [
            f"**Recurring:** {when} · {sched.duration_minutes} min{state}",
            f"*Target {target}× per {cadence.value} — change it at the weekly schedule review.*",
            "",
        ]
    if note.habit and note.habit.log:
        miss = habitsmod.misses(note.habit)
        # Consecutive misses, never a streak. A streak counter turns the first
        # miss into the loss of a whole number, at exactly the moment Lally et
        # al. found nothing has gone wrong yet.
        lines += [f"*{miss.message}*", ""]
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


def _parse_moment(value: Any) -> Optional[dt.datetime]:
    """An ISO moment from a browser or a log line, always timezone-aware."""
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            moment = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.astimezone()
    return moment
