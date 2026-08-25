"""Decks, cards, and where they live.

A deck belongs to exactly one note — the Project you are learning or the
Resource you filed — and is stored beside the vault as one markdown file per
deck in `_decks/`. Underscore-prefixed so it sorts out of the way in Obsidian's
explorer, like `_system/` and `_templates/`, but unlike `_system/` it is **not
disposable**: a calendar can be regenerated from the notes, and a year of
review history cannot.

## The split inside a deck file

The rest of the vault puts machine truth in frontmatter and a human view in the
body. A deck cannot quite do that, because both halves are authoritative and
they are owned by different parties:

  * **Frontmatter owns scheduling.** Stability, difficulty, due date, reps,
    lapses, status. You should never hand-edit this, and there is no reason to
    want to.
  * **The body owns the card text.** Question, answer, the quote from the
    source that justifies the answer. This is yours. Rewrite a question in
    Obsidian, add a card by typing one, delete a card by deleting its block —
    all of it works, because the body is read as the list of cards that exist.

They are joined by the card id in the heading. A card in the body with no
frontmatter entry is new and starts unscheduled; a frontmatter entry whose card
has been deleted from the body is dropped on the next write. That rule is what
makes "edit your flashcards in your notes app" safe rather than a sync bug
waiting to happen — the two halves can never disagree about *which cards
exist*, only ever about state the human does not touch.

## Review history

`_decks/_reviews.jsonl` is append-only: one line per answer, recording the
pre-review state alongside the grade. It powers the streak and heatmap, and it
is also exactly the dataset an FSRS optimizer wants if lj ever accumulates
enough reviews to fit personal weights.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from . import frontmatter, fsrs
from .models import now as _now
from .models import slugify

DECK_DIR = "_decks"
REVIEW_LOG = "_reviews.jsonl"

#: One card block in a deck body. The id in the heading is the join key.
#: The trailing `.*` swallows the decorative status marker the renderer adds
#: ("· *awaiting review*"); only the id is load-bearing.
CARD_HEADING = re.compile(r"^###\s+Card\s+([A-Za-z0-9_-]+)\b.*$", re.M)
FIELD_LABEL = re.compile(r"^\*\*(Q|A|Hint|Why)\.\*\*[ \t]*", re.M)

LABEL_TO_FIELD = {"Q": "front", "A": "back", "Hint": "hint", "Why": "source"}
FIELD_TO_LABEL = {v: k for k, v in LABEL_TO_FIELD.items()}

CLOZE = re.compile(r"\{\{([^{}]+)\}\}")


# --------------------------------------------------------------------------
# models
# --------------------------------------------------------------------------


class Card(BaseModel):
    """One question. `status` is the gate the generator writes into.

    A model-drafted card lands as `draft` and is invisible to the scheduler
    until lj approves it. That is deliberate: a wrong flashcard is worse than
    no flashcard, because spaced repetition will patiently drill the error
    into you on an optimal schedule.
    """

    model_config = ConfigDict(extra="ignore")

    id: str
    front: str = ""
    back: str = ""
    hint: str = ""
    source: str = ""  # the line from the note that justifies the answer
    status: str = "draft"  # draft | active | suspended
    stability: float = 0.0
    difficulty: float = 0.0
    due: Optional[dt.date] = None
    last_review: Optional[dt.datetime] = None
    reps: int = 0
    lapses: int = 0

    # -- derived ------------------------------------------------------------

    @property
    def kind(self) -> str:
        return "cloze" if CLOZE.search(self.front or "") else "basic"

    @property
    def is_new(self) -> bool:
        return self.reps == 0 or self.stability <= 0

    @property
    def memory(self) -> fsrs.Memory:
        return fsrs.Memory(
            stability=self.stability,
            difficulty=self.difficulty,
            reps=self.reps,
            lapses=self.lapses,
        )

    def elapsed_days(self, at: Optional[dt.datetime] = None) -> float:
        if not self.last_review:
            return 0.0
        at = at or _now()
        last = self.last_review
        if last.tzinfo is None and at.tzinfo is not None:
            last = last.replace(tzinfo=at.tzinfo)
        return max(0.0, (at - last).total_seconds() / 86400.0)

    def retrievability(self, at: Optional[dt.datetime] = None) -> float:
        return fsrs.retrievability(self.stability, self.elapsed_days(at))

    def is_due(self, on: Optional[dt.date] = None) -> bool:
        if self.status != "active":
            return False
        if self.is_new or self.due is None:
            return False
        return self.due <= (on or dt.date.today())

    def apply(self, scheduled: fsrs.Scheduled) -> None:
        self.stability = round(scheduled.memory.stability, 4)
        self.difficulty = round(scheduled.memory.difficulty, 4)
        self.reps = scheduled.memory.reps
        self.lapses = scheduled.memory.lapses
        self.due = scheduled.due
        self.last_review = _now()

    # -- rendering ----------------------------------------------------------

    def question(self) -> str:
        """A cloze question with its blank hidden. Basic cards pass through."""
        if self.kind != "cloze":
            return self.front
        return CLOZE.sub(lambda m: "[ ... ]", self.front)

    def answer(self) -> str:
        """A cloze card's answer is its own sentence with the blank filled.

        The back is appended only when it adds something. A cloze generated
        from a definition sentence often has the blanked term as its back, and
        showing "X is the first step. / X" reads like a bug.
        """
        if self.kind != "cloze":
            return self.back
        filled = CLOZE.sub(lambda m: f"**{m.group(1)}**", self.front)
        extra = (self.back or "").strip()
        if not extra or extra.lower() in filled.lower():
            return filled
        return f"{filled}\n\n{extra}"

    def cloze_answers(self) -> List[str]:
        return [m.group(1).strip() for m in CLOZE.finditer(self.front or "")]

    def schedule_record(self) -> Dict[str, Any]:
        """The compact frontmatter row. Only fields the machine owns."""
        rec: Dict[str, Any] = {"id": self.id, "status": self.status}
        if self.stability:
            rec["s"] = round(self.stability, 4)
        if self.difficulty:
            rec["d"] = round(self.difficulty, 4)
        if self.due:
            rec["due"] = self.due.isoformat()
        if self.last_review:
            rec["last"] = self.last_review.isoformat()
        if self.reps:
            rec["reps"] = self.reps
        if self.lapses:
            rec["lapses"] = self.lapses
        return rec

    def load_schedule(self, rec: Dict[str, Any]) -> None:
        self.status = str(rec.get("status") or self.status)
        self.stability = float(rec.get("s") or 0.0)
        self.difficulty = float(rec.get("d") or 0.0)
        self.reps = int(rec.get("reps") or 0)
        self.lapses = int(rec.get("lapses") or 0)
        self.due = _as_date(rec.get("due"))
        self.last_review = _as_datetime(rec.get("last"))


class Deck(BaseModel):
    """Every card generated from one note."""

    model_config = ConfigDict(extra="allow")

    note_id: str
    subject: str = ""  # display name — the note's title
    bucket: str = "project"
    category: Optional[str] = None
    created: dt.datetime = Field(default_factory=_now)
    updated: dt.datetime = Field(default_factory=_now)
    cards: List[Card] = Field(default_factory=list)
    #: Fingerprint of the note text the cards were generated from, so the UI
    #: can say "the source has changed since these were made".
    source_fingerprint: str = ""

    # -- queries ------------------------------------------------------------

    def card(self, card_id: str) -> Card:
        for c in self.cards:
            if c.id == card_id:
                return c
        raise KeyError(f"no card {card_id!r} in deck {self.note_id!r}")

    @property
    def active(self) -> List[Card]:
        return [c for c in self.cards if c.status == "active"]

    @property
    def drafts(self) -> List[Card]:
        return [c for c in self.cards if c.status == "draft"]

    def due(self, on: Optional[dt.date] = None) -> List[Card]:
        return [c for c in self.active if c.is_due(on)]

    def new(self) -> List[Card]:
        return [c for c in self.active if c.is_new]

    def next_card_id(self) -> str:
        used = {c.id for c in self.cards}
        n = len(self.cards) + 1
        while f"c{n}" in used:
            n += 1
        return f"c{n}"

    def add(self, **fields: Any) -> Card:
        card = Card(id=fields.pop("id", None) or self.next_card_id(), **fields)
        self.cards.append(card)
        return card


# --------------------------------------------------------------------------
# serialization
# --------------------------------------------------------------------------


def render_body(deck: Deck) -> str:
    """The human-owned half of a deck file.

    Written so that adding a card by hand means copying the shape of the one
    above it — no syntax to learn beyond a heading and two bold labels.
    """
    lines = [
        f"# {deck.subject or deck.note_id}",
        "",
        "*Flashcards. Edit the text freely — questions, answers and the source "
        "quote live here. Scheduling lives in the frontmatter; leave that to "
        "the app.*",
        "",
        "*Add a card by copying a block below and giving it a new id. Delete a "
        "card by deleting its block. Wrap a phrase in `{{double braces}}` to "
        "make it a cloze deletion.*",
        "",
    ]
    for card in deck.cards:
        state = {
            "draft": "  ·  *awaiting review*",
            "suspended": "  ·  *suspended*",
        }.get(card.status, "")
        lines += [f"### Card {card.id}{state}", ""]
        lines += [f"**Q.** {card.front.strip()}", ""]
        lines += [f"**A.** {card.back.strip()}", ""]
        if card.hint.strip():
            lines += [f"**Hint.** {card.hint.strip()}", ""]
        if card.source.strip():
            lines += [f"**Why.** {card.source.strip()}", ""]
    return "\n".join(lines).rstrip() + "\n"


def parse_body(body: str) -> List[Card]:
    """Read the cards a deck body declares, in the order they appear."""
    cards: List[Card] = []
    matches = list(CARD_HEADING.finditer(body))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        fields = _parse_fields(body[start:end])
        if not fields.get("front"):
            continue  # a heading with no question is not a card
        cards.append(Card(id=m.group(1), **fields))
    return cards


def _parse_fields(chunk: str) -> Dict[str, str]:
    """Split one card block into its labelled fields.

    Each label owns everything up to the next label, so answers can run to
    several paragraphs, hold code fences, or contain blank lines.
    """
    out: Dict[str, str] = {}
    labels = list(FIELD_LABEL.finditer(chunk))
    for i, m in enumerate(labels):
        start = m.end()
        end = labels[i + 1].start() if i + 1 < len(labels) else len(chunk)
        field = LABEL_TO_FIELD[m.group(1)]
        out[field] = chunk[start:end].strip()
    return out


def dump(deck: Deck) -> str:
    meta: Dict[str, Any] = {
        "kind": "deck",
        "note_id": deck.note_id,
        "subject": deck.subject,
        "bucket": deck.bucket,
        "created": deck.created.isoformat(),
        "updated": _now().isoformat(),
        "cards": [c.schedule_record() for c in deck.cards],
    }
    if deck.category:
        meta["category"] = deck.category
    if deck.source_fingerprint:
        meta["source_fingerprint"] = deck.source_fingerprint
    return frontmatter.dump(meta, render_body(deck))


def loads(text: str) -> Deck:
    meta, body = frontmatter.parse(text)
    cards = parse_body(body)
    schedules = {
        str(rec.get("id")): rec
        for rec in (meta.get("cards") or [])
        if isinstance(rec, dict) and rec.get("id")
    }
    for card in cards:
        rec = schedules.get(card.id)
        if rec:
            card.load_schedule(rec)
        else:
            # Hand-written and never seen by the scheduler. It enters the deck
            # as a real card rather than a draft: you typed it, you meant it.
            card.status = card.status or "active"
            if card.status == "draft":
                card.status = "active"
    return Deck(
        note_id=str(meta.get("note_id") or ""),
        subject=str(meta.get("subject") or ""),
        bucket=str(meta.get("bucket") or "project"),
        category=meta.get("category"),
        created=_as_datetime(meta.get("created")) or _now(),
        updated=_as_datetime(meta.get("updated")) or _now(),
        source_fingerprint=str(meta.get("source_fingerprint") or ""),
        cards=cards,
    )


# --------------------------------------------------------------------------
# store
# --------------------------------------------------------------------------


class DeckStore:
    """Deck files on disk, one per note, plus the review log."""

    def __init__(self, vault_root: Path):
        self.root = Path(vault_root) / DECK_DIR

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        if not readme.exists():
            readme.write_text(_README, encoding="utf-8")

    # -- paths --------------------------------------------------------------

    def path_for(self, note_id: str, subject: str = "") -> Path:
        existing = self.find_path(note_id)
        if existing:
            return existing
        stem = slugify(subject or note_id, 40)
        return self.root / f"{stem}--{note_id[:15]}.md"

    def find_path(self, note_id: str) -> Optional[Path]:
        if not self.root.exists():
            return None
        for p in sorted(self.root.glob("*.md")):
            if p.name.startswith("_") or p.name == "README.md":
                continue
            try:
                meta, _ = frontmatter.parse(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if meta.get("note_id") == note_id:
                return p
        return None

    # -- read ---------------------------------------------------------------

    def get(self, note_id: str) -> Optional[Deck]:
        path = self.find_path(note_id)
        if not path:
            return None
        return loads(path.read_text(encoding="utf-8"))

    def all(self) -> List[Deck]:
        out: List[Deck] = []
        if not self.root.exists():
            return out
        for p in sorted(self.root.glob("*.md")):
            if p.name.startswith("_") or p.name == "README.md":
                continue
            try:
                deck = loads(p.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                continue
            if deck.note_id:
                out.append(deck)
        return out

    # -- write --------------------------------------------------------------

    def save(self, deck: Deck) -> Path:
        """Atomic, with a unique temp name — see `Vault.write` for why the
        temp name has to be unique. A deck is rewritten on every single answer,
        so two reviews graded a moment apart is the *normal* case here, not an
        edge one."""
        import os
        import uuid

        self.ensure()
        deck.updated = _now()
        target = self.path_for(deck.note_id, deck.subject)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(dump(deck), encoding="utf-8")
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        return target

    def delete(self, note_id: str) -> bool:
        path = self.find_path(note_id)
        if not path:
            return False
        path.unlink()
        return True

    # -- review log ---------------------------------------------------------

    @property
    def log_path(self) -> Path:
        return self.root / REVIEW_LOG

    def log_review(self, entry: Dict[str, Any]) -> None:
        self.ensure()
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, default=str) + "\n")

    def reviews(self, since: Optional[dt.date] = None) -> Iterator[Dict[str, Any]]:
        """Every logged answer, oldest first. A corrupt line is skipped rather
        than raising — a broken byte in a log should never break studying."""
        if not self.log_path.exists():
            return
        for line in self.log_path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if since:
                at = _as_datetime(rec.get("at"))
                if not at or at.date() < since:
                    continue
            yield rec


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _as_date(value: Any) -> Optional[dt.date]:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def _as_datetime(value: Any) -> Optional[dt.datetime]:
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, dt.date):
        return dt.datetime.combine(value, dt.time.min).astimezone()
    if isinstance(value, str) and value.strip():
        try:
            return dt.datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    return None


def fingerprint(text: str) -> str:
    import hashlib

    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:16]


_README = """# _decks

Flashcard decks — one file per note, plus `_reviews.jsonl`.

Unlike `_system/`, **this folder is not disposable.** It holds your review
history, which cannot be rebuilt from anything else. Back it up; don't delete
it to "reset" something.

Each deck file has two halves:

* **Frontmatter** — scheduling state (stability, difficulty, due date). The
  app owns this. Editing it by hand will confuse the scheduler.
* **Body** — the cards themselves. You own this. Rewrite a question, fix a
  wrong answer, add a card by copying a block and giving it a new id, delete a
  card by deleting its block.

Add this folder to Obsidian's excluded files if you don't want cards showing up
in search results.
"""
