"""Ask a question of everything you have filed (blueprint §7).

The tutor's *Explain this* answers from one card's source note, which needs no
index because you already know which note to read. This is the other half: a
question with no note attached, answered out of the whole corpus.

Three properties matter more than answer quality here, because this is a
personal knowledge base rather than a search engine:

**It answers from lj's notes, not from the model's training.** A model asked
"what did I decide about the deposit?" will happily invent a plausible
deposit. So retrieval runs first, the answer is grounded in what came back,
and when nothing comes back the honest response is "nothing in your notes
covers this" — not a fluent paragraph. `SCORE_FLOOR` in `sb/index.py` is where
that line sits.

**Every claim is traceable.** Sources are numbered, the model is required to
cite them inline, and the UI lists what it used. An answer you cannot check is
worth less than the note it came from.

**Archive is opt-in.** §7 is explicit: day-to-day retrieval covers Resources,
and reaching into Archive is a deliberate act. Archiving something is a
decision that it is no longer relevant, and quietly ignoring that decision
would make the Archive bucket meaningless.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config
from .index import Index
from .llm import resolve_provider

SYSTEM_PROMPT = """You answer questions from someone's personal notes. You are \
given numbered excerpts from their own files.

Rules:
- Answer only from the excerpts. They are the user's own notes; treat them as \
the authority, even where you would say otherwise.
- Cite the excerpt each claim comes from, inline, as [1] or [2][3].
- If the excerpts do not answer the question, say exactly what is missing in \
one sentence. Do not fill the gap from general knowledge. Do not guess.
- If the excerpts disagree with each other, say so and cite both.
- Be brief and direct. A few sentences. No preamble, no "based on your notes", \
no restating the question.
- Plain prose. Use a short list only when the answer really is a list."""


@dataclass
class Answer:
    text: str = ""
    sources: List[Dict[str, Any]] = field(default_factory=list)
    used: List[int] = field(default_factory=list)
    semantic: bool = False
    searched_archive: bool = False
    provider: str = ""
    note: str = ""

    @property
    def grounded(self) -> bool:
        return bool(self.sources)


def ask(
    question: str,
    cfg: Config,
    *,
    index: Optional[Index] = None,
    include_archive: bool = False,
    k: int = 6,
) -> Answer:
    """Retrieve, then answer. Returns the sources whether or not the model ran."""
    question = (question or "").strip()
    if not question:
        raise ValueError("ask what?")

    idx = index or Index(cfg)
    if not idx.exists():
        return Answer(
            text="",
            note="No index yet — build one and I can answer from your resources.",
        )

    hits = idx.search(question, k=k, include_archive=include_archive)
    answer = Answer(
        sources=[_source(i + 1, h) for i, h in enumerate(hits)],
        semantic=bool(hits and hits[0].get("semantic")),
        searched_archive=include_archive,
    )

    if not hits:
        answer.text = _nothing_found(include_archive)
        answer.note = "no matching notes"
        return answer

    provider = resolve_provider(cfg.llm, "ask")
    answer.provider = provider.name
    if not getattr(provider, "is_llm", False):
        # No model to write prose with. The excerpts are still the useful part,
        # so hand them over plainly rather than returning an error.
        answer.text = (
            "No model is running, so here are the passages themselves rather "
            "than an answer written from them."
        )
        answer.note = "excerpts only — start Ollama for a written answer"
        return answer

    try:
        answer.text = provider.complete_text(
            _prompt(question, hits), system=SYSTEM_PROMPT
        ).strip()
    except Exception as exc:
        answer.text = ""
        answer.note = f"Could not reach the model ({type(exc).__name__}) — excerpts below."
        return answer

    answer.used = _cited(answer.text, len(hits))
    return answer


def _prompt(question: str, hits: List[Dict[str, Any]]) -> str:
    blocks = []
    for i, hit in enumerate(hits, 1):
        where = hit.get("title", "")
        if hit.get("heading"):
            where += f" › {hit['heading']}"
        if hit.get("bucket") == "archive":
            where += "  (archived)"
        blocks.append(f"[{i}] {where}\n{hit.get('text', '').strip()}")
    return (
        "Excerpts from the user's notes:\n\n"
        + "\n\n---\n\n".join(blocks)
        + f"\n\nQuestion: {question}"
    )


def _source(n: int, hit: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "n": n,
        "note_id": hit.get("note_id", ""),
        "title": hit.get("title", ""),
        "heading": hit.get("heading", ""),
        "bucket": hit.get("bucket", "resource"),
        "score": hit.get("score", 0),
        "excerpt": _excerpt(hit.get("text", "")),
    }


def _excerpt(text: str, limit: int = 320) -> str:
    """The chunk minus the title prefix the indexer added, trimmed to a
    quotable length — the UI shows this under the answer, and a wall of text
    there is not a citation, it is a second answer."""
    body = text.split("\n\n", 1)[-1].strip()
    body = re.sub(r"\s+", " ", body)
    return body if len(body) <= limit else body[: limit - 1].rstrip() + "…"


def _cited(text: str, count: int) -> List[int]:
    """Which sources the answer actually leaned on, so the UI can mark the
    rest as merely retrieved."""
    return sorted(
        {n for n in (int(m) for m in re.findall(r"\[(\d{1,2})\]", text)) if 1 <= n <= count}
    )


def _nothing_found(include_archive: bool) -> str:
    tail = (
        "" if include_archive
        else " Archive was not searched — tick “include archive” to widen it."
    )
    return "Nothing in your resources covers that." + tail
