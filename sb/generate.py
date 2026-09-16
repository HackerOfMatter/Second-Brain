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
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import frontmatter, guide as guidemod, quality
from .cards import CLOZE, Card, Deck, fingerprint
from .config import Config
from .llm import resolve_provider

SYSTEM_PROMPT = """You write flashcards for a spaced-repetition system. \
You are given one passage of the user's own notes and you turn it into cards.

Rules:
- Output JSON only. No prose, no code fence.
- Every card must be answerable from the passage alone. Never use outside \
knowledge, and never write a card the passage does not settle.
- One fact per card. If an answer needs "and", it is two cards. Never \
answer with a list, a set, or an enumeration — those are the least learnable \
cards there are.
- The question must make sense on its own, with no "this", "the above", or \
"as mentioned". Someone reading only the question should know what is asked.
- The answer is the shortest complete form: a term, a number, or a short \
phrase — under 15 words, and one sentence at most. Never restate the question.
- "why" must be a short exact quote copied from the passage, word for word, \
that contains the answer. Copy it; do not paraphrase it.
- Never write a card whose answer is the passage's title or the note's name.
- Prefer cards that test understanding — why, when, what happens if — over \
cards that test wording.
- "type" is two or three words naming the *kind* of question this is within \
the subject — "elasticity", "unit conversion", "date", "definition". Reuse the \
same label for cards of the same kind. It is used to mix question types \
within a session, so a generic label is worse than none.
- If the passage is boilerplate, navigation, or too thin to test, return an \
empty list. Returning nothing is a correct answer.
- When you are told the key ideas of a passage, test those first — they are \
what the whole note is built around. Name the idea in the question itself; \
never write "this chapter", "this section" or "the reading".

"format" is how the card should be asked. Pick the one the material calls for:
- "basic" — the default. Read the question, recall the answer, grade yourself.
- "short" — the answer is one exact term, number or name worth *producing* \
from memory rather than recognising. Use it where being able to say the word \
is the skill.
- "choice" — multiple choice. Use it ONLY when the passage itself supports \
three wrong answers that are specific, plausible and about this material. Put \
them in "wrong". Never write "all of the above" or "none of the above", never \
a wrong answer that is obviously silly, and never make the right answer the \
longest or the most qualified one — that gives it away without reading the \
question. If you cannot write three real wrong answers from this passage, use \
"basic" instead. At most one card in three should be "choice".
- "explain" — the question asks *why* or *how* something works and the answer \
is a short explanation, not a term. Use it for mechanisms, causes and \
consequences. Keep the explanation under 40 words."""

SCHEMA_HINT = """{
  "cards": [
    {"q": "question, self-contained", "a": "shortest complete answer",
     "why": "exact quote from the passage containing the answer",
     "type": "two or three words naming the kind of question",
     "format": "basic | short | choice | explain",
     "wrong": ["only for format=choice: three plausible wrong answers"]}
  ]
}"""

