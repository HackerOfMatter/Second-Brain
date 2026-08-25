"""Note → flashcards.

The NotebookLM half of the tutor: point at something you filed and get a deck
out of it. Same three-stage shape as `parser.py` — rules first, model second,
rules again as a guard — because the failure mode here is worse than a bad
parse. A wrong flashcard does not just sit in a file; spaced repetition will
patiently drill it into you on an optimal schedule. Being confidently wrong is
the one thing this module must not do.

Three defences, in order of how much they catch:

  1. **Chunking.** The source is split into passages before the model sees it,
     and each request covers one passage. A small local model asked to make
     twenty cards from four pages will drift and invent; asked for three cards
     from one paragraph it stays honest.
  2. **Citation.** Every card must quote the sentence from the passage that
     justifies its answer, and that quote is checked against the passage. A
     quote that is not really there is dropped rather than shown — a
     fabricated citation is worse than none, because it looks like evidence.
  3. **Approval.** Everything lands as `draft`. Nothing enters the scheduler
     until lj has read it. The generator's job is to save typing, not to be
     trusted.

When no model is reachable the heuristic path still produces cloze cards from
definition-shaped sentences. Fewer and blunter, but a capture is never lost
because Ollama is closed, and neither is a study session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .cards import CLOZE, Card, Deck, fingerprint
from .config import Config
from .llm import resolve_provider

SYSTEM_PROMPT = """You write flashcards for a spaced-repetition system. \
You are given one passage of the user's own notes and you turn it into cards.

Rules:
- Output JSON only. No prose, no code fence.
- Every card must be answerable from the passage alone. Never use outside \
knowledge, and never write a card the passage does not settle.
- One fact per card. If an answer needs "and", it is two cards.
- The question must make sense on its own, with no "this", "the above", or \
"as mentioned". Someone reading only the question should know what is asked.
- The answer is the shortest complete form: a term, a number, a phrase, one \
sentence at most. Never restate the question.
- "why" must be a short exact quote copied from the passage, word for word, \
that contains the answer. Copy it; do not paraphrase it.
- Never write a card whose answer is the passage's title or the note's name.
- Prefer cards that test understanding — why, when, what happens if — over \
cards that test wording.
- If the passage is boilerplate, navigation, or too thin to test, return an \
empty list. Returning nothing is a correct answer."""

SCHEMA_HINT = """{
  "cards": [
    {"q": "question, self-contained", "a": "shortest complete answer",
     "why": "exact quote from the passage containing the answer"}
  ]
}"""

#: Sections of a rendered note that are the system's own boilerplate, not
#: material worth testing. Generating "What is this project's level?" cards
#: from our own template output is the card-generation version of the colour
#: bug in Revision 1, and the fix is the same: exclude what we wrote ourselves.
SKIP_HEADINGS = {
    "steps",
    "materials",
    "hardware",
    "software",
    "skills",
    "check-in log",
    "cards",
}

MAX_CHUNK_CHARS = 1100
MIN_CHUNK_CHARS = 120

#: `[[Note]]`, `[[Note|shown]]`, `[[Note#Heading]]` — capture the note title.
WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")

#: How many linked notes one generation may pull in. A Syllabus links a whole
#: term; expanding all of it would bury the note actually being asked about.
MAX_LINKED_NOTES = 40


def expand_links(text: str, resolve: Any, *, max_notes: int = MAX_LINKED_NOTES) -> str:
    """Append the bodies of `[[linked]]` notes to the source material.

    A deck is generated from one note's own text, and the notes this system
    produces are deliberately thin indexes: an Assignment's questions and a
    Quiz's concepts are links, and the substance lives in the atomic notes
    they point at. Without this, generating cards from a Quiz reads a page of
    link syntax and correctly concludes there is nothing worth testing.

    Deliberately **one level deep**. Atomic notes cite each other, so
    following links transitively turns "make cards from this quiz" into "make
    cards from the whole vault", and a card drawn from a note two hops away
    is no longer traceable to anything lj thinks they are studying.
    """
    text = text or ""
    seen: set = set()
    parts = [text]
    for match in WIKILINK.finditer(text):
        title = match.group(1).strip()
        key = title.lower()
        if not title or key in seen:
            continue
        seen.add(key)
        if len(seen) > max_notes:
            break
        try:
            linked = resolve(title)
        except Exception:
            continue  # a broken link is a missing passage, not a failed run
        if linked and linked.strip():
            parts.append(f"## {title}\n\n{_strip_title(linked, title)}")
    return "\n\n".join(parts)


def _strip_title(body: str, title: str) -> str:
    """Drop a linked note's own `# Title` line — we just wrote the heading."""
    first, _, rest = body.lstrip().partition("\n")
    if first.strip().lstrip("#").strip().lower() == title.strip().lower():
        return rest
    return body


