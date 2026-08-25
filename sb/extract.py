"""Rule-based extraction from raw capture text.

This module does two jobs:

  1. It is the *prior* for the LLM parser. Dates and durations are exactly the
     fields a small local model gets wrong most often ("next Friday" becomes a
     hallucinated date), and they are also the fields plain code gets right.
     So the rules run first and the model is told what they found.
  2. It is the *fallback*. With Ollama down, captures still become structured
     Projects -- worse ones, but never lost ones.

Everything here is pure and deterministic, which makes it cheap to unit-test.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

WEEKDAYS = {
    "monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3, "apr": 4,
    "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7, "aug": 8,
    "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10, "october": 10,
    "nov": 11, "november": 11, "dec": 12, "december": 12,
}

LEARNING_VERBS = (
    "learn", "study", "understand", "master", "revise", "review", "memorize",
    "practice", "read up on", "get good at", "teach myself", "figure out how",
)

HARD_SIGNALS = ("advanced", "deep dive", "from scratch", "internals", "proof", "theory")
EASY_SIGNALS = ("intro", "basics", "quick", "skim", "overview", "refresher", "cheat sheet")

_STOPWORDS = {
    "the", "a", "an", "and", "or", "to", "of", "for", "in", "on", "by", "with", "my",
    "me", "i", "learn", "study", "build", "make", "do", "read", "write", "how", "then",
    "before", "after", "next", "this", "that", "it", "about", "using", "use", "up",
    "book", "chapter", "notes", "today", "tomorrow", "week", "month", "day",
} | set(WEEKDAYS) | set(MONTHS)


# --------------------------------------------------------------------------
# dates
# --------------------------------------------------------------------------
#
# Two things go wrong with dates in a capture, and only one of them is a bug.
#
# The bug half is ordinary parsing: "next Friday" and "this Friday" landing on
# the same day, "a week from today" returning today, "in a month" meaning 30
# days rather than a calendar month, and "read pages 3/4" being read as the
# 4th of March. Those are fixed below, and each has a regression test.
#
# The other half cannot be fixed by code at all. "By the end of the week" is
# Friday to one person and Sunday to another; "next Friday" on a Saturday is
# six days away or thirteen depending on who you ask. There is no correct
# answer to find — so instead of guessing silently, the parser reports *how* it
# knows. `parse_deadline_guess` returns the date, the words it came from, and a
# kind:
#
#   explicit    the text names a date          "sept 8", "2026-09-01", "by 9/8"
#   exact       computed, one sane reading     "tomorrow", "in 2 weeks", "eod"
#   ambiguous   two people would disagree      "end of the week", "next Friday"
#
# Only the first two are treated as confirmed. An ambiguous date still reaches
# the calendar — a forgotten confirmation should not mean no reminder at all —
# but it is surfaced for approval, quoting the words it was read from, so the
# disagreement is visible instead of silent.
#
# Two more kinds are stamped further up the stack: `llm`, when the model
# returns a date the rules did not (a small model quietly rewriting "sept 8"
# into a different Tuesday is exactly what that catches), and `manual`, which
# means lj picked it and is confirmed by definition.

#: A slash or dash pair is only a date when something marks it as one. Without
#: this, "do 1/2 the chapter" is 2 January and "read pages 3/4" is 4 March.
DATE_MARKERS = r"(?:by|due|deadline|before|until|till|on|for|due\s*:|deadline\s*:)"

#: Kinds. `confirmed` is derived from these in one place, below.
EXPLICIT = "explicit"
EXACT = "exact"
AMBIGUOUS = "ambiguous"
LLM = "llm"
MANUAL = "manual"
NONE = "none"

CONFIRMED_KINDS = (EXPLICIT, EXACT, MANUAL)


class DateGuess:
    """A parsed deadline, and how much to trust it.

    The distinction that matters is not "did we find a date" but "did the text
    *name* one". `phrase` is what makes the approval prompt readable: it can
    say *read from "end of the week"* rather than asking lj to re-derive why
    the system thinks a project is due on the 28th.
    """

    __slots__ = ("date", "phrase", "kind")

    def __init__(self, date: Optional[dt.date] = None, phrase: str = "", kind: str = NONE):
        self.date = date
        self.phrase = phrase.strip()
        self.kind = kind if date else NONE

    @property
    def confirmed(self) -> bool:
        return self.kind in CONFIRMED_KINDS

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"DateGuess({self.date}, {self.phrase!r}, {self.kind})"

    def __bool__(self) -> bool:
        return self.date is not None


def parse_deadline(text: str, today: Optional[dt.date] = None) -> Optional[dt.date]:
    """Just the date. Returns None when unsure — an absent deadline is far
    less harmful than a wrong one."""
    return parse_deadline_guess(text, today).date


def parse_deadline_guess(text: str, today: Optional[dt.date] = None) -> DateGuess:
    """Pull a due date out of natural language, with its provenance.

    Rule order is load-bearing and not arbitrary:

      1. An explicit `due:` prefix wins over anything else in the sentence.
      2. Formats that *name* a date come next, most specific first.
      3. Intervals ("a week from today") run **before** the bare `today`
         matcher, which used to claim the word "today" out of the middle of
         the phrase and return the wrong end of the interval.
      4. Relative phrases last, because they are the guesses.
    """
    today = today or dt.date.today()
    if not text:
        return DateGuess()
    t = text.lower()

    for rule in (
        _explicit_prefix,
        _iso_date,
        _numeric_pair,
        _month_name,
        _day_of_month,
        _interval,
        _named_day,
        _weekend,
        _weekday_reference,
        _end_of_period,
        _next_period,
    ):
        guess = rule(t, text, today)
        if guess and guess.date:
            return guess
    return DateGuess()


# -- rules, in priority order ----------------------------------------------


def _explicit_prefix(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`due: 2026-12-01`, `deadline = friday`.

    Typing a `due:` prefix is an unambiguous act, so whatever follows is read
    literally and never re-interpreted: it is explicit even when the phrase
    inside it ("friday") would otherwise be a guess.
    """
    m = re.search(r"\b(?:due|deadline)\s*[:=]\s*([^\n,;]+)", t)
    if not m:
        return None
    inner = parse_deadline_guess(m.group(1), today)
    if not inner.date:
        return None
    return DateGuess(inner.date, raw[m.start() : m.end()].strip(), EXPLICIT)