#: Recognition is a weaker test than recall, so a deck made mostly of choice
#: cards is a deck that feels easy and measures little. Cards past this share
#: keep their question and lose their options.
MAX_CHOICE_SHARE = 1 / 3

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
    #: Cards the quality pass rewrote rather than threw away (sb/quality.py).
    repaired: int = 0
    #: Why cards were dropped, by rule. Surfaced so a deck that comes back
    #: thin says which rule ate it instead of looking like a model failure.
    rejections: Dict[str, int] = field(default_factory=dict)
    #: How many of each kind were made — basic, cloze, mcq, recall, explain.
    #: A deck that came back all-basic because every option set failed its
    #: gate should say so rather than look like the model never tried.
    kinds: Dict[str, int] = field(default_factory=dict)
    #: Choice cards that lost their options, by the rule that took them.
    downgraded: Dict[str, int] = field(default_factory=dict)
    #: How each kept card is supported — "verbatim" when the model's own
    #: quote is in the passage, "recovered" when a passage sentence carrying
    #: the answer was found instead (sb/guide.ground). Unsupported cards are
    #: dropped under the rule "ungrounded".
    grounding: Dict[str, int] = field(default_factory=dict)
    #: The study guide's key ideas, heaviest first (sb/guide.py).
    concepts: List[Dict[str, Any]] = field(default_factory=list)
    #: Passage indexes visited, in the order they were asked about.
    visited: List[int] = field(default_factory=list)
    #: Which passages have cards once this run is attached (add_to_deck).
    coverage: Dict[str, Any] = field(default_factory=dict)
    guide: Any = None
    note: str = ""

    def _drop(self, rule: str) -> None:
        self.rejected += 1
        self.rejections[rule] = self.rejections.get(rule, 0) + 1

    def _count(self, card: Card) -> None:
        self.kinds[card.kind] = self.kinds.get(card.kind, 0) + 1


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def chunk(text: str) -> List[str]:
    """Split source material into passages a small model can hold at once.

    Headings start a new passage, paragraphs pack up to a size cap, and the
    system's own rendered sections are dropped. Short trailing fragments are
    merged backwards so a stray line does not become its own request.
    """
    text = frontmatter.strip(text or "")
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
    fill_gaps: bool = False,
    focus: str = "",
) -> GenerationResult:
    """Draft cards from `source`. Nothing here writes to disk.

    `fill_gaps` visits only passages no existing card was drawn from.
    `focus` is an extra instruction for every passage — the leech rewrite
    uses it to say which card keeps failing.
    """
    max_answer_words = (
        cfg.study.max_answer_words if getattr(cfg.study, "enforce_card_quality", True) else 0
    )
    require_grounding = bool(getattr(cfg.study, "require_grounding", True))
    passages = chunk(source)
    seen = {_norm(c.front) for c in (existing or [])}
    result = GenerationResult(chunks=len(passages))

    if not passages:
        result.note = "Nothing to make cards from — the note has no prose yet."
        result.degraded = True
        return result

    choices_made = 0
    provider = resolve_provider(cfg.llm, "generate")
    result.provider = provider.name

    # NotebookLM's move: read the whole note before writing a question.
    study_guide = guidemod.build(
        frontmatter.strip(source or ""),
        passages,
        provider=provider,
        use_model=bool(getattr(cfg.study, "study_guide", True)),
        subject=subject,
    )
    result.guide = study_guide
    result.concepts = study_guide.summary()
    order = guidemod.plan(
        study_guide,
        guidemod.covered_counts(existing or [], passages),
        per_chunk=per_chunk,
        skip_covered=fill_gaps,
    )
    if not order:
        result.note = (
            "Every passage already has cards — nothing left to fill."
            if fill_gaps else "Every passage already has its share of cards."
        )
        return result

    if not getattr(provider, "is_llm", False):
        result.degraded = True
        result.visited = list(order)
        result.cards = _heuristic_cards([passages[i] for i in order], max_cards, seen)
        for card in result.cards:
            result._count(card)
        result.note = (
            "No model reachable — made cloze cards from definition sentences. "
            "Re-generate later for better questions."
        )
        return result

    for index in order:
        if len(result.cards) >= max_cards:
            break
        passage = passages[index]
        result.visited.append(index)
        try:
            raw = provider.complete_json(
                _user_prompt(
                    passage, subject, per_chunk,
                    heading=study_guide.heading(index),
                    terms=study_guide.terms_for(index),
                    focus=focus,
                ),
                system=SYSTEM_PROMPT,
                schema_hint=SCHEMA_HINT,
            )
        except Exception as exc:
            result.note = f"{type(exc).__name__} on one passage; kept what was made."
            continue
        for item in _coerce_cards(raw):
            if len(result.cards) >= max_cards:
                break
            made, rule = _validate(
                item, passage, subject,
                max_answer_words=max_answer_words,
                require_grounding=require_grounding,
                grounding=result.grounding,
            )
            if not made:
                result._drop(rule or "unusable")
                continue
            if rule == "repaired":
                result.repaired += 1
            elif rule.startswith("choice-"):
                # The option set failed its gate and the question survived it.
                result.downgraded[rule] = result.downgraded.get(rule, 0) + 1
            for card in made:
                if len(result.cards) >= max_cards:
                    break
                key = _norm(card.front)
                if key in seen:
                    result._drop("duplicate")
                    continue
                # Recognition is cheaper than recall, so the share is capped
                # here rather than trusted to the prompt. Over the cap a
                # choice card keeps its question and loses its options.
                if card.choices and choices_made >= max(1, int(max_cards * MAX_CHOICE_SHARE)):
                    card.choices = []
                    result.downgraded["choice-over-share"] = (
                        result.downgraded.get("choice-over-share", 0) + 1
                    )
                if card.choices:
                    choices_made += 1
                seen.add(key)
                result._count(card)
                result.cards.append(card)

    if not result.cards and not result.note:
        result.note = "The model returned nothing usable from this note."
    return result