@dataclass
class GenerationResult:
    cards: List[Card] = field(default_factory=list)
    provider: str = ""
    degraded: bool = False
    chunks: int = 0
    rejected: int = 0
    note: str = ""


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def chunk(text: str) -> List[str]:
    """Split source material into passages a small model can hold at once.

    Headings start a new passage, paragraphs pack up to a size cap, and the
    system's own rendered sections are dropped. Short trailing fragments are
    merged backwards so a stray line does not become its own request.
    """
    text = _strip_frontmatter(text or "")
    passages: List[str] = []
    current: List[str] = []
    skipping = False

    def flush() -> None:
        if current:
            joined = "\n\n".join(current).strip()
            if len(joined) >= MIN_CHUNK_CHARS:
                passages.append(joined)
            elif passages and len(passages[-1]) + len(joined) < MAX_CHUNK_CHARS:
                passages[-1] = passages[-1] + "\n\n" + joined
            elif joined:
                passages.append(joined)
            current.clear()

    for block in re.split(r"\n\s*\n", text):
        block = block.strip()
        if not block:
            continue
        heading = re.match(r"^#{1,6}\s+(.*)$", block)
        if heading:
            flush()
            skipping = heading.group(1).strip().lower().rstrip(":") in SKIP_HEADINGS
            continue
        if skipping:
            continue
        if sum(len(b) for b in current) + len(block) > MAX_CHUNK_CHARS:
            flush()
        current.append(block)
    flush()
    return [p for p in passages if len(p.strip()) >= 60]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :]
    return text


# --------------------------------------------------------------------------
# generation
# --------------------------------------------------------------------------


def generate(
    source: str,
    cfg: Config,
    *,
    subject: str = "",
    max_cards: int = 20,
    per_chunk: int = 3,
    existing: Optional[Sequence[Card]] = None,
) -> GenerationResult:
    """Draft cards from `source`. Nothing here writes to disk."""
    passages = chunk(source)
    seen = {_norm(c.front) for c in (existing or [])}
    result = GenerationResult(chunks=len(passages))

    if not passages:
        result.note = "Nothing to make cards from — the note has no prose yet."
        result.degraded = True
        return result

    provider = resolve_provider(cfg.llm, "generate")
    result.provider = provider.name
    if not getattr(provider, "is_llm", False):
        result.degraded = True
        result.cards = _heuristic_cards(passages, max_cards, seen)
        result.note = (
            "No model reachable — made cloze cards from definition sentences. "
            "Re-generate later for better questions."
        )
        return result

    for passage in passages:
        if len(result.cards) >= max_cards:
            break
        try:
            raw = provider.complete_json(
                _user_prompt(passage, subject, per_chunk),
                system=SYSTEM_PROMPT,
                schema_hint=SCHEMA_HINT,
            )
        except Exception as exc:
            result.note = f"{type(exc).__name__} on one passage; kept what was made."
            continue
        for item in _coerce_cards(raw):
            if len(result.cards) >= max_cards:
                break
            card = _validate(item, passage, subject)
            if card is None:
                result.rejected += 1
                continue
            key = _norm(card.front)
            if key in seen:
                result.rejected += 1
                continue
            seen.add(key)
            result.cards.append(card)

    if not result.cards and not result.note:
        result.note = "The model returned nothing usable from this note."
    return result


def _user_prompt(passage: str, subject: str, per_chunk: int) -> str:
    head = f'These are notes from "{subject}".\n\n' if subject else ""
    return (
        f"{head}Passage:\n\"\"\"\n{passage.strip()}\n\"\"\"\n\n"
        f"Write at most {per_chunk} flashcards testing what this passage "
        f"actually says. Fewer is better than padded. Return JSON."
    )


