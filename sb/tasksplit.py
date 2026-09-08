"""One prompt → several Projects.

The capture box takes one thought and makes one note, which is right until the
thought arrives the way thoughts actually arrive:

    finish the marketing case by Tuesday, study for the finance midterm
    Friday, and call the bank about the account

That is three Projects with three deadlines and three step lists, and typing it
three times is exactly the friction that makes a capture box go unused. This
module finds the seams.

## Why it is conservative

A missed split costs one edit. A **wrong** split costs three: a Project that
should not exist, a calendar block for it, and a deadline reminder for work
nobody planned. The dashboard's whole value is that everything on it is real,
so the bias here runs hard toward under-splitting, and every strategy has to
clear a bar before it fires:

  1. **Bullets and lines.** Someone who typed a list already did the splitting.
     Highest confidence, and by far the most common real shape.
  2. **Clauses.** One sentence, split on `;`, `, and`, `then`, `also`. Only
     fires when *each* side carries its own action verb or its own date —
     "call the bank and ask about fees" is one task with two verbs about one
     object, and it must not become two.
  3. **The model.** Free-form prose. It is asked to return the words it split
     on, and every returned task is checked back against the input: a task
     whose text is not really in what you typed is dropped, the same defence
     `generate.py` uses for citations. A model cannot invent you a deadline.
  4. **Whole.** One task. Always available, and what the three above fall back
     to.

Nothing here parses dates, estimates or steps — that is `parser.apply_to_note`
running on each task's own text, which is the point of returning verbatim
clauses rather than summaries.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .extract import derive_title, parse_deadline_guess

#: A clause shorter than this is a fragment, not a task.
MIN_TASK_WORDS = 2

#: A "task" longer than this is a paragraph that happens to contain a comma.
#: Above it, clause splitting does not fire at all.
MAX_CLAUSE_WORDS = 40

#: A line longer than this is prose, not a list item.
MAX_LINE_WORDS = 30

#: Never return more than this from one capture. A paste that looks like
#: forty tasks is a document, and it belongs in the segmenter.
MAX_TASKS = 25

BULLET = re.compile(r"^\s*(?:[-*+•‣]|\d{1,3}[.)]|\(\d{1,3}\)|\[[ xX]\])\s+(?=\S)")
CONTINUATION = re.compile(r"^\s{2,}\S")

#: Where a sentence may be cut. Ordered longest-first so `, and then` is
#: consumed before `, and`.
SEPARATORS = re.compile(
    r"""
    \s*;\s*
    | ,?\s+and\s+then\s+
    | ,?\s+then\s+
    | ,\s+and\s+also\s+
    | ,\s+also\s+
    | ,\s+and\s+
    | ,\s+plus\s+
    | \s+&\s+
    """,
    re.I | re.X,
)

#: Verbs that start a task someone captured. Deliberately a closed list: a
#: part-of-speech tagger would be a dependency, and the failure it prevents —
#: splitting "bread and butter" into two projects — is prevented just as well
#: by requiring a word from this list.
ACTION_VERBS = {
    "add", "ask", "book", "build", "buy", "call", "cancel", "check", "clean",
    "code", "compare", "compile", "complete", "confirm", "cook", "create",
    "debug", "deploy", "design", "do", "download", "draft", "email", "fill",
    "find", "finish", "fix", "follow", "get", "go", "grade", "hand", "install",
    "learn", "look", "mail", "make", "meet", "memorize", "message", "move",
    "order", "organize", "outline", "pack", "pay", "phone", "pick", "plan",
    "post", "practice", "practise", "prep", "prepare", "print", "publish",
    "read", "record", "refactor", "register", "rehearse", "remind", "renew",
    "reply", "research", "return", "review", "revise", "rewrite", "run",
    "schedule", "send", "set", "ship", "shop", "sign", "start", "study",
    "submit", "summarize", "summarise", "sync", "take", "talk", "teach",
    "test", "text", "train", "update", "upload", "verify", "visit", "watch",
    "work", "write",
}

#: Phrases that carry an obligation even without an imperative verb —
#: "the essay is due Friday", "I need the slides by noon".
OBLIGATION = re.compile(
    r"\b(?:need to|needs to|have to|has to|must|should|due|deadline|by tomorrow"
    r"|todo|to-do)\b",
    re.I,
)


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class TaskSpec:
    """One Project-to-be. `text` is verbatim — the parser reads it next."""

    text: str
    title: str = ""
    ordinal: int = 0
    boundary: str = "whole"
    #: A deadline phrase from a shared preamble ("Due Friday: A, B, C"), used
    #: only by tasks that carry no date of their own.
    inherited_date: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "title": self.title,
            "ordinal": self.ordinal,
            "boundary": self.boundary,
            "inherited_date": self.inherited_date,
        }


@dataclass
class SplitResult:
    tasks: List[TaskSpec] = field(default_factory=list)
    strategy: str = "whole"
    provider: str = ""
    degraded: bool = False
    note: str = ""
    preamble: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "tasks": [t.as_dict() for t in self.tasks],
            "strategy": self.strategy,
            "provider": self.provider,
            "degraded": self.degraded,
            "note": self.note,
            "preamble": self.preamble,
            "count": len(self.tasks),
        }


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------


def split_tasks(text: str, cfg: Any = None, *, allow_model: bool = True) -> SplitResult:
    """Find the separate tasks in one capture. Writes nothing."""
    raw = (text or "").strip()
    result = SplitResult()
    if not raw:
        return result

    preamble, rest = _peel_preamble(raw)
    result.preamble = preamble
    inherited = _date_phrase(preamble)

    parts = _by_lines(rest)
    strategy = "lines"
    if not parts:
        parts = _by_clauses(rest)
        strategy = "clauses"
    if not parts and allow_model and cfg is not None:
        parts = _by_model(rest, cfg, result)
        strategy = "model"

    if not parts:
        result.tasks = [TaskSpec(text=raw, title=derive_title(raw), ordinal=1, boundary="whole")]
        result.strategy = "whole"
        if not result.note:
            result.note = "One task — nothing in this reads as a separate job."
        return result

    result.strategy = strategy
    result.tasks = _finish(parts, strategy, inherited, preamble)
    if len(result.tasks) == 1:
        # Everything but one clause failed its guard, so the split never
        # really happened; say so rather than claiming a strategy.
        result.strategy = "whole"
        result.tasks[0].boundary = "whole"
    return result


def _finish(
    parts: Sequence[str], strategy: str, inherited: str, preamble: str
) -> List[TaskSpec]:
    out: List[TaskSpec] = []
    seen: set = set()
    for part in parts[:MAX_TASKS]:
        body = _strip_connective(part.strip().strip("-•*").strip())
        if len(body.split()) < MIN_TASK_WORDS:
            continue
        key = re.sub(r"[^a-z0-9 ]+", "", body.lower()).strip()
        if key in seen:
            continue
        seen.add(key)
        out.append(
            TaskSpec(
                text=body,
                title=derive_title(body),
                boundary=strategy,
                inherited_date="" if _date_phrase(body) else inherited,
            )
        )
    for i, task in enumerate(out, start=1):
        task.ordinal = i
    return out


# --------------------------------------------------------------------------
# the preamble
# --------------------------------------------------------------------------


def _peel_preamble(text: str) -> tuple:
    """Split off a leading `Due Friday:` / `This week:` header line.

    Returns `(preamble, rest)`. The preamble is not a task — it is context,
    and most usefully a date every task below it inherits. If peeling it would
    leave nothing, it was the whole capture and stays put.
    """
    first, _, remainder = text.partition("\n")
    if remainder.strip() and first.strip().endswith(":") and len(first.split()) <= 10:
        return first.strip().rstrip(":").strip(), remainder.strip()
    m = re.match(r"^([^:\n]{3,60}):\s+(.+)$", text, re.S)
    if m and len(m.group(1).split()) <= 8 and not BULLET.match(m.group(1)):
        head, body = m.group(1).strip(), m.group(2).strip()
        # Only a real header — "http://x: y" and "Note: see above" are not.
        if _date_phrase(head) or head.lower().rstrip("s") in (
            "todo", "to-do", "task", "this week", "today", "tomorrow", "agenda"
        ):
            return head, body
    return "", text


def _date_phrase(text: str) -> str:
    guess = parse_deadline_guess(text or "")
    return guess.phrase if guess else ""


# --------------------------------------------------------------------------
# strategy 1 — bullets and lines
# --------------------------------------------------------------------------


def _by_lines(text: str) -> List[str]:
    """One task per bullet, or per line when the lines look like a list.

    Indented continuation lines belong to the item above them, so a bullet
    with a note under it stays one task.
    """
    lines = [l for l in text.split("\n")]
    items: List[str] = []
    current: List[str] = []
    bulleted = 0

    for line in lines:
        if not line.strip():
            continue
        if BULLET.match(line):
            bulleted += 1
            if current:
                items.append("\n".join(current))
            current = [BULLET.sub("", line, count=1).strip()]
        elif current and CONTINUATION.match(line):
            current.append(line.strip())
        else:
            if current:
                items.append("\n".join(current))
            current = [line.strip()]
    if current:
        items.append("\n".join(current))

    if len(items) < 2:
        return []
    if bulleted >= 2:
        return items
    # Unbulleted lines are only a task list when they read like one: short,
    # and at least two of them carrying an action. A pasted paragraph that
    # happens to be hard-wrapped must not become six Projects.
    if any(len(i.split()) > MAX_LINE_WORDS for i in items):
        return []
    if sum(1 for i in items if _is_actionable(i)) < max(2, len(items) - 1):
        return []
    return items


#: Words a clause carries over from the separator that cut it.
LEADING_CONNECTIVE = re.compile(r"^(?:and|then|also|plus|next|after that)\s+", re.I)


def _strip_connective(text: str) -> str:
    return LEADING_CONNECTIVE.sub("", (text or "").strip()).strip()


def _comma_split(part: str) -> List[str]:
    """Cut a clause at a comma, but only where a new imperative starts.

    "finish the case by Tuesday, study for the midterm Friday" is two jobs;
    "buy eggs, milk and bread" is one. The difference is whether every piece
    begins with a verb, and that has to be *every* piece, not most of them.

    Absorbing the odd non-verb piece back into its neighbour looked reasonable
    and was wrong: it turned "learn Rust generics by next Friday, ~4h, start
    with the Book ch.10" — one Project with an estimate and a first step —
    into two. A comma list where one item is not an imperative is a sentence
    with modifiers in it, so the whole split is abandoned rather than patched.
    """
    pieces = [p.strip() for p in re.split(r",\s+", part) if p.strip()]
    if len(pieces) < 2:
        return [part]
    for piece in pieces:
        first = re.findall(r"[a-z']+", piece.lower())
        if not first or first[0] not in ACTION_VERBS:
            return [part]
    return pieces


def _is_actionable(text: str) -> bool:
    words = re.findall(r"[a-z']+", (text or "").lower())
    if not words:
        return False
    if words[0] in ACTION_VERBS:
        return True
    if OBLIGATION.search(text or ""):
        return True
    # "I'll email the TA", "we should book the room" — the verb is second or
    # third, after a pronoun or a modal.
    for w in words[:4]:
        if w in ACTION_VERBS:
            return True
    return False


# --------------------------------------------------------------------------
# strategy 2 — clauses in one sentence
# --------------------------------------------------------------------------


def _by_clauses(text: str) -> List[str]:
    """Split one sentence on `;`, `, and`, `then`, `also`.

    The guard is the whole strategy: **every** clause must carry its own
    action verb or its own date, and one failure abandons the entire split.
    That is what stops "eggs, milk and bread" becoming three Projects and
    "read chapters 4 and 5" becoming two — neither `bread` nor `5` is work
    anyone would schedule. A partly-right split is worse than none, because
    the half that fails the guard is a task that silently disappears.
    """
    flat = " ".join(text.split())
    if not flat or len(flat.split()) > MAX_CLAUSE_WORDS:
        return []
    if "\n" in text.strip():
        return []
    parts: List[str] = []
    for chunk in SEPARATORS.split(flat):
        if chunk and chunk.strip(" ,.;"):
            parts.extend(_comma_split(chunk.strip(" ,.;")))
    parts = [_strip_connective(p) for p in parts if p.strip()]
    if len(parts) < 2:
        return []
    # Every part must carry its own action or its own deadline. One bare noun
    # phrase in the list and the whole split is abandoned — a partly-right
    # split is worse than none, because it silently drops half a task.
    for part in parts:
        if len(part.split()) < MIN_TASK_WORDS:
            return []
        if not _is_actionable(part) and not _date_phrase(part):
            return []
    return parts


# --------------------------------------------------------------------------
# strategy 3 — the model, checked against the input
# --------------------------------------------------------------------------

SYSTEM_PROMPT = """You separate a capture into the distinct jobs it contains.