def _iso_date(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    m = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t)
    if not m:
        return None
    try:
        return DateGuess(
            dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3))),
            raw[m.start() : m.end()],
            EXPLICIT,
        )
    except ValueError:
        return None


def _numeric_pair(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`9/8`, `09/01/2026`, `8-25` — but only when marked as a date.

    A bare number pair is far more often a fraction or a page range than a
    date. It counts only when a preposition introduces it, a four-digit year
    settles it, or the pair is the entire capture. "do 1/2 the chapter" and
    "read pages 3/4" therefore parse to *no date*, which is the right answer:
    this module's own rule is that an absent deadline beats a wrong one.
    """
    pattern = re.compile(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b")
    whole = t.strip()
    for m in pattern.finditer(t):
        before = t[max(0, m.start() - 12) : m.start()]
        marked = (
            bool(re.search(DATE_MARKERS + r"\s+$", before))
            or bool(m.group(3) and len(m.group(3)) == 4)
            or m.group(0) == whole
        )
        if not marked:
            continue
        month, day = int(m.group(1)), int(m.group(2))
        year = int(m.group(3) or today.year)
        if year < 100:
            year += 2000
        try:
            cand = dt.date(year, month, day)
        except ValueError:
            continue
        if not m.group(3) and cand < today:
            try:
                cand = dt.date(year + 1, month, day)
            except ValueError:
                continue
        return DateGuess(cand, raw[m.start() : m.end()], EXPLICIT)
    return None


def _month_name(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`sept 8`, `8 september`, `the 30th of September`."""
    m = re.search(r"\b([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?\b", t)
    if m and m.group(1) in MONTHS:
        found = _clamp_forward(today, MONTHS[m.group(1)], int(m.group(2)))
        if found:
            return DateGuess(found, raw[m.start() : m.end()], EXPLICIT)
    m = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]{3,9})\b", t)
    if m and m.group(2) in MONTHS:
        found = _clamp_forward(today, MONTHS[m.group(2)], int(m.group(1)))
        if found:
            return DateGuess(found, raw[m.start() : m.end()], EXPLICIT)
    return None