def _coerce_cards(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("cards", "flashcards", "items", "questions"):
            if isinstance(raw.get(key), list):
                return [c for c in raw[key] if isinstance(c, dict)]
        # a bare single card
        if raw.get("q") or raw.get("question"):
            return [raw]
    if isinstance(raw, list):
        return [c for c in raw if isinstance(c, dict)]
    return []


# --------------------------------------------------------------------------
# validation — the guard rail
# --------------------------------------------------------------------------


def _validate(item: Dict[str, Any], passage: str, subject: str) -> Optional[Card]:
    front = _text(item.get("q") or item.get("question") or item.get("front"))
    back = _text(item.get("a") or item.get("answer") or item.get("back"))
    why = _text(item.get("why") or item.get("source") or item.get("quote"))

    if not front or not back:
        return None
    if len(front) < 8 or len(front) > 320 or len(back) > 400:
        return None
    if _norm(front) == _norm(back):
        return None
    # An answer that is just the note's name tests nothing.
    if subject and _norm(back) == _norm(subject):
        return None
    # "As mentioned above" questions are unanswerable outside their passage.
    if re.search(r"\b(the above|as mentioned|this passage|the text|the note)\b", front, re.I):
        return None
    if not re.search(r"[?？]$", front) and not CLOZE.search(front):
        front = front.rstrip(".") + "?"

    return Card(
        id="tmp",
        front=front,
        back=back,
        source=why if _quote_is_real(why, passage) else "",
        status="draft",
    )


def _quote_is_real(quote: str, passage: str) -> bool:
    """Is this quote actually in the passage?

    Compared on collapsed whitespace and case, because a model will reflow a
    line it copied faithfully. A quote that fails this check is dropped, not
    kept with a caveat: a citation that points nowhere is worse than no
    citation, because it reads as evidence.
    """
    if not quote or len(quote) < 12:
        return False
    return _norm(quote) in _norm(passage)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip().strip('"').strip()


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", re.sub(r"\s+", " ", (text or "").lower())).strip()


# --------------------------------------------------------------------------
# the no-model path
# --------------------------------------------------------------------------

#: Sentence shapes that carry a definition worth blanking out.
DEFINITION = re.compile(
    r"^(?P<term>[A-Z][^.!?]{2,60}?)\s+(?:is|are|means|refers to|stands for)\s+(?P<rest>.{15,220}?[.!?])",
    re.S,
)


def _heuristic_cards(passages: List[str], max_cards: int, seen: set) -> List[Card]:
    """Cloze cards from definition-shaped sentences.

    Deliberately unambitious. It exists so that "Ollama is closed" degrades to
    a smaller deck rather than to no deck, and every card it makes is a
    verbatim sentence from the note, so it cannot invent anything.
    """
    out: List[Card] = []
    for passage in passages:
        for sentence in re.split(r"(?<=[.!?])\s+", passage):
            if len(out) >= max_cards:
                return out
            sentence = re.sub(r"\s+", " ", sentence).strip(" -*#>")
            m = DEFINITION.match(sentence)
            if not m:
                continue
            term, rest = m.group("term").strip(), m.group("rest").strip()
            if len(term) < 3 or _norm(term) in seen:
                continue
            seen.add(_norm(term))
            out.append(
                Card(
                    id="tmp",
                    front=f"{{{{{term}}}}} {sentence[len(term):].strip()}",
                    back=term,
                    source=sentence,
                    status="draft",
                )
            )
    return out


# --------------------------------------------------------------------------
# deck-level entry point
# --------------------------------------------------------------------------


def add_to_deck(
    deck: Deck, source: str, cfg: Config, *, max_cards: int = 20
) -> GenerationResult:
    """Generate and append, assigning real ids and skipping duplicates."""
    result = generate(
        source,
        cfg,
        subject=deck.subject,
        max_cards=max_cards,
        existing=deck.cards,
    )
    attached: List[Card] = []
    for card in result.cards:
        attached.append(
            deck.add(
                front=card.front,
                back=card.back,
                hint=card.hint,
                source=card.source,
                status="draft",
            )
        )
    result.cards = attached
    deck.source_fingerprint = fingerprint(source)
    return result
