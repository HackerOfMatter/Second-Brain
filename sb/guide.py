"""The study guide: what a note is *about*, before any card is written.

This is the NotebookLM half of card making. `generate.py` used to read a note
one passage at a time, in order, and stop when the card cap was reached. That
has two failure modes, and both were visible in lj's first real decks:

  * **Front-loading.** Twenty cards at three per passage is seven passages.
    A chapter with thirty passages got cards from its first seven and
    nothing from the rest, however important the rest was.
  * **Trivia over ideas.** A passage read in isolation has no way to know
    which of its sentences the whole note is built around, so the model
    tests whatever is easiest to phrase as a question.

NotebookLM's answer is to read the whole source first — a study guide, a list
of key terms — and write questions against that. This module does the same
thing with the budget a 12 GB card allows:

  1. **Key terms by rule**, always: bold text, definition sentences,
     glossary lines (`Term: meaning`), and acronyms with their expansion.
     Free, deterministic, and exactly the things an author marks as
     important.
  2. **Key concepts by model**, optionally: one call over a compact outline
     (a heading and an opening line per passage), never over the full text.
     Every concept the model names must appear in the note — the same
     citation defence the cards get — or it is dropped.
  3. **A plan** that visits passages by weight, then by spread, and skips
     passages that already have cards. "Fill gaps" is this plan with covered
     passages removed.

It also owns **grounding** — deciding whether a drafted card is supported by
its passage — and **coverage**, which passage each existing card came from.
Both are pure functions over text, which is what makes them testable without
a model.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: A passage with no key terms still gets a base weight, so an unmarked note
#: is visited in spread order rather than not at all.
BASE_WEIGHT = 1.0
#: How much an uncovered passage outranks a covered one. Large on purpose:
#: coverage is the point, weight only orders within it.
UNCOVERED_BONUS = 10.0
#: Terms longer than this are sentences the regex caught, not terms.
MAX_TERM_WORDS = 6
#: The model concept pass asks for at most this many.
MAX_MODEL_CONCEPTS = 12
#: How much of each passage the outline shows the model.
OUTLINE_CHARS = 160

#: Grounding thresholds — the share of an answer's content words that must
#: appear in one sentence of the passage. An explanation is a paraphrase by
#: nature, so it is held to less.
GROUND_SHARE = 0.6
GROUND_SHARE_EXPLAIN = 0.4
MIN_QUOTE_CHARS = 12

_STOP = {
    "a", "an", "the", "and", "or", "but", "of", "to", "in", "on", "at", "by",
    "for", "with", "from", "as", "is", "are", "was", "were", "be", "been",
    "being", "it", "its", "this", "that", "these", "those", "which", "what",
    "who", "whom", "whose", "when", "where", "why", "how", "do", "does", "did",
    "has", "have", "had", "not", "no", "so", "if", "then", "than", "into",
    "about", "can", "will", "would", "should", "could", "may", "might", "must",
    "their", "they", "them", "there", "you", "your", "we", "our", "he", "she",
    "his", "her", "i", "me", "my", "also", "such", "more", "most", "very",
}

_BOLD = re.compile(r"(?:\*\*|__)([^*_\n]{2,80}?)(?:\*\*|__)")
_DEFINITION = re.compile(
    r"(?:^|(?<=[.!?]\s))(?P<term>[A-Z][A-Za-z0-9'’\- ]{1,60}?)\s+"
    r"(?:is|are|means|refers to|stands for|is defined as|may be defined as)\s+\S",
)
#: "… is called noise" — the term is the thing named, at the end.
_CALLED = re.compile(
    r"\b(?:is|are|was|were)\s+(?:called|known as|termed|referred to as)\s+"
    r"(?P<term>[A-Za-z][A-Za-z0-9'’\- ]{2,40}?)(?=[.,;:)]|$)",
)
#: Sentence subjects that are not terms: "Anything that…", "This…".
_NOT_A_TERM = re.compile(
    r"^(?:anything|something|everything|nothing|this|that|these|those|it|one|"
    r"there|here|what|which|who|each|every|some|many|most|all)\b|\b(?:this|that|these|those)\b",
    re.I,
)
MAX_DEFINITION_WORDS = 4

_GLOSSARY = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?(?P<term>[A-Za-z][A-Za-z0-9'’\- ]{1,50}?)(?:\*\*)?"
    r"\s*(?::|—|–|\s-\s)\s+\S",
    re.M,
)
_ACRONYM = re.compile(r"\(\s*(?P<acr>[A-Z]{2,7})\s*\)|\b(?P<acr2>[A-Z]{2,7})\s*\(")
_HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$", re.M)
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")

#: Glossary-shaped lines that are really the system's own labels.
_LABEL_WORDS = {
    "due", "note", "notes", "source", "from", "tags", "status", "date", "time",
    "example", "examples", "step", "steps", "q", "a", "why", "type", "mode",
    "hint", "worked", "choices", "created", "updated", "link", "links", "see",
    # Section labels the vault's own templates write as "Label: …" lines.
    "connected to", "additional notes", "summary", "key points", "key terms",
    "questions", "definition", "definitions", "related", "sources", "references",
    "course", "chapter", "week", "due date", "instructor", "reading", "readings",
    "assignment", "notes from", "takeaway", "takeaways", "action items",
}


# --------------------------------------------------------------------------
# text helpers
# --------------------------------------------------------------------------


def norm(text: str) -> str:
    """Lower-case, punctuation-free, whitespace-collapsed."""
    text = (text or "").lower().replace("’", "'")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9' ]+", " ", text)).strip()


def _stem(word: str) -> str:
    for suffix in ("ing", "ies", "ed", "es", "s"):
        if len(word) > len(suffix) + 2 and word.endswith(suffix):
            return word[: -len(suffix)] + ("y" if suffix == "ies" else "")
    return word


def content_words(text: str) -> set:
    return {_stem(w) for w in norm(text).replace("'", " ").split()
            if w not in _STOP and (len(w) > 2 or w.isdigit())}


def sentences(text: str) -> List[str]:
    out = []
    for s in _SENTENCE.split(re.sub(r"[ \t]+", " ", text or "")):
        s = s.strip(" -*#>\t")
        if len(s) >= 3:
            out.append(s)
    return out


def _clean_term(term: str) -> str:
    term = re.sub(r"\s+", " ", term or "").strip(" *_:-–—.,;\"'`")
    term = re.sub(r"^(?:the|a|an)\s+", "", term, flags=re.I)
    return term


# --------------------------------------------------------------------------
# the guide
# --------------------------------------------------------------------------


@dataclass
class Concept:
    term: str
    passage: int
    weight: float
    source: str  # heading | bold | definition | glossary | acronym | model
    gist: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"term": self.term, "passage": self.passage,
                "weight": round(self.weight, 2), "source": self.source,
                "gist": self.gist}


@dataclass
class Guide:
    passages: List[str]
    headings: List[str] = field(default_factory=list)
    concepts: List[Concept] = field(default_factory=list)
    provider: str = "rules"
    note: str = ""

    def terms_for(self, index: int, limit: int = 6) -> List[str]:
        hits = [c for c in self.concepts if c.passage == index]
        hits.sort(key=lambda c: -c.weight)
        out: List[str] = []
        for c in hits:
            if c.term.lower() not in (t.lower() for t in out):
                out.append(c.term)
            if len(out) >= limit:
                break
        return out

    def weight(self, index: int) -> float:
        extra = sum(c.weight for c in self.concepts if c.passage == index)
        return BASE_WEIGHT + min(extra, 9.0)

    def heading(self, index: int) -> str:
        return self.headings[index] if 0 <= index < len(self.headings) else ""

    def summary(self, limit: int = 12) -> List[Dict[str, Any]]:
        ranked = sorted(self.concepts, key=lambda c: (-c.weight, c.passage))
        seen, out = set(), []
        for c in ranked:
            key = c.term.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(c.as_dict())
            if len(out) >= limit:
                break
        return out


def headings_for(text: str, passages: Sequence[str]) -> List[str]:
    """The heading in force where each passage starts.

    `generate.chunk` throws headings away — they would otherwise become
    passages of their own — so this finds each passage's opening in the
    original text and walks back to the nearest heading above it.
    """
    text = text or ""
    marks = [(m.start(), m.group(2).strip()) for m in _HEADING.finditer(text)]
    out: List[str] = []
    cursor = 0
    for passage in passages:
        probe = (passage or "").strip()[:40]
        at = text.find(probe, cursor) if probe else -1
        if at < 0:
            at = text.find(probe) if probe else -1
        if at < 0:
            out.append(out[-1] if out else "")
            continue
        cursor = at
        heading = ""
        for pos, title in marks:
            if pos > at:
                break
            heading = title
        out.append(heading)
    return out


def key_terms(passages: Sequence[str], headings: Sequence[str] = ()) -> List[Concept]:
    """The terms an author marked as important, by rule."""
    found: Dict[str, Concept] = {}

    def add(term: str, index: int, weight: float, source: str) -> None:
        term = _clean_term(term)
        if not term or len(term) < 3 or len(term.split()) > MAX_TERM_WORDS:
            return
        if term.lower() in _LABEL_WORDS or norm(term) in _STOP:
            return
        key = term.lower()
        have = found.get(key)
        if have is None or weight > have.weight:
            found[key] = Concept(term=term, passage=index, weight=weight, source=source)

    for i, passage in enumerate(passages):
        for m in _BOLD.finditer(passage):
            add(m.group(1), i, 2.0, "bold")
        flat = re.sub(r"\s+", " ", passage)
        for m in _DEFINITION.finditer(flat):
            term = _clean_term(m.group("term"))
            if _NOT_A_TERM.search(term) or len(term.split()) > MAX_DEFINITION_WORDS:
                continue
            add(term, i, 3.0, "definition")
        for m in _CALLED.finditer(flat):
            add(m.group("term"), i, 3.0, "definition")
        for m in _GLOSSARY.finditer(passage):
            add(m.group("term"), i, 2.5, "glossary")
        for m in _ACRONYM.finditer(passage):
            add(m.group("acr") or m.group("acr2"), i, 2.0, "acronym")

    # A term that recurs across passages is structural, not incidental.
    lowered = [p.lower() for p in passages]
    for concept in found.values():
        # Word-bounded: "Term1" is not mentioned by a passage about "Term10".
        pattern = re.compile(r"(?<!\w)" + re.escape(concept.term.lower()) + r"(?!\w)")
        spread = sum(1 for p in lowered if pattern.search(p))
        if spread > 1:
            concept.weight += min(1.5, 0.5 * (spread - 1))
    return sorted(found.values(), key=lambda c: (c.passage, -c.weight))


MODEL_SYSTEM = """You read the outline of a student's notes and name the key \
ideas a good exam would test. Output JSON only.

