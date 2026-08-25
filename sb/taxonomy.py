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

import re
from typing import Any, Dict, Iterable, List, Optional

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


def table(cfg=None) -> List[Category]:
    """The built-in categories with `calendar.categories` folded in.

    A config entry for an existing key patches that category — its `keywords`
    are *added* to the built-in list unless `replace_keywords: true`. An entry
    for an unknown key defines a new category, and must supply enough to be
    drawable (emoji, color, hex, google_color_id all have defaults, so in
    practice `keywords` alone is enough).
    """
    overrides: Dict[str, Dict[str, Any]] = {}
    if cfg is not None:
        overrides = dict(getattr(getattr(cfg, "calendar", None), "categories", {}) or {})

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
    return {c.key: c for c in table(cfg)}


def get(key: Optional[str], cfg=None) -> Category:
    """Look up a category, falling back to `general` for unknown keys."""
    idx = index(cfg)
    if key and key in idx:
        return idx[key]
    return idx.get(FALLBACK, BUILTIN[-1])


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

_WORD = re.compile(r"[a-z0-9]+")


def _pattern(keyword: str) -> re.Pattern:
    """Word-boundary match, tolerant of the separators a keyword might appear
    with: 'problem set' matches 'problem-set' and 'problem  set'."""
    parts = _WORD.findall(keyword.lower())
    if not parts:
        return re.compile(r"(?!)")
    body = r"[\s\-_/]*".join(re.escape(p) for p in parts)
    return re.compile(rf"(?<![a-z0-9]){body}(?![a-z0-9])", re.IGNORECASE)


_CACHE: Dict[str, re.Pattern] = {}


def _match(keyword: str, text: str) -> bool:
    pat = _CACHE.get(keyword)
    if pat is None:
        pat = _CACHE[keyword] = _pattern(keyword)
    return bool(pat.search(text))


def score(cat: Category, tags: Iterable[str], title: str, body: str) -> int:
    tag_text = " ".join(str(t) for t in tags).lower()
    total = 0
    if cat.key and _match(cat.key, tag_text):
        total += W_TAG
    for kw in cat.keywords:
        if _match(kw, tag_text):
            total += W_TAG
        if _match(kw, title):
            total += W_TITLE
        if _match(kw, body):
            total += W_BODY
    return total


def detect(
    title: str = "",
    body: str = "",
    tags: Optional[Iterable[str]] = None,
    cfg=None,
) -> str:
    """Best-scoring category key for a piece of text, or `general`."""
    tags = list(tags or [])
    title = title or ""
    body = (body or "")[:4000]  # a long note should not outvote its own title
    best_key, best_score = FALLBACK, 0
    for cat in table(cfg):
        if cat.key == FALLBACK:
            continue
        s = score(cat, tags, title, body)
        if s > best_score:
            best_key, best_score = cat.key, s
    return best_key


def categorize(note, cfg=None) -> str:
    """The category of a note: the frontmatter override if it names a real
    category, otherwise detection. Detection never overwrites the stored value
    — a manual correction has to stick."""
    manual = getattr(note, "category", None)
    if manual and manual in index(cfg):
        return manual
    return detect(
        title=getattr(note, "title", "") or "",
        body=getattr(note, "body", "") or "",
        tags=getattr(note, "tags", None) or [],
        cfg=cfg,
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