def _user_prompt(
    passage: str,
    subject: str,
    per_chunk: int,
    *,
    heading: str = "",
    terms: Sequence[str] = (),
    focus: str = "",
) -> str:
    head = f'These are notes from "{subject}".\n' if subject else ""
    if heading:
        head += f'Section: "{heading}"\n'
    if terms:
        head += "Key ideas in this passage: " + "; ".join(terms) + "\n"
    if focus:
        head += focus.strip() + "\n"
    head = head + "\n" if head else ""
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


def _validate(
    item: Dict[str, Any],
    passage: str,
    subject: str,
    *,
    max_answer_words: int = quality.MAX_ANSWER_WORDS,
    require_grounding: bool = True,
    grounding: Optional[Dict[str, int]] = None,
) -> Tuple[List[Card], str]:
    """One model card in, zero to four real cards out.

    Returns the cards and a tag: `""` when the card passed as written,
    `"repaired"` when the quality pass rewrote it, and the broken rule's name
    when nothing survived. The caller counts the tags so a thin deck can say
    *why* it is thin.
    """
    front = _text(item.get("q") or item.get("question") or item.get("front"))
    back = _text(item.get("a") or item.get("answer") or item.get("back"))
    why = _text(item.get("why") or item.get("source") or item.get("quote"))

    if not front or not back:
        return [], "empty"
    if len(front) < 8 or len(front) > 320 or len(back) > 400:
        return [], "length"
    if _norm(front) == _norm(back):
        return [], "restates-question"
    # An answer that is just the note's name tests nothing.
    if subject and _norm(back) == _norm(subject):
        return [], "title-as-answer"
    # "As mentioned above" questions are unanswerable outside their passage.
    if re.search(
        r"\b(the above|as mentioned|this passage|the text|the note|"
        r"this (?:chapter|section|reading|lesson)|the reading)\b", front, re.I
    ):
        return [], "not-self-contained"
    if not re.search(r"[?？]$", front) and not CLOZE.search(front):
        front = front.rstrip(".") + "?"

    topic = _topic(item)
    fmt = _format(item)

    if require_grounding:
        # NotebookLM's rule: every answer points at a line of the source. A
        # card that cannot is dropped, not shipped without its citation —
        # the old behaviour, which kept exactly the cards most likely to be
        # the model's rather than the note's.
        if CLOZE.search(front):
            asked, answered = "", CLOZE.sub(lambda m: m.group(1), front)
        else:
            asked, answered = front, back
        quote, how = guidemod.ground(asked, answered, why, passage, explain=(fmt == "explain"))
        if not how:
            return [], "ungrounded"
        if grounding is not None:
            grounding[how] = grounding.get(how, 0) + 1
    else:
        quote = why if _quote_is_real(why, passage) else ""

    def card(f: str, b: str, src: str, *, choices=None, mode: str = "") -> Card:
        return Card(
            id="tmp", front=f, back=b, source=src, topic=topic, status="draft",
            choices=list(choices or []), mode=mode,
        )

    # A choice card is decided before anything else, because its whole shape
    # is different: the answer is *shown*, so the minimum-information rules
    # about answer length are not the ones that matter. What matters is
    # whether the options measure anything, and that is `assess_choices`.
    if fmt == "choice":
        options = _options(item, back)
        verdict = quality.assess_choices(back, options)
        if verdict.ok:
            return [card(front, back, quote, choices=options)], ""
        # A bad option set is not a bad card. The question and the answer came
        # through the same checks every other card gets, so the options are
        # dropped and the card ships as a plain one — losing a good question
        # because its distractors were lazy is a worse trade than the one card
        # of recognition practice it costs.
        fmt = "basic"
        downgrade = verdict.rule
    else:
        downgrade = ""

    # An explanation is longer than a term by definition, so the answer-length
    # and enumeration rules would reject every one of them. The rules that
    # still apply are the ones about what a question may ask for.
    if fmt == "explain":
        if quality.is_list_question(front):
            return [], "list-question"
        if quality.words(back) > quality.MAX_EXPLAIN_WORDS:
            return [], "length"
        if quality.words(back) < 4:
            # "Why does X happen?" answered in two words is a basic card that
            # called itself an explanation.
            return [card(front, back, quote)], downgrade
        return [card(front, back, quote, mode="explain")], downgrade

    mode = "recall" if fmt == "short" else ""

    if max_answer_words <= 0:  # quality enforcement switched off in config
        return [card(front, back, quote, mode=mode)], downgrade

    # Rule 5 first: a wordy definition is better asked as a blank than as a
    # question, and converting it usually also fixes the length.
    if quality.words(back) >= 8:
        clozed = quality.to_cloze(front, back, quote, passage)
        if clozed:
            return [card(*clozed)], "repaired"

    verdict = quality.assess(front, back, max_words=max_answer_words)
    if verdict.ok:
        return [card(front, back, quote, mode=mode)], downgrade

    # Rules 7 and 8: a list answer becomes one cloze per item, over the
    # note's own sentence. Unciteable means unrepairable.
    if verdict.rule in ("enumeration", "set", "list-question"):
        split = quality.split_to_cloze(back, quote, passage)
        if split:
            return [card(*triple) for triple in split], "repaired"

    return [], verdict.rule


