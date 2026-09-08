"""Card quality — the minimum information principle, enforced.

`generate.py` already refuses four shapes: a question that restates itself, a
question that says "the above", an answer that is only the note's title, and a
duplicate. None of those is the thing that actually decides whether a card is
learnable. That is **how much one card asks you to remember**, and until now
nothing checked it.

The rules implemented here are Piotr Woźniak's, *Twenty rules of formulating
knowledge* (SuperMemo, 1999) — the only published standard for this:

  * **rule 4, minimum information.** One card, one thing. A two-part answer is
    not a hard card; it is two cards sharing a schedule, and the schedule is
    then wrong for both. Failing one half re-drills the half you knew.
  * **rule 5, cloze deletion.** For definitional text a fill-in-the-blank
    beats a question, because the sentence supplies the context the question
    would otherwise have to carry.
  * **rule 7, avoid sets.** A set has no order and no cue; recall of the last
    item is the hardest thing in the deck.
  * **rule 8, avoid enumerations.** Famously the least learnable card there
    is, and the shape a summarising model produces most eagerly.

## Reject, or repair?

Rejecting is cheap but it throws away a passage the model already read.
Where the offending answer is a list *and* the source sentence is available
verbatim, this module rewrites it instead: one cloze card per item, over the
sentence the note actually contains. That is exactly rule 8's own prescribed
remedy, and it cannot invent anything — every character of the new card is
copied from the source, the same guarantee `generate.py`'s citation check
gives.

Where no verbatim sentence backs the answer, the card is rejected and counted.
A rejection here is not a failure: the model returned three cards from the
passage and one of them was a list, so two good cards ship.

## Deliberately not enforced on cards lj types

`assess()` is advisory outside the generator. A card someone writes by hand is
a decision, not a draft; the dashboard shows the warning and files the card.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

#: Woźniak gives no number; the practitioner consensus (Anki manual, the
#: SuperMemo forums) lands around a dozen words as the point where an answer
#: has stopped being one thing. 15 leaves room for a qualified definition.
MAX_ANSWER_WORDS = 15

#: Past this, no repair is attempted — an answer this long is a paragraph the
#: model failed to reduce, not a card with a fixable shape.
HARD_ANSWER_WORDS = 30

#: A list of ten becomes ten cards only in theory; in practice the note is
#: better studied as prose. Cap what one bad card is allowed to expand into.
MAX_SPLIT = 4

MIN_ITEM_CHARS = 2
MIN_CLOZE_CONTEXT = 24

#: An explanation is allowed to be longer than a term, because that is what
#: makes it an explanation. Still capped: past this it is a passage, and a
#: passage is not a card whatever you call it.
MAX_EXPLAIN_WORDS = 45

#: Multiple choice, from the measurement literature on item flaws (Haladyna,
#: Downing & Rodriguez, *A Review of Multiple-Choice Item-Writing Guidelines*,
#: 2002). Three options is the floor at which guessing stops carrying the
#: card; above five the extra distractors are filler nobody reads.
MIN_OPTIONS = 3
MAX_OPTIONS = 5

#: The best-documented flaw there is: the correct option is longer than the
#: rest, because the writer qualified it into correctness. A test-wise reader
#: scores well above chance on that alone, which means the card is measuring
#: test-wiseness rather than the material. Compared against the *median*
#: distractor so one rambling wrong answer cannot excuse a long right one.
LENGTH_TELL_MAX = 1.6
LENGTH_TELL_MIN = 0.4
#: The check is skipped only when *everything* is short — "12%" against "8%"
#: is noise, not a tell. It is emphatically not skipped when the options are
#: short and the answer is not: that is the tell at its most blatant.
LENGTH_TELL_FLOOR_CHARS = 12

#: Options that give the game away or ask a different question than the stem.
_GIVEAWAY = re.compile(
    r"\b(?:all|none|both|any) of (?:the )?(?:above|these|them)\b"
    r"|\bboth [a-d] and [a-d]\b|\bnot (?:listed|given)\b",
    re.I,
)

_BULLET = re.compile(r"(?m)^[ \t]*(?:[-*•–]|\d+[.)])[ \t]+")
_SPLIT = re.compile(r"\s*(?:;|,\s*(?:and|or|plus)\b|,|\band\b|\bor\b|\bplus\b|\bas well as\b)\s*", re.I)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
_DIGIT_COMMA = re.compile(r"(?<=\d),(?=\d)")

#: "the difference between X and Y", "both A and B" — one idea that happens to
#: contain a conjunction. Splitting these produces two nonsense cards.
_BOUND_AND = re.compile(r"\b(?:between|both|either|neither|from)\b[^,;]{0,60}?\b(?:and|or|to)\b", re.I)

#: Fixed pairs that are single terms. Short list on purpose: every entry is a
#: claim that lj will never want the halves tested separately.
_IDIOMS = (
    "trial and error", "cause and effect", "supply and demand",
    "risk and return", "rise and fall", "black and white",
    "salt and pepper", "cost and benefit", "trial and appeal",
    "research and development", "terms and conditions",
)

_LIST_QUESTION = re.compile(r"\b(?:list|enumerate|name (?:all|the \w+|three|four|five))\b", re.I)
_DEFINITION_Q = re.compile(
    r"^\s*(?:what\s+(?:is|are|was|were)\s+|what\s+does\s+.+\s+mean|define\s+|definition\s+of\s+)",
    re.I,
)
_DEFINITION_S = r"\s+(?:is|are|was|were|means|refers to|stands for)\s+"
_LEADING_ARTICLE = re.compile(r"^(?:a|an|the)\s+", re.I)


# --------------------------------------------------------------------------
# verdicts
# --------------------------------------------------------------------------


@dataclass
class Verdict:
    """Why a card is or is not one card."""

    ok: bool
    rule: str = ""      # minimum-information | enumeration | set | length | list-question
    reason: str = ""
    items: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:  # `if assess(...)` reads naturally
        return self.ok


def words(text: str) -> int:
    return len((text or "").split())


def facts(answer: str) -> List[str]:
    """The distinct things this answer asks you to recall.

    One item back means one fact — the card is fine. Two or more means the
    answer is a set (rule 7) or an enumeration (rule 8) wearing one card's
    clothes.

    The detection is deliberately conservative in one direction: a phrase is
    only split when the punctuation or the conjunction is doing list work.
    "September 4, 2026" and "salt and pepper" are one fact; "increases
    pressure and lowers volume" is two.
    """
    text = (answer or "").strip().rstrip(".")
    if not text:
        return []

    bullets = [p.strip(" \t•-*") for p in _BULLET.split(text)]
    bullets = [b.strip().rstrip(".;,") for b in bullets if b.strip()]
    if len(bullets) >= 2:
        return [b for b in bullets if len(b) >= MIN_ITEM_CHARS][:12]

    probe = _DIGIT_COMMA.sub("", text)          # 1,000 is not a list
    lowered = probe.lower()
    if any(idiom in lowered for idiom in _IDIOMS):
        return [text]
    if _BOUND_AND.search(probe):
        return [text]

    has_conj = re.search(r"\b(?:and|or|plus|as well as)\b", probe, re.I) is not None
    listy = (
        ";" in probe
        or probe.count(",") >= 2
        or (probe.count(",") >= 1 and has_conj)
        or (has_conj and words(probe) > 4)
    )
    if not listy:
        sentences = [s for s in _SENTENCE.split(text) if words(s) >= 3]
        return sentences if len(sentences) >= 2 else [text]

    parts = [p.strip().strip("•-*").strip().rstrip(".;,") for p in _SPLIT.split(probe)]
    parts = [p for p in parts if len(p) >= MIN_ITEM_CHARS]
    return parts[:12] if len(parts) >= 2 else [text]


def assess(front: str, back: str, *, max_words: int = MAX_ANSWER_WORDS) -> Verdict:
    """Is this one card? Ordered so the most specific reason is the one given."""
    front, back = (front or "").strip(), (back or "").strip()

    if words(back) > HARD_ANSWER_WORDS:
        return Verdict(False, "length", f"the answer is {words(back)} words — that is a paragraph, not a card")

    if _LIST_QUESTION.search(front):
        return Verdict(False, "list-question", "the question asks for a list; lists are not recallable as one card")

    items = facts(back)
    if len(items) >= 3:
        return Verdict(False, "enumeration", f"the answer enumerates {len(items)} things", items)
    if len(items) == 2:
        return Verdict(False, "set", "the answer holds two facts — that is two cards", items)

    if words(back) > max_words:
        return Verdict(False, "length", f"the answer is {words(back)} words; one card should ask for one thing")

    return Verdict(True)


def is_list_question(front: str) -> bool:
    """Does this question ask for a list? True of every card type there is."""
    return bool(_LIST_QUESTION.search(front or ""))


def assess_choices(answer: str, options: List[str]) -> Verdict:
    """Is this a multiple-choice card worth answering?

    Every rule here is a way a card can look right and measure nothing. They
    run cheapest-first so the reason given is the most specific one true.
    """
    answer = (answer or "").strip()
    clean = [o.strip() for o in (options or []) if o and o.strip()]

    if not answer:
        return Verdict(False, "choice-no-answer", "no correct answer to mark against")
    if len(clean) < MIN_OPTIONS:
        return Verdict(
            False, "choice-too-few",
            f"{len(clean)} options — under {MIN_OPTIONS}, guessing carries the card",
        )
    if len(clean) > MAX_OPTIONS:
        return Verdict(
            False, "choice-too-many", f"{len(clean)} options is filler, not discrimination"
        )

    normed = [_norm_choice(o) for o in clean]
    if len(set(normed)) != len(normed):
        return Verdict(False, "choice-duplicate", "two options are the same option")

    matches = [o for o in clean if _norm_choice(o) == _norm_choice(answer)]
    if len(matches) != 1:
        return Verdict(
            False, "choice-not-one-answer",
            f"{len(matches)} of the options are the answer; exactly one must be",
        )

    for option in clean:
        if _GIVEAWAY.search(option):
            return Verdict(False, "choice-giveaway", f"“{option}” is not a real option")

    distractors = [o for o in clean if _norm_choice(o) != _norm_choice(answer)]
    lengths = sorted(len(d) for d in distractors)
    median = lengths[len(lengths) // 2] if lengths else 0
    if median and max(len(matches[0]), median) >= LENGTH_TELL_FLOOR_CHARS:
        ratio = len(matches[0]) / median
        if ratio > LENGTH_TELL_MAX:
            return Verdict(
                False, "choice-length-tell",
                f"the right answer is {ratio:.1f}x the length of the others — "
                f"it can be picked without reading the question",
            )
        if ratio < LENGTH_TELL_MIN:
            return Verdict(
                False, "choice-length-tell",
                "the right answer is far shorter than every wrong one, which is "
                "the same tell in reverse",
            )
    return Verdict(True)


def _norm_choice(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


# --------------------------------------------------------------------------
# repair — rule 8's own remedy, and rule 5
# --------------------------------------------------------------------------


def _sentence_for(item: str, *sources: str) -> str:
    """The shortest verbatim sentence, from anything we were given, that
    contains `item`. Verbatim is the whole point: a repaired card must be as
    citable as the card it replaces."""
    needle = item.strip().lower()
    if len(needle) < MIN_ITEM_CHARS:
        return ""
    best = ""
    for source in sources:
        for sentence in _SENTENCE.split(re.sub(r"\s+", " ", source or "")):
            sentence = sentence.strip()
            if needle in sentence.lower() and (not best or len(sentence) < len(best)):
                best = sentence
    return best


def _blank(sentence: str, item: str) -> Tuple[str, str]:
    """Wrap the first occurrence of `item` in cloze braces.

    Returns the blanked sentence and the text as the sentence spells it — the
    answer has to be what the blank hides, not what the model's answer field
    happened to capitalise.
    """
    match = re.search(re.escape(item), sentence, re.I)
    if not match:
        return "", ""
    start, end = match.span()
    return (
        f"{sentence[:start]}{{{{{sentence[start:end]}}}}}{sentence[end:]}",
        sentence[start:end],
    )


def split_to_cloze(
    back: str, quote: str, passage: str = "", *, max_cards: int = MAX_SPLIT
) -> List[Tuple[str, str, str]]:
    """Rewrite a list answer as one cloze card per item.

    Returns `(front, back, source)` triples. Every character of every card
    comes from `quote` or `passage`, so this can add cards but never facts.
    Returns an empty list when no verbatim sentence backs an item — a repair
    we cannot cite is a repair we do not make.
    """
    out: List[Tuple[str, str, str]] = []
    seen = set()
    for item in facts(back):
        if len(out) >= max_cards:
            break
        item = item.strip()
        if len(item) < MIN_ITEM_CHARS or words(item) > 8 or item.lower() in seen:
            continue
        sentence = _sentence_for(item, quote, passage)
        if not sentence or "{{" in sentence:
            continue
        front, shown = _blank(sentence, item)
        if not front or len(sentence) - len(item) < MIN_CLOZE_CONTEXT:
            continue
        seen.add(item.lower())
        out.append((front, shown, sentence))
    return out if len(out) >= 2 else []


def definiendum(front: str) -> str:
    """The term a "what is X?" question is asking about, or ""."""
    if not _DEFINITION_Q.match(front or ""):
        return ""
    term = _DEFINITION_Q.sub("", front.strip(), count=1)
    term = re.sub(r"^\s*(?:the\s+term\s+)?", "", term, flags=re.I)
    term = term.strip().strip("?.:“”\"'")
    term = _LEADING_ARTICLE.sub("", term).strip()
    return term if 3 <= len(term) and words(term) <= 6 else ""


def to_cloze(front: str, back: str, quote: str, passage: str = "") -> Optional[Tuple[str, str, str]]:
    """Rule 5: turn "What is X?" / long definition into "{{X}} is ..." .

    The blank falls on the term, not on the definition — blanking a
    twenty-word definition is the original card with extra steps. Requires a
    verbatim sentence that defines the term, so the wording tested is the
    note's own.
    """
    term = definiendum(front)
    if not term:
        return None
    for source in (quote, passage):
        for sentence in _SENTENCE.split(re.sub(r"\s+", " ", source or "")):
            sentence = sentence.strip()
            if not sentence or "{{" in sentence:
                continue
            if not re.search(re.escape(term) + _DEFINITION_S, sentence, re.I):
                continue
            if len(sentence) - len(term) < MIN_CLOZE_CONTEXT:
                continue
            blanked, shown = _blank(sentence, term)
            if blanked:
                return (blanked, shown, sentence)
    return None
