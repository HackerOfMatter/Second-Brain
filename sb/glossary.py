"""A pasted glossary: several `term: definition` lines, one term note each.

Terms arrive in batches — the key-terms box at the end of a chapter, the
vocabulary slide, a Quizlet export, ten words typed during a lecture — and
the term capture took them one at a time: hotkey, Tab, term, Enter,
definition, Enter, and again. This module recognises the batch so every
capture surface can file it in one go, each line becoming the same Term note
and written card that `Engine.capture_term` makes.

Accepted line shapes, with or without a bullet or number in front:

    Elasticity: how much demand responds to price
    Elasticity :: how much demand responds to price
    **Elasticity** — how much demand responds to price
    Elasticity - how much demand responds to price
    Elasticity = how much demand responds to price
    Elasticity<TAB>how much demand responds to price        (spreadsheet/Quizlet)

A `# heading` inside the paste becomes the "Seen in" of the terms under it.
An indented line, or one that starts in lower case, continues the previous
definition.

**Recognising is the risky half.** "Speaker: Dr Lee / Room: 204" is also a
list of colon lines, and filing a meeting note as two term cards is a bad
surprise. So:

  * label-like words ("due", "room", "speaker", …) are never terms;
  * a term is at most six words and sixty characters, a definition at most
    eighty words;
  * automatic routing needs two or more entries covering at least 75% of the
    content lines — or the explicit `::`, which nothing else uses;
  * the dashboard preview, where lj sees the result before anything is
    written, uses a lower bar (60%).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .guide import _LABEL_WORDS

MAX_TERM_WORDS = 6
MAX_TERM_CHARS = 60
MAX_DEFINITION_WORDS = 80
AUTO_SHARE = 0.75
#: An unindented lower-case line this long continues the definition above it
#: (a line wrapped by a PDF); a shorter one ("buy milk") is its own line.
MIN_WRAP_WORDS = 4
PREVIEW_SHARE = 0.6

#: Colon-line labels from notes that are not glossaries.
NOT_TERMS = set(_LABEL_WORDS) | {
    "todo", "to do", "tip", "reminder", "warning", "important", "question",
    "answer", "deadline", "date", "time", "when", "where", "who", "what",
    "why", "how", "speaker", "speakers", "topic", "location", "room", "place",
    "attendees", "agenda", "subject", "re", "cc", "to", "from", "email",
    "phone", "page", "pages", "chapter", "section", "unit", "module", "lecture",
    "professor", "teacher", "class", "priority", "estimate", "level", "steps",
    "materials", "hardware", "software", "skills", "goal", "goals", "result",
    "results", "status", "owner", "project", "area", "resource", "inbox",
    "http", "https", "ftp", "mailto", "file", "obsidian", "ps", "nb", "fyi",
}

_BULLET = r"(?:[-*+•]\s+|\d{1,3}[.)]\s+)?"
_TERM = r"(?P<term>[^\t:=—–\n]{1,80}?)"
_LINE = re.compile(
    r"^\s*" + _BULLET + _TERM +
    r"\s*(?P<sep>::|\t+|:|=|\s[—–-]\s|[—–])\s*(?P<definition>\S.*?)\s*$"
)
_HEADING = re.compile(r"^\s*#{1,6}\s+(?P<text>.+?)\s*#*\s*$")
_MARKUP = re.compile(r"^[*_`\"'“”]+|[*_`\"'“”]+$")


@dataclass
class Entry:
    term: str
    definition: str
    source: str = ""
    line: int = 0
    explicit: bool = False  # written with `::` or a tab

    def as_dict(self) -> Dict[str, object]:
        return {"term": self.term, "definition": self.definition,
                "source": self.source, "line": self.line}


@dataclass
class Glossary:
    entries: List[Entry] = field(default_factory=list)
    #: Content lines that were neither an entry, a heading, nor a
    #: continuation — shown in the preview so nothing vanishes silently.
    leftover: List[str] = field(default_factory=list)
    duplicates: List[str] = field(default_factory=list)
    content_lines: int = 0
    entry_lines: int = 0

    @property
    def share(self) -> float:
        return self.entry_lines / self.content_lines if self.content_lines else 0.0

    @property
    def explicit(self) -> bool:
        return any(e.explicit for e in self.entries)

    def as_dict(self) -> Dict[str, object]:
        return {
            "entries": [e.as_dict() for e in self.entries],
            "leftover": self.leftover[:20],
            "duplicates": self.duplicates,
            "share": round(self.share, 3),
        }


def _clean(text: str) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    prev = None
    while prev != text:
        prev = text
        text = _MARKUP.sub("", text).strip()
    return text


def _is_term(term: str) -> bool:
    if not term or len(term) > MAX_TERM_CHARS or len(term.split()) > MAX_TERM_WORDS:
        return False
    low = term.lower().strip(" .")
    if low in NOT_TERMS or re.fullmatch(r"[\d\s.,/]+", low):
        return False
    if term.endswith("?") or not re.match(r"[\w(“\"'']", term):
        return False
    if re.search(r"https?|www\.", low):
        return False
    return True


def parse_line(line: str) -> Optional[Entry]:
    m = _LINE.match(line)
    if not m:
        return None
    term = _clean(m.group("term"))
    definition = _clean(m.group("definition"))
    sep = m.group("sep")
    if not _is_term(term):
        return None
    if definition.startswith("//"):  # a URL, split at its scheme
        return None
    if len(definition) < 2 or len(definition.split()) > MAX_DEFINITION_WORDS:
        return None
    if re.fullmatch(r"\d{1,2}(:\d{2})?\s*(am|pm)?", definition, re.I) and sep == ":":
        return None  # "Starts: 10:30"
    return Entry(term=term, definition=definition,
                 explicit=(sep == "::" or sep.startswith("\t")))


def parse(text: str, source: str = "") -> Glossary:
    out = Glossary()
    heading = ""
    seen: Dict[str, Entry] = {}
    last: Optional[Entry] = None
    for number, raw in enumerate((text or "").splitlines(), start=1):
        if not raw.strip():
            last = None
            continue
        h = _HEADING.match(raw)
        if h:
            heading = _clean(h.group("text"))
            last = None
            continue
        entry = parse_line(raw)
        if entry is None and last is not None and (
            raw[:1] in (" ", "\t")
            or (raw.strip()[:1].islower() and len(raw.split()) >= MIN_WRAP_WORDS)
        ):
            # A definition that wrapped onto the next line.
            last.definition = f"{last.definition} {_clean(raw)}".strip()
            continue
        out.content_lines += 1
        if entry is None:
            out.leftover.append(raw.strip())
            last = None
            continue
        out.entry_lines += 1
        entry.source = heading or source
        entry.line = number
        key = entry.term.lower()
        if key in seen:
            out.duplicates.append(entry.term)
            last = None
            continue
        seen[key] = entry
        out.entries.append(entry)
        last = entry
    return out


def detect(text: str, *, share: float = AUTO_SHARE, source: str = "") -> Optional[Glossary]:
    """The glossary in `text`, or None when it is not one.

    Two or more entries covering `share` of the content lines, or a single
    explicit `term :: definition` line.
    """
    g = parse(text, source)
    if not g.entries:
        return None
    if len(g.entries) == 1:
        return g if g.explicit and g.content_lines == 1 else None
    return g if g.share >= share else None