#: What the model may call a card. Anything else is a basic card, because a
#: format nobody implements must not change how the card is asked.
FORMATS = ("basic", "short", "choice", "explain")


def _format(item: Dict[str, Any]) -> str:
    raw = _text(item.get("format") or item.get("kind") or item.get("style")).lower()
    raw = raw.strip().strip(".")
    aliases = {
        "multiple choice": "choice", "multiple-choice": "choice", "mcq": "choice",
        "recall": "short", "free recall": "short", "term": "short",
        "why": "explain", "explanation": "explain", "reasoning": "explain",
        "qa": "basic", "q&a": "basic", "": "basic",
    }
    raw = aliases.get(raw, raw)
    return raw if raw in FORMATS else "basic"


def _options(item: Dict[str, Any], answer: str) -> List[str]:
    """The option list for a choice card.

    A model may hand back either the wrong answers alone or the whole list
    with the answer somewhere inside it. Both are accepted; what is *not*
    accepted is a list missing its answer, so the answer is appended when it
    is not already there. `quality.assess_choices` then decides whether any of
    it is worth showing.
    """
    raw = item.get("wrong")
    if not isinstance(raw, list):
        raw = item.get("distractors")
    full = False
    if not isinstance(raw, list):
        raw = item.get("options") or item.get("choices")
        full = True
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    for value in raw:
        text = _text(value)
        if text and not any(_norm(text) == _norm(o) for o in out):
            out.append(text)
    answer = _text(answer)
    if answer and not any(_norm(answer) == _norm(o) for o in out):
        out.append(answer)
    return out[: quality.MAX_OPTIONS + 1]


#: A label this generic tells the interleaver nothing, so it is dropped
#: rather than kept — a lane called "concept" holding every card is the same
#: as no lanes at all.
USELESS_TOPICS = {
    "general", "concept", "fact", "misc", "other", "knowledge", "question",
    "recall", "note", "notes", "topic", "info", "information",
}


def _topic(item: Dict[str, Any]) -> str:
    """The problem-type label, normalised, or "".

    Rohrer & Taylor's effect needs the *kinds* to be distinguishable; a label
    that is a whole sentence is a label the model invented per-card and will
    never reuse, so it cannot group anything.
    """
    raw = _text(item.get("type") or item.get("topic") or item.get("kind"))
    raw = raw.strip().strip(".").lower()
    if not raw or len(raw.split()) > 4 or raw in USELESS_TOPICS:
        return ""
    return raw


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
            # A definition long enough to be a paragraph is not one card
            # either, even when every word of it came from the note.
            if quality.words(sentence) > 45:
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
    deck: Deck,
    source: str,
    cfg: Config,
    *,
    max_cards: int = 20,
    fill_gaps: bool = False,
    focus: str = "",
) -> GenerationResult:
    """Generate and append, assigning real ids and skipping duplicates."""
    result = generate(
        source,
        cfg,
        subject=deck.subject,
        max_cards=max_cards,
        per_chunk=max(1, int(getattr(cfg.study, "generate_per_passage", 3) or 3)),
        existing=deck.cards,
        fill_gaps=fill_gaps,
        focus=focus,
    )
    attached: List[Card] = []
    for card in result.cards:
        attached.append(
            deck.add(
                front=card.front,
                back=card.back,
                hint=card.hint,
                source=card.source,
                topic=card.topic,
                worked=card.worked,
                choices=list(card.choices),
                mode=card.mode,
                status="draft",
            )
        )
    result.cards = attached
    if result.guide is not None:
        result.coverage = guidemod.coverage(deck.cards, result.guide)
    deck.source_fingerprint = fingerprint(source)
    return result