Rules:
- Each "term" must be a word or short phrase that appears in the outline or \
is plainly the subject of a passage. Never invent a term.
- "passage" is the [number] of the passage where the idea is explained.
- "importance" is 3 for ideas the rest depends on, 2 for supporting ideas, \
1 for details.
- "why" is one short line on why it matters. No more than 15 words.
- At most 12 ideas. Fewer is fine."""

MODEL_SCHEMA = """{"concepts": [{"term": "...", "passage": 0, "importance": 3, "why": "..."}]}"""


def outline(passages: Sequence[str], headings: Sequence[str]) -> str:
    lines = []
    for i, passage in enumerate(passages):
        head = headings[i] if i < len(headings) else ""
        opening = re.sub(r"\s+", " ", passage).strip()[:OUTLINE_CHARS]
        lines.append(f"[{i}] {head + ' — ' if head else ''}{opening}")
    return "\n".join(lines)


def model_concepts(
    passages: Sequence[str], headings: Sequence[str], provider: Any, subject: str = ""
) -> List[Concept]:
    """One call over the outline. Every term must be findable in the note."""
    head = f'Notes: "{subject}"\n\n' if subject else ""
    raw = provider.complete_json(
        f"{head}Outline:\n{outline(passages, headings)}\n\nReturn JSON.",
        system=MODEL_SYSTEM,
        schema_hint=MODEL_SCHEMA,
    )
    items = raw.get("concepts") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    lowered = [norm(p) for p in passages]
    out: List[Concept] = []
    for item in items[:MAX_MODEL_CONCEPTS]:
        if not isinstance(item, dict):
            continue
        term = _clean_term(str(item.get("term") or ""))
        key = norm(term)
        if not key or len(term.split()) > MAX_TERM_WORDS:
            continue
        where = [i for i, p in enumerate(lowered) if key in p]
        if not where:
            continue  # a concept the note never mentions is the model's, not lj's
        try:
            index = int(item.get("passage"))
        except (TypeError, ValueError):
            index = where[0]
        if index not in where:
            index = where[0]
        try:
            importance = max(1, min(3, int(item.get("importance") or 2)))
        except (TypeError, ValueError):
            importance = 2
        gist = re.sub(r"\s+", " ", str(item.get("why") or "")).strip()[:140]
        out.append(Concept(term=term, passage=index, weight=1.0 + importance,
                           source="model", gist=gist))
    return out


def build(
    text: str,
    passages: Sequence[str],
    *,
    provider: Any = None,
    use_model: bool = False,
    subject: str = "",
) -> Guide:
    """The guide for one note. Never raises: a failed model pass is a
    rules-only guide with a note saying so."""
    passages = list(passages)
    heads = headings_for(text, passages)
    guide = Guide(passages=passages, headings=heads, concepts=key_terms(passages, heads))
    if use_model and provider is not None and getattr(provider, "is_llm", False) and len(passages) > 1:
        try:
            extra = model_concepts(passages, heads, provider, subject)
        except Exception as exc:  # the outline pass is a bonus, never a blocker
            guide.note = f"concept pass skipped ({type(exc).__name__})"
            extra = []
        if extra:
            have = {c.term.lower(): c for c in guide.concepts}
            for c in extra:
                prior = have.get(c.term.lower())
                if prior:
                    prior.weight = max(prior.weight, c.weight) + 0.5
                    prior.gist = prior.gist or c.gist
                else:
                    guide.concepts.append(c)
            guide.provider = getattr(provider, "name", "model")
    return guide


# --------------------------------------------------------------------------
# planning and coverage
# --------------------------------------------------------------------------


def spread_order(n: int) -> List[int]:
    """0..n-1 in an order that covers the range evenly from the first pick.

    Bisection order: 0, n/2, n/4, 3n/4, … A budget that runs out part way
    through still leaves cards spread over the whole note.
    """
    if n <= 0:
        return []
    order: List[int] = []
    seen = set()
    denom = 1
    while len(order) < n:
        for k in range(denom):
            i = int(k * n / denom)
            if i not in seen:
                seen.add(i)
                order.append(i)
        denom *= 2
        if denom > 4 * n:
            break
    order.extend(i for i in range(n) if i not in seen)
    return order


def plan(
    guide: Guide,
    covered: Optional[Dict[int, int]] = None,
    *,
    per_chunk: int = 3,
    skip_covered: bool = False,
) -> List[int]:
    """Which passages to visit, best first.

    Uncovered before covered, then heavier before lighter, then spread order
    — so a note with no marked terms at all is still sampled evenly rather
    than read from the top.
    """
    covered = covered or {}
    rank = {i: r for r, i in enumerate(spread_order(len(guide.passages)))}
    out = []
    for i in range(len(guide.passages)):
        have = covered.get(i, 0)
        if skip_covered and have:
            continue
        if have >= per_chunk:
            continue
        score = guide.weight(i) + (UNCOVERED_BONUS if not have else 0.0)
        out.append((-score, rank[i], i))
    out.sort()
    return [i for _, _, i in out]


def passage_of(card: Any, passages: Sequence[str]) -> Optional[int]:
    """The passage a card was drawn from, or None.

    The citation decides it when there is one. Otherwise the answer — or the
    cloze sentence — is looked for, which is how hand-typed cards count.
    """
    normed = [norm(p) for p in passages]
    probes = []
    source = norm(getattr(card, "source", "") or "")
    if len(source) >= MIN_QUOTE_CHARS:
        probes.append(source)
    front = getattr(card, "front", "") or ""
    if "{{" in front:
        probes.append(norm(front.replace("{{", "").replace("}}", ""))[:120])
    back = norm(getattr(card, "back", "") or "")
    if len(back) >= 6:
        probes.append(back)
    for probe in probes:
        for i, p in enumerate(normed):
            if probe and probe in p:
                return i
    # Trimmed cloze fronts carry an ellipsis; fall back to the longest run.
    if "{{" in front:
        pieces = [norm(x) for x in re.split(r"…|\.\.\.", front.replace("{{", "").replace("}}", ""))]
        pieces = sorted((x for x in pieces if len(x) >= 20), key=len, reverse=True)
        for piece in pieces:
            for i, p in enumerate(normed):
                if piece in p:
                    return i
    return None


def coverage(cards: Iterable[Any], guide: Guide) -> Dict[str, Any]:
    """Which parts of the note have cards, and which key ideas have none."""
    counts: Dict[int, int] = {}
    unplaced = 0
    live = [c for c in cards if getattr(c, "status", "") != "suspended"]
    for card in live:
        where = passage_of(card, guide.passages)
        if where is None:
            unplaced += 1
            continue
        counts[where] = counts.get(where, 0) + 1
    rows = []
    for i, passage in enumerate(guide.passages):
        flat = re.sub(r"\s+", " ", passage).strip()
        rows.append({
            "index": i,
            "heading": guide.heading(i),
            "preview": flat[:90] + ("…" if len(flat) > 90 else ""),
            "cards": counts.get(i, 0),
            "terms": guide.terms_for(i, 4),
        })
    text_of_cards = " ".join(
        norm(f"{getattr(c, 'front', '')} {getattr(c, 'back', '')}") for c in live
    )
    missing = []
    for c in guide.summary(limit=40):
        if counts.get(c["passage"]) or norm(c["term"]) in text_of_cards:
            continue
        missing.append(c["term"])
    total = len(guide.passages)
    done = sum(1 for i in range(total) if counts.get(i))
    return {
        "passages": total,
        "covered": done,
        "share": round(done / total, 3) if total else 0.0,
        "unplaced": unplaced,
        "rows": rows,
        "missing_concepts": missing[:12],
        "concepts": guide.summary(),
    }


def covered_counts(cards: Iterable[Any], passages: Sequence[str]) -> Dict[int, int]:
    counts: Dict[int, int] = {}
    for card in cards:
        where = passage_of(card, passages)
        if where is not None:
            counts[where] = counts.get(where, 0) + 1
    return counts


# --------------------------------------------------------------------------
# grounding
# --------------------------------------------------------------------------


def ground(
    question: str, answer: str, quote: str, passage: str, *, explain: bool = False
) -> Tuple[str, str]:
    """Is this card supported by its passage? Returns (citation, how).

    `how` is "verbatim" when the model's own quote is really there,
    "recovered" when it was not but one sentence of the passage carries the
    answer, and "" when nothing does — the card is then unsupported, and an
    unsupported card is the one thing a spaced-repetition deck must not hold.

    Recovery is what NotebookLM does with every answer: point at the line.
    A small model paraphrases its quote more often than it invents a fact,
    and dropping the citation of a true card was the old behaviour.
    """
    if quote and len(quote.strip()) >= MIN_QUOTE_CHARS and norm(quote) in norm(passage):
        return quote.strip(), "verbatim"

    need = GROUND_SHARE_EXPLAIN if explain else GROUND_SHARE
    target = content_words(answer)
    asked = content_words(question)
    best, best_score = "", 0.0
    for sentence in sentences(passage):
        words = content_words(sentence)
        if target:
            share = len(target & words) / len(target)
        else:
            share = 1.0 if norm(answer) and norm(answer) in norm(sentence) else 0.0
        if share < need:
            continue
        score = share + 0.1 * (len(asked & words) / (len(asked) or 1))
        if score > best_score:
            best, best_score = sentence, score
    if best:
        return best, "recovered"
    # A short exact answer ("1990", "noise") may sit in a sentence that
    # shares nothing else with it.
    flat = norm(answer)
    if flat and len(flat) >= 3:
        for sentence in sentences(passage):
            if re.search(r"\b" + re.escape(flat) + r"\b", norm(sentence)):
                return sentence, "recovered"
    return "", ""
