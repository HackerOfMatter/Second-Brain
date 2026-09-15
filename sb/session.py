"""Note-taking sessions — a class, a chapter, and everything captured between.

The problem this exists for is filing, and it is a problem of *when* rather
than of *where*. During a lecture the destination of every note is the same
and is completely obvious: this class, this chapter. Afterwards it is neither
— it is forty minutes of triage over a pile of captures that have lost the one
piece of context that made them easy to file, which is that they all arrived
together while one specific thing was being explained.

So the destination is declared once, at the start, and every capture until the
session is closed inherits it. Nothing about a note changes: it is still a
Resource, still classified, still linked, still carded. It simply lands in
`30-Resources/Federal Taxation/Ch 4` instead of in a pile.

Three decisions worth writing down.

**The course folder is matched, not created on sight.** Typing "fed tax" at
the start of a lecture must find the `Federal Taxation` folder that already
holds forty notes, because the failure mode of the obvious implementation —
`mkdir` whatever was typed — is a vault that slowly accumulates `Fed Tax`,
`fed tax`, `Federal Tax` and `Federal Taxation`, each holding a fraction of
the subject, and none of them wrong enough to notice. `resolve_folder` tries
exact, prefix, every-word and initials before it agrees to make a new one, and
it says which it did, because silently filing into the wrong existing folder
is the one outcome worse than a duplicate.

**State lives in `_system/`, which is disposable, and that is correct.** The
session is not a record of anything — the notes are the record. It holds
"which folder am I filing into right now", and losing it costs the routing of
the notes not yet taken, which is a thing you fix by starting the session
again. Nothing durable is kept here, so nothing durable can be lost here.

**The summary is written at the end and not before.** It lists what was
actually captured, with the terms separated out and the undefined ones marked,
because that list is the honest answer to "what happened in that hour" and it
is available at no cost — the session already knows every id. A summary
written from the session's own record cannot claim a note that was never
taken.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

STATE_FILE = "session.json"

#: Everything Windows forbids in a folder name, plus the two Obsidian treats
#: as link syntax. A chapter is typed in a hurry, mid-lecture.
_UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]]')


def clean_folder_name(name: str) -> str:
    """A folder name safe to create, with the typed name otherwise intact.

    Deliberately not a slug. `Ch 4 — Gross Income` is what lj typed and what
    lj will look for in Obsidian's file tree; turning it into `ch-4-gross-income`
    would be tidier and would mean the folder no longer answers to the name it
    was given.
    """
    # A space, not nothing: a character we drop is usually standing between
    # two words, and deleting it welds them together — `Gross [Income]/x`
    # would come out as `Gross Incomex`.
    out = _UNSAFE.sub(" ", str(name or "")).strip().strip(".")
    return re.sub(r"\s+", " ", out).strip()[:80]


def _tokens(text: str) -> List[str]:
    return [t for t in re.split(r"[^\w]+", str(text or "").lower()) if t]


def resolve_folder(existing: List[str], asked: str) -> Tuple[str, str]:
    """(folder to use, how it was decided) for a course or chapter name.

    Four ways to recognise a folder lj already has, tried in order of how
    sure each one is, and only then a new one:

      exact    — the same name, ignoring case
      prefix   — what was typed is the start of exactly one folder
      words    — every word typed appears in exactly one folder
      initials — every word typed starts a consecutive word of exactly one
                 folder, which is what makes "fed tax" find "Federal Taxation"

    "exactly one" is load-bearing in the last three. An ambiguous match is a
    coin flip between two subjects' worth of notes, so it is treated as no
    match at all and a new folder is made under the name as typed — a visible
    duplicate lj can merge beats a silent misfiling nobody ever sees.
    """
    asked = clean_folder_name(asked)
    if not asked:
        return "", "empty"
    low = asked.lower()

    for folder in existing:
        if folder.lower() == low:
            return folder, "exact"

    def only(matches: List[str]) -> Optional[str]:
        return matches[0] if len(matches) == 1 else None

    hit = only([f for f in existing if f.lower().startswith(low)])
    if hit:
        return hit, "prefix"

    want = _tokens(asked)
    hit = only([f for f in existing if all(w in _tokens(f) for w in want)])
    if hit:
        return hit, "words"

    def initials(folder: str) -> bool:
        words = _tokens(folder)
        if len(want) > len(words):
            return False
        for start in range(len(words) - len(want) + 1):
            if all(words[start + i].startswith(w) for i, w in enumerate(want)):
                return True
        return False

    hit = only([f for f in existing if initials(f)])
    if hit:
        return hit, "initials"

    return asked, "new"


@dataclass
class Session:
    """One sitting. Ids rather than notes: the vault is the truth, and a
    session that cached note bodies would be a second copy of them going
    stale from the moment it was written."""

    id: str
    course: str
    chapter: str
    started: str
    note_ids: List[str] = field(default_factory=list)
    term_ids: List[str] = field(default_factory=list)
    ended: str = ""
    summary_id: str = ""

    @property
    def folder(self) -> str:
        """Where captures go, relative to whichever bucket they land in."""
        return "/".join(p for p in (self.course, self.chapter) if p)

    def add(self, note_id: str, *, term: bool = False) -> None:
        if not note_id:
            return
        if note_id not in self.note_ids:
            self.note_ids.append(note_id)
        if term and note_id not in self.term_ids:
            self.term_ids.append(note_id)

    def as_dict(self) -> Dict[str, Any]:
        return {**asdict(self), "folder": self.folder,
                "notes": len(self.note_ids), "terms": len(self.term_ids)}


class SessionStore:
    """The open session, or nothing. At most one at a time, on purpose.

    Two open sessions would mean every capture needs to say which one it
    belongs to, and the entire value here is that a capture during a lecture
    needs to say nothing at all.
    """

    def __init__(self, system_dir: Path):
        self.path = Path(system_dir) / STATE_FILE

    def open(self) -> Optional[Session]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(raw, dict) or raw.get("ended"):
            return None
        try:
            return Session(
                id=str(raw["id"]),
                course=str(raw.get("course") or ""),
                chapter=str(raw.get("chapter") or ""),
                started=str(raw.get("started") or ""),
                note_ids=[str(x) for x in raw.get("note_ids") or []],
                term_ids=[str(x) for x in raw.get("term_ids") or []],
            )
        except KeyError:
            # A state file we cannot read is a state file we do not have. It
            # must never be the reason a capture fails.
            return None

    def save(self, session: Session) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(session.as_dict(), indent=2), encoding="utf-8"
        )

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


def new_id(course: str, chapter: str, at: Optional[dt.datetime] = None) -> str:
    stamp = (at or dt.datetime.now()).strftime("%Y%m%dT%H%M%S")
    tail = re.sub(r"[^\w]+", "-", f"{course} {chapter}".strip().lower()).strip("-")
    return f"{stamp}-session-{tail}"[:80] if tail else f"{stamp}-session"


def summary_sections(
    session: Session,
    notes: List[Dict[str, Any]],
    terms: List[Dict[str, Any]],
    ended: dt.datetime,
) -> Dict[str, str]:
    """What happened in that hour, as content per heading.

    Returns sections rather than a finished body, because `_templates/Session.md`
    decides which headings exist and in what order — this only knows what goes
    inside them. Before that was true, this function rendered the whole note and
    a session summary was the one note in the vault whose shape lived in Python
    instead of in a template lj could edit.

    The terms are kept apart from the notes, and the undefined ones apart again,
    because those are answers to different questions. The notes are what was
    captured. The undefined terms are the homework the session generated — the
    words that were met and not understood — and that list is worth nothing if
    it is buried in the middle of the other one.
    """
    started = session.started or ended.isoformat(timespec="seconds")
    try:
        minutes = max(
            0, int((ended - dt.datetime.fromisoformat(started)).total_seconds() // 60)
        )
    except (TypeError, ValueError):
        minutes = 0

    undefined = [t for t in terms if t.get("needs_definition")]
    term_ids = {t["id"] for t in terms}
    plain = [n for n in notes if n["id"] not in term_ids]

    sections = {
        "Look these up": "\n".join(f"- [ ] [[{t['title']}]]" for t in undefined),
        "Terms": "\n".join(
            f"- [[{t['title']}]]" + ("" if not t.get("needs_definition")
                                     else "  — no definition yet")
            for t in terms
        ),
        "Notes": "\n".join(f"- [[{n['title']}]]" for n in plain)
                 or "*Nothing was captured in this session.*",
    }
    # The counts only. The title already carries the course, the chapter and
    # the date, and a summary that opens by repeating its own title is a
    # summary nobody reads past.
    sections["__preamble__"] = (
        f"*{len(notes)} notes · {len(terms)} terms · {minutes} minutes*"
    )
    return sections