def _day_of_month(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`the 30th`, `by the 5th`, `due 15th`.

    The ordinal suffix and the leading word are both required, so "1st
    chapter" and "3 sections" are not mistaken for dates. Classed `exact`
    rather than `explicit`: the text names a day but leaves the month to be
    inferred, and the inference — the next occurrence — has only one sensible
    reading.
    """
    m = re.search(r"\b(?:on|by|due|before|the)\s+(?:the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", t)
    if not m:
        return None
    day = int(m.group(1))
    if not 1 <= day <= 31:
        return None
    cursor = today
    for _ in range(13):
        last = _last_day(cursor.year, cursor.month)
        if day <= last:
            cand = dt.date(cursor.year, cursor.month, day)
            if cand >= today:
                return DateGuess(cand, raw[m.start() : m.end()], EXACT)
        cursor = _add_months(cursor.replace(day=1), 1)
    return None


def _interval(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`in 3 days`, `in 2 weeks`, `a week from today`, `2 weeks from now`.

    Runs **before** the bare `today` matcher. It used to run after, so "a week
    from today" hit the word "today" in the middle of the phrase and returned
    the wrong end of the interval — an off-by-a-week that looks like a
    correctly parsed date.

    Months are calendar months, not 30 days, clamped to the end of short ones:
    a month after 31 January is 28 February, not 3 March.
    """
    patterns = (
        r"\bin\s+(a|an|\d+)\s+(day|week|month)s?\b",
        r"\b(a|an|\d+)\s+(day|week|month)s?\s+from\s+(?:now|today)\b",
    )
    for pattern in patterns:
        m = re.search(pattern, t)
        if not m:
            continue
        n = 1 if m.group(1) in ("a", "an") else int(m.group(1))
        unit = m.group(2)
        phrase = raw[m.start() : m.end()]
        if unit == "month":
            # "in a month" is a gesture at a rough horizon, not a date.
            return DateGuess(_add_months(today, n), phrase, AMBIGUOUS)
        days = n * (7 if unit == "week" else 1)
        return DateGuess(today + dt.timedelta(days=days), phrase, EXACT)
    return None


def _named_day(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`today`, `tonight`, `eod`, `tomorrow`, `the day after tomorrow`."""
    for pattern, offset in (
        (r"\bday after tomorrow\b", 2),
        (r"\btomorrow\b", 1),
        (r"\btoday\b|\btonight\b|\beod\b|\bend of (?:the )?day\b", 0),
    ):
        m = re.search(pattern, t)
        if m:
            return DateGuess(today + dt.timedelta(days=offset), raw[m.start() : m.end()], EXACT)
    return None


def _weekend(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`this weekend`, `next weekend` — the Saturday, and ambiguous about it.

    Saturday is the usual read, but plenty of people mean Sunday, and "next
    weekend" mid-week is genuinely contested. Resolved to Saturday and flagged
    rather than settled quietly.
    """
    m = re.search(r"\b(this|next|the)?\s*weekend\b", t)
    if not m:
        return None
    saturday = _coming_weekday(today, 5, include_today=True)
    if (m.group(1) or "") == "next":
        saturday += dt.timedelta(days=7)
    return DateGuess(saturday, raw[m.start() : m.end()].strip(), AMBIGUOUS)


def _weekday_reference(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`friday`, `by monday`, `next friday`, `due thurs`.

    Scans for the first real weekday token and reads the qualifier in front of
    it, so "by next friday" works as well as "next friday".

    The semantics, which used to collapse into each other:

      * bare or "this" → the next occurrence, strictly in the future. On a
        Saturday, "this Friday" is six days away.
      * "next"         → that, plus a week. On a Saturday, "next Friday" is
        thirteen days away.

    The old code read "next" as *Friday of next calendar week*, which on a
    Saturday is the same Friday a bare "Friday" already meant — both returned
    the 28th. A deadline landing a week early is exactly the kind of wrong
    that looks right. "next" is flagged ambiguous even now, because this is a
    convention lj chose rather than a fact the text settles.
    """
    for m in re.finditer(r"\b([a-z]{3,9})\b", t):
        if m.group(1) not in WEEKDAYS:
            continue
        prefix = t[max(0, m.start() - 24) : m.start()]
        qualifier = ""
        q = re.search(r"\b(next|this)\s+$", prefix)
        if q:
            qualifier = q.group(1)
        start = m.start() - (len(qualifier) + 1 if qualifier else 0)
        phrase = raw[max(0, start) : m.end()]
        target = _coming_weekday(today, WEEKDAYS[m.group(1)], include_today=False)
        if qualifier == "next":
            return DateGuess(target + dt.timedelta(days=7), phrase, AMBIGUOUS)
        return DateGuess(target, phrase, EXACT)
    return None


def _end_of_period(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`eow`, `end of the week`, `eom`, `end of the month`."""
    m = re.search(r"\beow\b|\bend of (?:the )?week\b", t)
    if m:
        # Friday to some, Sunday to others. Friday, and say so.
        return DateGuess(
            _coming_weekday(today, 4, include_today=True), raw[m.start() : m.end()], AMBIGUOUS
        )
    m = re.search(r"\beom\b|\bend of (?:the )?month\b", t)
    if m:
        # The last day of the month has no competing reading.
        return DateGuess(
            dt.date(today.year, today.month, _last_day(today.year, today.month)),
            raw[m.start() : m.end()],
            EXACT,
        )
    return None


def _next_period(t: str, raw: str, today: dt.date) -> Optional[DateGuess]:
    """`next week`, `next month` — a horizon, not a date."""
    m = re.search(r"\bnext week\b", t)
    if m:
        monday = today + dt.timedelta(days=7 - today.weekday())
        return DateGuess(monday + dt.timedelta(days=4), raw[m.start() : m.end()], AMBIGUOUS)
    m = re.search(r"\bnext month\b", t)
    if m:
        return DateGuess(_add_months(today, 1), raw[m.start() : m.end()], AMBIGUOUS)
    return None


# -- date arithmetic --------------------------------------------------------


def _coming_weekday(today: dt.date, weekday: int, include_today: bool = False) -> dt.date:
    delta = (weekday - today.weekday()) % 7
    if delta == 0 and not include_today:
        delta = 7
    return today + dt.timedelta(days=delta)


def _next_weekday(today: dt.date, weekday: int, force_next: bool = False) -> dt.date:
    """Kept for callers outside this module. "next" now means a full week
    beyond the coming occurrence, which is the fix, not the old
    "Friday of next calendar week"."""
    coming = _coming_weekday(today, weekday)
    return coming + dt.timedelta(days=7) if force_next else coming


def _last_day(year: int, month: int) -> int:
    import calendar

    return calendar.monthrange(year, month)[1]


def _add_months(day: dt.date, n: int) -> dt.date:
    """Real calendar months, clamped: 31 Jan + 1 month is 28 Feb, not 3 Mar."""
    index = day.month - 1 + n
    year = day.year + index // 12
    month = index % 12 + 1
    return dt.date(year, month, min(day.day, _last_day(year, month)))


def _clamp_forward(today: dt.date, month: int, day: int) -> Optional[dt.date]:
    try:
        cand = dt.date(today.year, month, day)
    except ValueError:
        return None
    if cand < today:
        try:
            cand = dt.date(today.year + 1, month, day)
        except ValueError:
            return None
    return cand


# --------------------------------------------------------------------------
# duration / difficulty / topic
# --------------------------------------------------------------------------


def parse_duration_minutes(text: str) -> Optional[int]:
    t = text.lower()
    total = 0
    found = False
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h)\b", t):
        total += int(float(m.group(1)) * 60)
        found = True
    for m in re.finditer(r"(\d+)\s*(minutes?|mins?|m)\b", t):
        total += int(m.group(1))
        found = True
    for m in re.finditer(r"(\d+)\s*(days?)\b", t):
        total += int(m.group(1)) * 240  # a "day" of focused work, not 24h
        found = True
    return total if found and total > 0 else None


def guess_level(text: str) -> int:
    t = text.lower()
    score = 3
    score += sum(1 for s in HARD_SIGNALS if s in t)
    score -= sum(1 for s in EASY_SIGNALS if s in t)
    if len(text) > 400:
        score += 1
    return max(1, min(5, score))


def is_learning(text: str) -> bool:
    t = text.lower()
    return any(v in t for v in LEARNING_VERBS)


def extract_steps(text: str) -> List[str]:
    """Explicit steps only: markdown bullets, numbered lists, or 'then' chains.
    If the capture has no structure, return nothing and let the LLM propose a
    breakdown -- inventing steps with regex produces noise."""
    steps: List[str] = []
    for line in text.split("\n"):
        s = line.strip()
        m = re.match(r"^(?:[-*+]\s+|\d+[.)]\s+)\[?[ xX]?\]?\s*(.+)$", s)
        if m and m.group(1).strip():
            steps.append(m.group(1).strip())
    if steps:
        return steps

    first = text.strip().split("\n")[0]
    if " then " in first.lower():
        parts = re.split(r"\s+then\s+", first, flags=re.I)
        if len(parts) > 1:
            return [p.strip(" .,;") for p in parts if p.strip()]
    return []


def extract_skills(text: str) -> List[str]:
    """Cheap keyword pass: proper nouns, code spans and hashtags. The LLM does
    this better; this exists so the fallback path is not empty."""
    skills: List[str] = []
    skills += re.findall(r"`([^`]{2,30})`", text)
    skills += [h.lstrip("#") for h in re.findall(r"#(\w{2,30})", text)]
    for tok in re.findall(r"\b([A-Z][A-Za-z0-9+#.]{2,20})\b", text):
        if tok.lower() not in _STOPWORDS:
            skills.append(tok)
    seen, out = set(), []
    for s in skills:
        k = s.lower().strip()
        if k and k not in seen and k not in _STOPWORDS:
            seen.add(k)
            out.append(s.strip())
    return out[:6]


def extract_materials(text: str) -> List[str]:
    mats: List[str] = []
    mats += re.findall(r"https?://\S+", text)
    mats += re.findall(r"\[\[([^\]]+)\]\]", text)  # Obsidian wikilinks
    for m in re.finditer(r'"([^"]{3,60})"', text):
        mats.append(m.group(1))
    seen, out = set(), []
    for m in mats:
        if m not in seen:
            seen.add(m)
            out.append(m)
    return out[:8]


def derive_title(text: str, max_len: int = 80) -> str:
    """A note title should name the thing, not restate the whole capture.
    Strips the scheduling clauses -- "by next Friday", "about 4 hours",
    "in 2 weeks" -- because that information now lives in the metadata."""
    line = ""
    for candidate in text.strip().split("\n"):
        candidate = candidate.strip().lstrip("#").strip()
        if candidate:
            line = candidate
            break
    if not line:
        return "Untitled"

    line = re.sub(r"^(?:todo|task|note|project|remember)\s*[:\-]\s*", "", line, flags=re.I)
    line = re.sub(r"^[-*+]\s+", "", line)

    cuts = [
        r"\s*[,;—-]?\s*\bby\s+(?:next|this|the|tomorrow|today|mon|tue|wed|thu|fri|sat|sun|eod|end\b|\d).*$",
        r"\s*[,;—-]?\s*\b(?:due|deadline|before|until|till)\b.*$",
        r"\s*[,;—-]?\s*\bin\s+(?:a|an|\d+)\s+(?:day|week|month)s?\b.*$",
        r"\s*[,;—-]?\s*\b(?:about|approx\.?|around|roughly|~)?\s*\d+(?:\.\d+)?\s*"
        r"(?:hours?|hrs?|h|minutes?|mins?)\b.*$",
        r"\s*[,;—-]?\s*\b(?:tomorrow|tonight|next week|next month|this week)\b.*$",
        # bare scheduling clauses with no preposition in front of them:
        # "Learn Rust generics next friday" should title as "Learn Rust generics"
        r"\s*[,;—-]?\s*\b(?:next|this)\s+"
        r"(?:mon|tue|tues|wed|thu|thur|thurs|fri|sat|sun)[a-z]*\b.*$",
        r"\s*[,;—-]?\s*\b(?:this|next)\s+weekend\b.*$",
        r"\s*[,;—-]?\s*\b(?:eow|eom|end of (?:the )?(?:week|month|day))\b.*$",
    ]
    trimmed = line
    for pattern in cuts:
        trimmed = re.sub(pattern, "", trimmed, flags=re.I)
    trimmed = trimmed.strip(" ,;:-—.")

    if len(trimmed) < 3:  # over-trimmed; keep the original line
        trimmed = line.strip(" ,;:-—.")
    trimmed = trimmed[:max_len].strip()
    return (trimmed[0].upper() + trimmed[1:]) if trimmed else "Untitled"


# --------------------------------------------------------------------------
# the composite prior
# --------------------------------------------------------------------------


def project_prior(text: str, today: Optional[dt.date] = None) -> Dict[str, object]:
    """Everything the rules can determine, in the shape of ProjectMeta."""
    today = today or dt.date.today()
    guess = parse_deadline_guess(text, today)
    minutes = parse_duration_minutes(text)
    steps = extract_steps(text)
    if minutes is None:
        minutes = max(30, 45 * len(steps)) if steps else 60
    return {
        "deadline": guess.date,
        "deadline_kind": guess.kind,
        "deadline_phrase": guess.phrase,
        "deadline_confirmed": guess.confirmed,
        "estimate_minutes": minutes,
        "level": guess_level(text),
        "skills": extract_skills(text),
        "materials": extract_materials(text),
        "steps": steps,
        "learning": is_learning(text),
    }