Rules:
- Output JSON only. No prose, no code fence.
- Every task's "text" must be copied word for word from the input. Never \
rewrite, summarise, tidy or translate it. Copy the span.
- Split only where there are genuinely separate jobs — different work, \
different deadlines, different places. One job described in two clauses is \
one task.
- Keep each task's own date words inside its text. They are what schedules it.
- If it is all one job, return one task containing the whole input.
- Never invent a task, a date, or a detail the input does not contain."""

SCHEMA_HINT = """{"tasks": [{"text": "words copied from the input"}]}"""


def _by_model(text: str, cfg: Any, result: SplitResult) -> List[str]:
    try:
        from .llm import resolve_provider

        provider = resolve_provider(cfg.llm, "parse")
    except Exception:  # noqa: BLE001 - no provider is not an error here
        return []
    result.provider = getattr(provider, "name", "")
    if not getattr(provider, "is_llm", False):
        result.degraded = True
        result.note = (
            "No model reachable, and this capture has no list or sentence "
            "seams — filed as one Project."
        )
        return []
    try:
        raw = provider.complete_json(
            f'Capture:\n"""\n{text.strip()}\n"""\n\n'
            f"Separate it into the distinct jobs it contains. Return JSON.",
            system=SYSTEM_PROMPT,
            schema_hint=SCHEMA_HINT,
        )
    except Exception as exc:  # noqa: BLE001
        result.note = f"{type(exc).__name__} splitting the capture; filed as one Project."
        return []

    proposed = _coerce_tasks(raw)
    kept = [p for p in proposed if _really_in(p, text)]
    if len(kept) < len(proposed):
        result.note = (
            f"Dropped {len(proposed) - len(kept)} task(s) the model wrote but "
            f"your capture does not contain."
        )
    if len(kept) < 2:
        return []
    return kept


def _coerce_tasks(raw: Any) -> List[str]:
    items: List[Any] = []
    if isinstance(raw, dict):
        for key in ("tasks", "items", "projects", "jobs"):
            if isinstance(raw.get(key), list):
                items = raw[key]
                break
    elif isinstance(raw, list):
        items = raw
    out: List[str] = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("text") or item.get("task") or item.get("title") or ""
        else:
            value = item
        value = " ".join(str(value or "").split()).strip()
        if value:
            out.append(value)
    return out


def _really_in(candidate: str, source: str) -> bool:
    """Is this task actually made of the words that were typed?

    Compared on collapsed, punctuation-stripped lower case, so a model that
    drops a comma still passes and one that paraphrases does not. This is the
    only thing standing between a model's imagination and a Project on the
    calendar.
    """
    return _norm(candidate) in _norm(source) and len(candidate.split()) >= MIN_TASK_WORDS


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", re.sub(r"\s+", " ", (text or "").lower())).strip()
