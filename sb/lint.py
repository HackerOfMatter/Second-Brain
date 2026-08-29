"""Are these notes actually atomic?

Phase 6's title-matching tier — the one that links notes for free, with no
embedding and no model call — works *because* notes are atomic and precisely
named. Reading a note and finding the literal string "Opportunity Cost" in it
is a reliable signal that the two are related only when there is exactly one
note called Opportunity Cost and it is about exactly that. That property is
currently an accident of how lj happens to write. Nothing enforces it, and the
day it stops holding, linking degrades silently — no error, just worse
suggestions, which is the hardest kind of regression to notice.

Ahrens (*How to Take Smart Notes*) and Luhmann's Zettelkasten give the rule:
**one idea per note, and links instead of folders.** The reason is mechanical
rather than aesthetic. A note holding three ideas can only be linked as a
unit, so the third idea is invisible to everything that finds notes by what
they are about — including this system's own retrieval, its card generator,
and Obsidian's graph.

## What is checked, and what is not

Every check here is a *smell*, not a rule, and the output is a report rather
than a refusal. Nothing is rewritten and nothing is blocked: lj's vault is
lj's, and a lint that edits your notes is a lint you turn off.

  * **multiple ideas** — several top-level sections, each with real prose.
  * **too long to be one idea** — a word count far past what a single claim
    needs. Deliberately generous; the failure mode of a strict limit is that
    every note trips it and the report becomes wallpaper.
  * **a vague title** — "Notes", "Chapter 3", "Untitled", a bare date. A note
    whose title is not the idea cannot be linked by title, which is exactly
    the tier that costs nothing.
  * **an orphan** — no links out and none in. Luhmann's actual claim is that
    an unlinked note is a lost note.
  * **a duplicate title** — two notes with the same name make every
    `[[link]]` between them ambiguous, and title-matching picks one at random.

Buckets that are not meant to be atomic are skipped: an Area is an ongoing
responsibility and a Project is a plan, and neither is a claim about the world.
Resources are where the rule applies.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Sequence

from .models import Bucket, Note, slugify

#: Where atomicity is a real expectation. Projects and Areas are plans and
#: responsibilities; asking them to hold one idea is a category error.
LINTED = (Bucket.RESOURCE,)

#: Generous on purpose. A note past this is not necessarily wrong — a genuinely
#: long single argument exists — but it is worth a second look.
LONG_WORDS = 500

#: Sections the system writes into a note itself; they are not lj's ideas and
#: must not count towards "this note holds several".
OWN_HEADINGS = {
    "related", "capture", "steps", "materials", "skills", "hardware",
    "software", "check-in log", "cards", "source",
}

VAGUE_TITLES = {
    "notes", "note", "untitled", "misc", "miscellaneous", "stuff", "ideas",
    "todo", "inbox", "reading", "summary", "thoughts", "scratch", "temp",
}

_VAGUE_SHAPE = re.compile(
    r"^(?:chapter|lecture|week|day|part|session|class|page)\s*\d+\s*$|^\d{1,4}[-/.]\d{1,2}([-/.]\d{1,4})?$",
    re.I,
)

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*$", re.M)
_WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")


@dataclass
class Finding:
    note_id: str
    title: str
    rule: str
    detail: str

    def as_dict(self) -> Dict[str, Any]:
        return {"note_id": self.note_id, "title": self.title, "rule": self.rule, "detail": self.detail}


@dataclass
class Report:
    checked: int = 0
    findings: List[Finding] = field(default_factory=list)

    @property
    def by_rule(self) -> Dict[str, int]:
        return dict(Counter(f.rule for f in self.findings))

    def as_dict(self) -> Dict[str, Any]:
        return {
            "checked": self.checked,
            "findings": [f.as_dict() for f in self.findings],
            "by_rule": self.by_rule,
            "clean": len(self.findings) == 0,
            "message": self.message,
        }

    @property
    def message(self) -> str:
        if not self.checked:
            return "No Resources to check yet."
        if not self.findings:
            return f"{self.checked} notes, all atomic. Title-matching links will keep working."
        return (
            f"{len(self.findings)} smell{'s' if len(self.findings) != 1 else ''} "
            f"across {self.checked} notes. None of it is an error — it is where "
            "linking by title will start missing things."
        )


def own_sections(body: str) -> List[str]:
    """Headings lj wrote, excluding the ones this system renders."""
    out: List[str] = []
    for match in _HEADING.finditer(body or ""):
        name = match.group(2).strip().lower().rstrip(":")
        if name and name not in OWN_HEADINGS:
            out.append(match.group(2).strip())
    return out


def words(body: str) -> int:
    stripped = re.sub(r"^---.*?^---", "", body or "", flags=re.S | re.M)
    stripped = re.sub(r"```.*?```", "", stripped, flags=re.S)
    return len(stripped.split())


def links_out(body: str) -> List[str]:
    return [m.group(1).strip() for m in _WIKILINK.finditer(body or "")]


def is_vague(title: str) -> bool:
    stripped = (title or "").strip().lower().rstrip(".")
    if not stripped or stripped in VAGUE_TITLES:
        return True
    if _VAGUE_SHAPE.match(stripped):
        return True
    # A title that is only a stopword-ish fragment cannot identify an idea.
    return len(stripped) < 4


def check(notes: Sequence[Note], *, buckets: Iterable[Bucket] = LINTED) -> Report:
    """Lint the vault. Reports; never edits."""
    wanted = tuple(buckets)
    subjects = [n for n in notes if n.bucket in wanted]
    report = Report(checked=len(subjects))

    inbound: Dict[str, int] = defaultdict(int)
    by_title: Dict[str, List[Note]] = defaultdict(list)
    for note in notes:
        by_title[slugify(note.title)].append(note)
        for title in links_out(note.body):
            inbound[slugify(title)] += 1

    for note in subjects:
        key = slugify(note.title)
        sections = own_sections(note.body)
        if len(sections) >= 3:
            report.findings.append(Finding(
                note.id, note.title, "multiple-ideas",
                f"{len(sections)} sections of your own ({', '.join(sections[:3])}…). "
                "Split it and each half becomes linkable on its own.",
            ))
        count = words(note.body)
        if count > LONG_WORDS:
            report.findings.append(Finding(
                note.id, note.title, "too-long",
                f"{count} words. One idea rarely needs that much; a long note "
                "can only ever be linked as a whole.",
            ))
        if is_vague(note.title):
            report.findings.append(Finding(
                note.id, note.title, "vague-title",
                "A title that does not name the idea cannot be matched by "
                "title — the one linking tier that costs nothing.",
            ))
        # Own outbound links exclude the ## Related section this system writes,
        # otherwise connect.py would silently cure every orphan it touched.
        body_without_related = re.split(r"^##\s+Related\s*$", note.body or "", flags=re.M)[0]
        if not links_out(body_without_related) and not inbound.get(key):
            report.findings.append(Finding(
                note.id, note.title, "orphan",
                "Nothing links to it and it links to nothing. Luhmann's rule: "
                "an unlinked note is a lost note.",
            ))
        same = [n for n in by_title[key] if n.id != note.id]
        if same:
            report.findings.append(Finding(
                note.id, note.title, "duplicate-title",
                f"{len(same) + 1} notes share this name, so every [[link]] to "
                "it is ambiguous and title-matching picks one at random.",
            ))
    return report
