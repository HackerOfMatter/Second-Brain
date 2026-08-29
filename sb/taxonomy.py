"""Keyword → category → colour.

One table drives every colour decision in the system: the .ics `COLOR:`
property, the Google Calendar `colorId`, the emoji that prefixes a task title,
and the swatch in the dashboard. Adding a category means editing this file (or
the `calendar.categories` block in config.yaml) and nothing else.

Why an emoji *and* a colour: Google Calendar can colour an event, but Google
Tasks has no colour API at all — every task renders in one colour. So a task's
category has to survive in the only field Tasks gives us, which is the title.
The emoji is that channel; the colour is the one events get for free.

Matching order, strongest first:
  1. an explicit `category:` in the note's frontmatter — always wins
  2. a tag that names a category or one of its keywords
  3. keywords in the title
  4. keywords in the body

Anything unmatched lands in `general`, which is a real category with a real
colour rather than "no colour" — an uncoloured item on a colour-coded calendar
reads as a bug.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Tuple

from pydantic import BaseModel, Field

# Weight per match site. Tags are a deliberate act, so one tag outranks any
# number of incidental body words.
W_TAG = 100
W_TITLE = 10
W_BODY = 1


class Category(BaseModel):
    """A colour bucket. `color` is a CSS3 colour name because RFC 7986's
    COLOR property requires one; `hex` is the same colour for the web UI, and
    `google_color_id` is Google Calendar's own 1–11 palette index."""

    key: str
    label: str
    emoji: str
    color: str
    hex: str
    google_color_id: str
    keywords: List[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# the built-in table
# --------------------------------------------------------------------------
# Order is the tie-breaker: when two categories score equally, the one listed
# first wins. Specific-and-time-critical (quiz, hw) sits above general-purpose
# (study, work) on purpose — "study for the quiz" is a quiz.

BUILTIN: List[Category] = [
    Category(
        key="quiz",
        label="Quiz / exam",
        emoji="📝",
        color="crimson",
        hex="#d50000",
        google_color_id="11",
        keywords=[
            "quiz", "exam", "midterm", "final", "finals", "test", "testing",
            "assessment", "practical", "oral", "defense", "presentation",
            "proctored", "blue book",
        ],
    ),
    Category(
        key="hw",
        label="Homework",
        emoji="✏️",
        color="royalblue",
        hex="#3f51b5",
        google_color_id="9",
        keywords=[
            "hw", "homework", "assignment", "assignments", "problem set",
            "pset", "p-set", "worksheet", "essay", "paper", "report",
            "lab report", "lab write-up", "submit", "turn in", "canvas",
            "blackboard", "deliverable", "draft",
            # deliberately NOT "due": every rendered Project body contains the
            # word, so it matches the system's own boilerplate rather than
            # anything lj wrote, and quietly paints the whole vault blue.
        ],
    ),
    Category(
        key="study",
        label="Study",
        emoji="📘",
        color="dodgerblue",
        hex="#039be5",
        google_color_id="7",
        keywords=[
            "study", "studying", "read", "reading", "revise", "revision",
            "review notes", "notes", "lecture", "lectures", "learn",
            "learning", "memorize", "flashcard", "flashcards", "anki",
            "practice", "tutorial", "course", "chapter", "textbook",
            "research", "outline",
        ],
    ),
    Category(
        key="health",
        label="Health",
        emoji="💚",
        color="seagreen",
        hex="#0b8043",
        google_color_id="10",
        keywords=[
            "health", "gym", "workout", "exercise", "lift", "lifting",
            "run", "running", "jog", "walk", "walking", "swim", "bike",
            "cycling", "yoga", "stretch", "stretching", "meditate",
            "meditation", "doctor", "dentist", "dentist appointment",
            "therapy", "therapist", "meds", "medication", "vitamins",
            "sleep", "hydrate", "water", "physio", "recovery",
            # not "steps": every rendered Project body has a "## Steps" heading
        ],
    ),
    Category(
        key="chore",
        label="Chore",
        emoji="🧹",
        color="dimgray",
        hex="#616161",
        google_color_id="8",
        keywords=[
            "chore", "chores", "clean", "cleaning", "tidy", "laundry",
            "dishes", "trash", "garbage", "recycling", "vacuum", "mop",
            "groceries", "grocery", "shopping", "errand", "errands",
            "dry cleaning", "car wash", "oil change", "mow", "yard",
            "pack", "packing", "unpack", "fold",
            "dust", "organize", "declutter", "meal prep",
        ],
    ),
    Category(
        key="fun",
        label="Fun",
        emoji="🎉",
        color="gold",
        hex="#f6bf26",
        google_color_id="5",
        keywords=[
            "fun", "game", "games", "gaming", "movie", "movies", "show",
            "series", "party", "hang", "hangout", "concert", "gig",
            "festival", "trip", "vacation", "play", "relax", "hobby",
            "chill", "beach", "hike", "camping", "bowling", "arcade",
        ],
    ),
    Category(
        key="social",
        label="Social",
        emoji="👥",
        color="darkorchid",
        hex="#8e24aa",
        google_color_id="3",
        keywords=[
            "call", "phone call", "text", "birthday", "anniversary",
            "family", "dinner with", "lunch with", "coffee with",
            "catch up", "visit", "wedding", "reunion", "friends",
            # not bare "date": an Area body says "No due date", and a calendar
            # system's own prose is full of the word
        ],
    ),
    Category(
        key="work",
        label="Work",
        emoji="💼",
        color="orangered",
        hex="#f4511e",
        google_color_id="6",
        keywords=[
            "work", "job", "shift", "client", "meeting", "standup",
            "stand-up", "1:1", "one on one", "email", "emails", "invoice",
            "deploy", "ship", "sprint", "ticket", "pr review", "interview",
            "resume", "cover letter", "internship",
        ],
    ),
    Category(
        key="admin",
        label="Admin",
        emoji="📋",
        color="salmon",
        hex="#e67c73",
        google_color_id="4",
        keywords=[
            "admin", "form", "forms", "paperwork", "register",
            "registration", "enroll", "enrollment", "apply", "application",
            "renew", "renewal", "license", "passport", "insurance",
            "taxes", "tax", "fafsa", "transcript", "appointment",
            "schedule a", "book a", "cancel",
        ],
    ),
    Category(
        key="finance",
        label="Finance",
        emoji="💰",
        color="mediumseagreen",
        hex="#33b679",
        google_color_id="2",
        keywords=[
            "finance", "budget", "money", "savings", "save", "invest",
            "investment", "bank", "rent", "bill", "bills", "pay",
            "payment", "loan", "tuition", "subscription", "refund",
        ],
    ),
    Category(
        key="general",
        label="General",
        emoji="•",
        color="slateblue",
        hex="#7986cb",
        google_color_id="1",
        keywords=[],
    ),
]

FALLBACK = "general"


# --------------------------------------------------------------------------
# the table, after config overrides
# --------------------------------------------------------------------------


def _overrides(cfg) -> Dict[str, Dict[str, Any]]:
    if cfg is None:
        return {}
    return dict(getattr(getattr(cfg, "calendar", None), "categories", {}) or {})


def _token(overrides: Dict[str, Dict[str, Any]]) -> str:
    """A hashable stand-in for one config's category overrides.

    The table is rebuilt from `BUILTIN` on every call to `table()`, `index()`,
    `get()` and `detect()` — pydantic `model_dump` plus a sort, for a result
    that only changes when config does. This token keys the cache. Config is
    read once at start-up and never mutated, so the cache is unbounded but
    holds one entry in practice.
    """
    if not overrides:
        return ""
    try:
        return json.dumps(overrides, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return repr(sorted(overrides.items(), key=lambda kv: kv[0]))


def table(cfg=None) -> List[Category]:
    """The built-in categories with `calendar.categories` folded in.

    A config entry for an existing key patches that category — its `keywords`
    are *added* to the built-in list unless `replace_keywords: true`. An entry
    for an unknown key defines a new category, and must supply enough to be
    drawable (emoji, color, hex, google_color_id all have defaults, so in
    practice `keywords` alone is enough).

    Returns a fresh list so a caller can sort or filter it, over cached
    `Category` objects, which are treated as immutable everywhere they are used.
    """
    return list(_table(_register(cfg)))


def _register(cfg) -> str:
    """Token for this config's overrides, remembering them so every cache
    below can be keyed on the token alone."""
    ov = _overrides(cfg)
    token = _token(ov)
    _BY_TOKEN.setdefault(token, ov)
    return token


def _table(token: str) -> List[Category]:
    cached = _TABLE_CACHE.get(token)
    if cached is None:
        cached = _TABLE_CACHE[token] = _build_table(_BY_TOKEN.get(token, {}))
    return cached


def _build_table(overrides: Dict[str, Dict[str, Any]]) -> List[Category]:
    out: List[Category] = []
    seen = set()
    for base in BUILTIN:
        patch = overrides.get(base.key)
        if not patch:
            out.append(base)
            seen.add(base.key)
            continue
        data = base.model_dump()
        extra_kw = list(patch.get("keywords") or [])
        for field in ("label", "emoji", "color", "hex", "google_color_id"):
            if patch.get(field):
                data[field] = patch[field]
        if patch.get("replace_keywords"):
            data["keywords"] = extra_kw
        else:
            data["keywords"] = base.keywords + [k for k in extra_kw if k not in base.keywords]
        out.append(Category(**data))
        seen.add(base.key)

    for key, patch in overrides.items():
        if key in seen:
            continue
        out.append(
            Category(
                key=key,
                label=patch.get("label") or key.replace("_", " ").title(),
                emoji=patch.get("emoji") or "•",
                color=patch.get("color") or "slateblue",
                hex=patch.get("hex") or "#7986cb",
                google_color_id=str(patch.get("google_color_id") or "1"),
                keywords=list(patch.get("keywords") or [key]),
            )
        )
    # keep `general` last so it never wins a tie against a real category
    out.sort(key=lambda c: c.key == FALLBACK)
    return out


def index(cfg=None) -> Dict[str, Category]:
    return dict(_index(_register(cfg)))


def _index(token: str) -> Dict[str, Category]:
    cached = _INDEX_CACHE.get(token)
    if cached is None:
        cached = _INDEX_CACHE[token] = {c.key: c for c in _table(token)}
    return cached


def get(key: Optional[str], cfg=None) -> Category:
    """Look up a category, falling back to `general` for unknown keys."""
    idx = _index(_register(cfg))
    if key and key in idx:
        return idx[key]
    return idx.get(FALLBACK, BUILTIN[-1])


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


#: Characters that may stand between the words of a keyword. ":" is here for
#: "1:1"; "." deliberately is not, or "…in the lab. Report your findings" would
#: match the keyword "lab report" across a sentence boundary.
_SEP = r"[\s\-_/:]"


def _pattern(keyword: str) -> re.Pattern:
    """Word-boundary match, tolerant of the separators a keyword might appear
    with: 'problem set' matches 'problem-set' and 'problem  set'.

    Two *numbers* are the exception: the separator is required between them.
    Allowing zero meant the keyword "1:1" never matched an actual "1:1" (":"
    was not a separator) while matching every bare "11" — so any note whose
    rendered body carried a due date ending in 11 was silently filed as work
    and painted with the work colour on the calendar. "problemset" is a real
    spelling of "problem set"; "11" is not a spelling of "1:1".
    """
    parts = _WORD.findall(keyword.lower())
    if not parts:
        return re.compile(r"(?!)")
    body = re.escape(parts[0])
    for prev, part in zip(parts, parts[1:]):
        numeric = prev[-1].isdigit() and part[0].isdigit()
        body += _SEP + ("+" if numeric else "*") + re.escape(part)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)


_CACHE: Dict[str, re.Pattern] = {}


def _match(keyword: str, text: str) -> bool:
    pat = _CACHE.get(keyword)
    if pat is None:
        pat = _CACHE[keyword] = _pattern(keyword)
    return bool(pat.search(text))


# --------------------------------------------------------------------------
# the scan
# --------------------------------------------------------------------------
#
# Scoring used to run one regex per (keyword, scope): 11 categories x ~218
# keywords x 3 scopes is ~650 `re.search` calls per note, a third of them over
# 4,000 characters of body. One dashboard render categorises ~470 notes, so a
# 700-note vault spent 1.5 million regex scans — 97% of the render — deciding
# what colour things are.
#
# The observation that removes almost all of it: `_pattern` is a word-boundary
# match, and 194 of the 218 keywords are a single word. For those, "does this
# keyword match?" is exactly "is this word one of the text's words?", because
# `_WORD` splits on the same character class the pattern's lookarounds guard.
# So each scope is tokenised once into a set, shared by every category, and a
# single-word keyword becomes one set lookup.
#
# Only multi-word keywords ("problem set", "lab write-up") still need a regex,
# because the separator is optional and "problemset" has to match too. A
# two-word keyword gets an exact prefilter first — the regex can only match if
# both words are present as words, or the two fused into one — which skips the
# scan for all but the handful of notes that could actually match. Three-word
# keywords can fuse partially, so they are left to the regex.
#
# Same inputs, same score, same winner; the regex is still what decides every
# multi-word match.


class _Scan:
    """One category's keywords, split by how cheaply they can be tested."""

    __slots__ = ("key", "key_word", "key_pat", "singles", "pairs", "long")

    def __init__(self, cat: Category):
        self.key = cat.key
        # A category key is normally one word, so it is a set lookup too; a
        # config-defined key like "deep_work" is two, and needs the regex.
        key_parts = _WORD.findall((cat.key or "").lower())
        self.key_word = key_parts[0] if len(key_parts) == 1 else ""
        self.key_pat = _pattern(cat.key) if len(key_parts) > 1 else None
        singles: List[str] = []
        pairs: List[Tuple[str, str, str, re.Pattern]] = []
        long: List[re.Pattern] = []
        for kw in cat.keywords:
            parts = _WORD.findall(kw.lower())
            if len(parts) == 1:
                singles.append(parts[0])
            elif len(parts) == 2:
                pairs.append((parts[0], parts[1], parts[0] + parts[1], _pattern(kw)))
            elif parts:
                long.append(_pattern(kw))
            # a keyword with no word characters matches nothing, as before
        self.singles = tuple(singles)   # tuple, not set: duplicates still count twice
        self.pairs = tuple(pairs)
        self.long = tuple(long)

    def key_score(self, tag_text: str, tag_words: frozenset) -> int:
        """W_TAG if a tag names this category outright."""
        if self.key_word:
            return W_TAG if self.key_word in tag_words else 0
        if self.key_pat is not None and self.key_pat.search(tag_text):
            return W_TAG
        return 0

    def hits(self, text: str, words: frozenset) -> int:
        """How many of this category's keywords occur in `text`."""
        n = 0
        for w in self.singles:
            if w in words:
                n += 1
        for a, b, fused, pat in self.pairs:
            # Necessary condition for `a[sep]*b` to match: either both words
            # stand alone, or they are written as one word.
            if (a in words and b in words) or fused in words:
                if pat.search(text):
                    n += 1
        for pat in self.long:
            if pat.search(text):
                n += 1
        return n


def _scans(token: str) -> List[_Scan]:
    cached = _SCAN_CACHE.get(token)
    if cached is None:
        cached = _SCAN_CACHE[token] = [
            _Scan(c) for c in _table(token) if c.key != FALLBACK
        ]
    return cached


_BY_TOKEN: Dict[str, Dict[str, Dict[str, Any]]] = {}
_TABLE_CACHE: Dict[str, List[Category]] = {}
_INDEX_CACHE: Dict[str, Dict[str, Category]] = {}
_SCAN_CACHE: Dict[str, List[_Scan]] = {}


def score(cat: Category, tags: Iterable[str], title: str, body: str) -> int:
    """Kept for callers and tests that score one category directly."""
    tag_text = " ".join(str(t) for t in tags).lower()
    scan = _Scan(cat)
    total = W_TAG if cat.key and _match(cat.key, tag_text) else 0
    total += W_TAG * scan.hits(tag_text, frozenset(_WORD.findall(tag_text)))
    total += W_TITLE * scan.hits(title, frozenset(_WORD.findall(title.lower())))
    total += W_BODY * scan.hits(body, frozenset(_WORD.findall(body.lower())))
    return total


def detect(
    title: str = "",
    body: str = "",
    tags: Optional[Iterable[str]] = None,
    cfg=None,
) -> str:
    """Best-scoring category key for a piece of text, or `general`."""
    return _detect(
        title or "",
        (body or "")[:4000],  # a long note should not outvote its own title
        tuple(str(t) for t in (tags or [])),
        _register(cfg),
    )


# One render categorises the same note from four different code paths — the
# dashboard, the calendar tasks, the pending-dates queue and the archive list.
# Memoising on the text makes the second through fourth free.
@lru_cache(maxsize=4096)
def _detect(title: str, body: str, tags: tuple, token: str) -> str:
    tag_text = " ".join(tags).lower()
    # Tokenise each scope once for all categories instead of once per keyword.
    tag_words = frozenset(_WORD.findall(tag_text))
    title_words = frozenset(_WORD.findall(title.lower()))
    body_words = frozenset(_WORD.findall(body.lower()))

    best_key, best_score = FALLBACK, 0
    for scan in _scans(token):
        s = scan.key_score(tag_text, tag_words)
        s += W_TAG * scan.hits(tag_text, tag_words)
        s += W_TITLE * scan.hits(title, title_words)
        s += W_BODY * scan.hits(body, body_words)
        if s > best_score:
            best_key, best_score = scan.key, s
    return best_key


def categorize(note, cfg=None) -> str:
    """The category of a note: the frontmatter override if it names a real
    category, otherwise detection. Detection never overwrites the stored value
    — a manual correction has to stick."""
    token = _register(cfg)
    manual = getattr(note, "category", None)
    if manual and manual in _index(token):
        return manual
    return _detect(
        getattr(note, "title", "") or "",
        (getattr(note, "body", "") or "")[:4000],
        tuple(str(t) for t in (getattr(note, "tags", None) or [])),
        token,
    )


def decorate(text: str, category: str, cfg=None, enabled: bool = True) -> str:
    """Prefix a summary with its category emoji, idempotently."""
    cat = get(category, cfg)
    if not enabled or not cat.emoji or cat.emoji == "•":
        return text
    if text.startswith(cat.emoji):
        return text
    return f"{cat.emoji} {text}"


def as_dicts(cfg=None) -> List[Dict[str, Any]]:
    """For the API / dashboard legend."""
    return [c.model_dump() for c in table(cfg)]
