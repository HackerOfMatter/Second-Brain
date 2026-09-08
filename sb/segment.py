"""Long text → several atomic notes.

The capture box used to be a one-note box. Paste a lecture, a chapter, or a
forty-minute brain dump into it and you got one enormous note: unlinkable,
because a note about eleven things links to everything; unquizzable, because
`generate.chunk` would slice it into passages that no longer knew what they
were about; and unfindable, because retrieval returns notes and a note that
answers eleven questions answers none of them well.

This module is the missing step. It takes text of any length and returns the
notes it is actually made of, and it is built on one rule:

    **Nothing may be lost.** Every line of the input ends up in exactly one
    segment. Boundaries are chosen; content is never dropped, summarised, or
    rewritten. `check_lossless()` asserts it and the test suite runs that
    assertion over every strategy below.

That rule is why the model is the *third* choice rather than the first. Four
strategies, tried in order of how much the text tells us:

  1. **Headings.** Text that says where its own topics start is text you do
     not need a model to split. Markdown ATX and setext headings, plus the
     shapes a pasted textbook uses — `Chapter 4`, `Section 2.1`, an ALL-CAPS
     line on its own.
  2. **Rules.** `---` and `***` between blocks: an author's explicit break.
  3. **Topics.** Unstructured prose — a transcript, a wall of typing. Here a
     model is asked where the subject changes, and it is asked for *boundary
     indices*, never for the text. A model that hallucinates cannot corrupt a
     segment it never wrote: the worst it can do is put a break in the wrong
     place, and a misplaced break is a note you re-file, not a fact you
     believe.
  4. **Size.** Pack paragraphs to a word budget. Always available, never
     fails, and it is what the three above degrade into.

Code fences, tables and list blocks are atomic — a boundary is never placed
inside one, because half a code block is not a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from .extract import derive_title

# --------------------------------------------------------------------------
# sizes
# --------------------------------------------------------------------------

#: A section past this is asked to split again at a paragraph boundary. Chosen
#: to sit above a long-but-single-idea section (a worked derivation runs to
#: ~350 words) and below the point where a note is plainly two subjects.
MAX_SEGMENT_WORDS = 400

#: Under this, a section is a fragment — a stray line under a heading — and is
#: merged into its neighbour rather than becoming a note nothing can link to.
#: The vault already has 69 empty files; this is how we stop making more.
MIN_SEGMENT_WORDS = 30

#: The same floor for a boundary the *author* chose by writing a heading.
#: Much lower on purpose: a machine-guessed boundary with 25 words under it is
#: probably a bad guess, and a heading with 25 words under it is a short
#: section someone meant to write. We only override a heading when there is
#: essentially nothing beneath it.
MIN_HEADING_WORDS = 12

#: Never emitted larger, even when a single indivisible block (one code fence,
#: one table) is bigger than `MAX_SEGMENT_WORDS`. Such a block is kept whole
#: and allowed to exceed the soft cap — splitting it would break the rule at
#: the top of this file in the one way that matters.
HARD_MAX_WORDS = 1200

#: Below this the whole paste is one note. Splitting a paragraph into two
#: half-paragraphs is worse than leaving it alone.
SPLIT_FLOOR_WORDS = 120

#: Paragraphs shown to the model in one boundary request.
MODEL_MAX_PARAGRAPHS = 150

#: Characters of each paragraph shown to the model. It is choosing boundaries,
#: not reading for comprehension, and the opening of a paragraph is where the
#: subject change shows.
MODEL_PARAGRAPH_CHARS = 220


# --------------------------------------------------------------------------
# lexing
# --------------------------------------------------------------------------

FENCE = re.compile(r"^\s{0,3}(`{3,}|~{3,})")
ATX = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*#*\s*$")
RULE = re.compile(r"^\s{0,3}([-*_])[ \t]*(?:\1[ \t]*){2,}$")
SETEXT_H1 = re.compile(r"^\s{0,3}=+\s*$")
SETEXT_H2 = re.compile(r"^\s{0,3}-{2,}\s*$")
BULLET = re.compile(r"^\s{0,3}([-*+]|\d{1,3}[.)])\s+\S")
TABLE_ROW = re.compile(r"^\s{0,3}\|")

#: Headings a pasted document has instead of markdown ones. Tried only when
#: the text contains no markdown heading at all, because a line reading
#: "Chapter 4" inside a document that already uses `##` is prose about a
#: chapter, not a heading.
PLAIN_HEADINGS = (
    re.compile(r"^\s{0,3}(chapter|section|part|unit|module|lecture|week|topic|lesson)"
               r"\s+([0-9]{1,3}|[ivxlc]{1,7})\b[.:\)]?\s*(.*)$", re.I),
    re.compile(r"^\s{0,3}(\d{1,2}(?:\.\d{1,2}){0,2})[.):]\s+([A-Z].{2,80})$"),
)

#: An ALL-CAPS line on its own is a heading in a surprising amount of pasted
#: material. Bounded hard: long enough to be a title, short enough not to be a
#: shouted paragraph, and with at least one letter.
CAPS_HEADING = re.compile(r"^\s{0,3}([A-Z][A-Z0-9 &/'’\-,()]{3,60})\s*$")


@dataclass
class Piece:
    """One indivisible block of the input, in input order."""

    kind: str  # heading | para | list | table | code | rule
    text: str
    level: int = 0
    title: str = ""

    @property
    def words(self) -> int:
        return len(self.text.split())


def _setext_level(line: str) -> int:
    if SETEXT_H1.match(line):
        return 1
    if SETEXT_H2.match(line):
        return 2
    return 0


def _block_kind(body: str) -> str:
    lines = [l for l in body.split("\n") if l.strip()]
    if not lines:
        return "para"
    if sum(1 for l in lines if TABLE_ROW.match(l)) >= max(2, len(lines) // 2):
        return "table"
    if sum(1 for l in lines if BULLET.match(l)) >= max(1, len(lines) // 2):
        return "list"
    return "para"


def strip_frontmatter(text: str) -> str:
    """Drop a leading `---` block. Shared shape with `generate._strip_frontmatter`."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :].lstrip("\n")
    return text


def pieces(text: str) -> List[Piece]:
    """Lex the input into blocks, keeping fences, tables and lists whole.

    The fence state machine runs first and swallows everything until its
    closing marker, which is the only reason a `# comment` line inside a
    Python block does not become a section heading.
    """
    out: List[Piece] = []
    buf: List[str] = []
    in_fence = False
    fence_char = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            body = "\n".join(buf).strip("\n")
            if body.strip():
                out.append(Piece(kind=_block_kind(body), text=body))
        buf = []

    for line in (text or "").split("\n"):
        fm = FENCE.match(line)
        if fm:
            ch = fm.group(1)[0]
            if not in_fence:
                flush()
                in_fence, fence_char = True, ch
                buf.append(line)
                continue
            if ch == fence_char:
                buf.append(line)
                out.append(Piece(kind="code", text="\n".join(buf)))
                buf = []
                in_fence = False
                continue
        if in_fence:
            buf.append(line)
            continue

        if not line.strip():
            flush()
            continue

        atx = ATX.match(line)
        if atx:
            flush()
            title = atx.group(2).strip()
            out.append(Piece("heading", line.rstrip(), len(atx.group(1)), title))
            continue

        level = _setext_level(line)
        if level and len(buf) == 1 and buf[0].strip():
            title = buf[0].strip()
            buf = []
            out.append(Piece("heading", f"{'#' * level} {title}", level, title))
            continue

        if RULE.match(line):
            flush()
            out.append(Piece("rule", line.strip()))
            continue

        buf.append(line)

    if in_fence and buf:
        # An unterminated fence. Keep it whole rather than re-lexing its
        # contents as markdown — the author meant it to be literal.
        out.append(Piece(kind="code", text="\n".join(buf)))
    else:
        flush()
    return out


def promote_plain_headings(blocks: List[Piece]) -> int:
    """Turn `Chapter 4` / `2.1 Elasticity` / `CASH FLOWS` lines into headings.

    Only called when the text has no markdown heading at all. Returns how many
    were promoted, so the caller can tell whether the strategy applies.

    A promoted heading only ever *splits* a block; the line itself is kept as
    the heading's own text, so `check_lossless` still passes.
    """
    promoted = 0
    i = 0
    while i < len(blocks):
        block = blocks[i]
        if block.kind not in ("para", "list"):
            i += 1
            continue
        lines = block.text.split("\n")
        replacement: List[Piece] = []
        run: List[str] = []

        def keep() -> None:
            if run:
                body = "\n".join(run).strip("\n")
                if body.strip():
                    replacement.append(Piece(_block_kind(body), body))
                run.clear()

        for line in lines:
            title = _plain_heading_title(line)
            if title:
                keep()
                replacement.append(Piece("heading", line.rstrip(), 2, title))
            else:
                run.append(line)
        keep()
        if any(p.kind == "heading" for p in replacement):
            promoted += sum(1 for p in replacement if p.kind == "heading")
            blocks[i : i + 1] = replacement
            i += len(replacement)
        else:
            i += 1
    return promoted


def _plain_heading_title(line: str) -> str:
    stripped = line.strip()
    if not stripped or len(stripped) > 90:
        return ""
    if BULLET.match(line) or TABLE_ROW.match(line):
        return ""
    for pattern in PLAIN_HEADINGS:
        m = pattern.match(stripped)
        if m:
            return re.sub(r"\s+", " ", stripped).strip(" .:)")
    if CAPS_HEADING.match(stripped) and any(c.isalpha() for c in stripped):
        # "I" and "OK" are not headings; a caps line ending in a full stop is
        # a shouted sentence.
        if len(stripped.split()) >= 2 and not stripped.endswith((".", "!", "?")):
            return stripped.title()
    return ""


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------


@dataclass
class Segment:
    """One note-to-be."""

    title: str
    body: str
    heading_path: List[str] = field(default_factory=list)
    boundary: str = "size"
    ordinal: int = 0

    @property
    def words(self) -> int:
        return len(self.body.split())

    def as_dict(self) -> Dict[str, Any]:
        return {
            "title": self.title,
            "body": self.body,
            "heading_path": list(self.heading_path),
            "boundary": self.boundary,
            "ordinal": self.ordinal,
            "words": self.words,
        }


@dataclass
class SegmentResult:
    segments: List[Segment] = field(default_factory=list)
    strategy: str = "whole"
    provider: str = ""
    degraded: bool = False
    note: str = ""
    words: int = 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "segments": [s.as_dict() for s in self.segments],
            "strategy": self.strategy,
            "provider": self.provider,
            "degraded": self.degraded,
            "note": self.note,
            "words": self.words,
            "count": len(self.segments),
        }


# --------------------------------------------------------------------------
# the entry point
# --------------------------------------------------------------------------


def segment(
    text: str,
    cfg: Any = None,
    *,
    max_words: int = MAX_SEGMENT_WORDS,
    min_words: int = MIN_SEGMENT_WORDS,
    floor_words: int = SPLIT_FLOOR_WORDS,
    allow_model: bool = True,
) -> SegmentResult:
    """Split `text` into the notes it is made of. Writes nothing."""
    raw = strip_frontmatter(text or "").strip("\n")
    result = SegmentResult(words=len(raw.split()))
    if not raw.strip():
        return result

    blocks = pieces(raw)
    if not blocks:
        return result

    has_headings = any(b.kind == "heading" for b in blocks)
    has_rules = any(b.kind == "rule" for b in blocks)
    if not has_headings and promote_plain_headings(blocks):
        has_headings = True
        result.note = "Split on the document's own chapter and section lines."

    # Short enough that splitting cannot help — *unless* the text says where
    # its own boundaries are. Three headings in a hundred words is still three
    # notes; the floor exists to stop us guessing, not to overrule the author.
    if result.words < floor_words and not has_headings and not has_rules:
        result.segments = [_segment_from(blocks, [], "whole", 0)]
        result.strategy = "whole"
        result.note = (
            f"Kept whole — {result.words} words is one thought, "
            f"below the {floor_words}-word split floor."
        )
        return _finish(result, max_words, min_words)

    if has_headings:
        result.segments = _split_on_headings(blocks)
        result.strategy = "heading"
        return _finish(result, max_words, min_words)

    if has_rules:
        result.segments = _split_on_rules(blocks)
        result.strategy = "rule"
        result.note = "Split on the horizontal rules already in the text."
        return _finish(result, max_words, min_words)

    if allow_model and cfg is not None:
        topical = _split_on_topics(blocks, cfg, result, max_words=max_words)
        if topical:
            result.segments = topical
            result.strategy = "topic"
            return _finish(result, max_words, min_words)

    result.segments = _split_on_size(blocks, max_words)
    result.strategy = "size"
    result.degraded = True
    if not result.note:
        result.note = (
            "No headings, rules or model — packed into notes by size. "
            "Check the boundaries before saving."
        )
    return _finish(result, max_words, min_words)


def _finish(result: SegmentResult, max_words: int, min_words: int) -> SegmentResult:
    """Enforce the size band and number the segments.

    Order matters: split the oversized first, then merge the undersized, so a
    fragment left over by a split is absorbed rather than shipped.
    """
    split_out: List[Segment] = []
    for seg in result.segments:
        split_out.extend(_enforce_max(seg, max_words))
    merged = _merge_small(split_out, min_words, max_words)
    for i, seg in enumerate(merged, start=1):
        seg.ordinal = i
        if not seg.title:
            seg.title = derive_title(seg.body) or f"Note {i}"
    result.segments = [s for s in merged if s.body.strip()]
    return result


# --------------------------------------------------------------------------
# strategies
# --------------------------------------------------------------------------


def _render(blocks: Sequence[Piece]) -> str:
    return "\n\n".join(b.text.strip("\n") for b in blocks if b.text.strip()).strip()


def _segment_from(
    blocks: Sequence[Piece], path: Sequence[str], boundary: str, ordinal: int, title: str = ""
) -> Segment:
    body = _render(blocks)
    return Segment(
        title=title or derive_title(body),
        body=body,
        heading_path=list(path),
        boundary=boundary,
        ordinal=ordinal,
    )


def _split_level(blocks: Sequence[Piece]) -> int:
    """The heading depth to cut at.

    The shallowest level that occurs more than once — one `# Title` above
    twelve `## Topic`s is a document title, not twelve documents' worth of
    boundary.

    When *nothing* repeats the reasoning inverts: there is no level that reads
    as a wrapper, so every heading is its own topic and the cut goes at the
    deepest one. Cutting at the shallowest instead gave "# Chapter 4" with
    "## Elasticity" inside it a single section named after the chapter, which
    is the one-enormous-note failure this module exists to fix.
    """
    levels: Dict[int, int] = {}
    for b in blocks:
        if b.kind == "heading":
            levels[b.level] = levels.get(b.level, 0) + 1
    if not levels:
        return 0
    repeated = sorted(l for l, n in levels.items() if n > 1)
    return repeated[0] if repeated else max(levels)


def _split_on_headings(blocks: List[Piece]) -> List[Segment]:
    cut_at = _split_level(blocks)
    out: List[Segment] = []
    path: List[str] = []
    current: List[Piece] = []
    current_title = ""
    current_path: List[str] = []

    def close() -> None:
        if current and _render(current).strip():
            out.append(
                _segment_from(current, current_path, "heading", 0, title=current_title)
            )
        current.clear()

    for block in blocks:
        if block.kind == "heading" and block.level <= cut_at:
            close()
            path = path[: max(0, block.level - 1)]
            current_path = list(path)
            path = path + [block.title]
            current_title = block.title
            continue
        if block.kind == "heading":
            # A deeper heading stays in the body — it is structure inside this
            # note, and dropping it would lose a line.
            current.append(block)
            continue
        if block.kind == "rule" and not current:
            continue
        current.append(block)
    close()

    # Preamble before the first heading, if any, is the first segment and has
    # no heading of its own.
    return [s for s in out if s.body.strip()]


def _split_on_rules(blocks: List[Piece]) -> List[Segment]:
    out: List[Segment] = []
    current: List[Piece] = []
    for block in blocks:
        if block.kind == "rule":
            if _render(current).strip():
                out.append(_segment_from(current, [], "rule", 0))
            current = []
            continue
        current.append(block)
    if _render(current).strip():
        out.append(_segment_from(current, [], "rule", 0))
    return out


def _split_on_size(blocks: Sequence[Piece], max_words: int) -> List[Segment]:
    out: List[Segment] = []
    current: List[Piece] = []
    count = 0
    for block in blocks:
        if block.kind == "rule":
            continue
        if current and count + block.words > max_words:
            out.append(_segment_from(current, [], "size", 0))
            current, count = [], 0
        current.append(block)
        count += block.words
    if current:
        out.append(_segment_from(current, [], "size", 0))
    return out


# --------------------------------------------------------------------------
# the model path — boundaries only, never text
# --------------------------------------------------------------------------

TOPIC_SYSTEM = """You mark where the subject changes in someone's notes.

You are given numbered paragraphs. You return the paragraph numbers where a \
new topic begins, and a short title for each topic.

Rules:
- Output JSON only. No prose, no code fence.
- Never return the paragraph text. Only numbers and titles.
- The first section always starts at paragraph 1.
- A new section starts only where the subject genuinely changes. Two \
paragraphs about the same idea belong together.
- A title is 2-7 words naming that section's subject, in the notes' own \
vocabulary. Not "Introduction", not "Section 2".
- Prefer fewer, larger sections. Six paragraphs on one subject is one \
section, not six.
- If the whole thing is one topic, return a single section starting at 1."""

TOPIC_SCHEMA = """{"sections": [{"start": 1, "title": "short subject name"}]}"""


def _split_on_topics(
    blocks: List[Piece], cfg: Any, result: SegmentResult, *, max_words: int
) -> List[Segment]:
    """Ask a model where the subject changes. Returns [] when it cannot."""
    try:
        from .llm import resolve_provider

        provider = resolve_provider(cfg.llm, "generate")
    except Exception:  # noqa: BLE001 - a missing provider is not an error here
        return []
    result.provider = getattr(provider, "name", "")
    if not getattr(provider, "is_llm", False):
        return []

    body_blocks = [b for b in blocks if b.kind != "rule"]
    if len(body_blocks) < 3:
        return []
    shown = body_blocks[:MODEL_MAX_PARAGRAPHS]
    listing = "\n".join(
        f"{i}. {_preview(b.text)}" for i, b in enumerate(shown, start=1)
    )
    try:
        raw = provider.complete_json(
            f"Paragraphs:\n{listing}\n\n"
            f"Mark where each new topic begins. Return JSON.",
            system=TOPIC_SYSTEM,
            schema_hint=TOPIC_SCHEMA,
        )
    except Exception as exc:  # noqa: BLE001 - fall through to size packing
        result.note = f"{type(exc).__name__} asking for topic boundaries; packed by size."
        return []

    marks = _coerce_marks(raw, len(shown))
    if len(marks) < 2:
        return []

    out: List[Segment] = []
    for i, (start, title) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(body_blocks) + 1
        chunk = body_blocks[start - 1 : end - 1]
        if not chunk:
            continue
        out.append(_segment_from(chunk, [], "topic", 0, title=title))
    # Anything past the window the model saw is packed by size and appended,
    # so a 400-paragraph transcript is not silently truncated to 150.
    if len(body_blocks) > len(shown):
        out.extend(_split_on_size(body_blocks[len(shown) :], max_words))
    result.note = "Topic boundaries proposed by the model — check them before saving."
    return [s for s in out if s.body.strip()]


def _preview(text: str) -> str:
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[:MODEL_PARAGRAPH_CHARS] + ("…" if len(flat) > MODEL_PARAGRAPH_CHARS else "")


def _coerce_marks(raw: Any, n: int) -> List[tuple]:
    """Model output → a clean, ordered, in-range list of (start, title).

    Every failure mode gets clamped rather than raised: out-of-range indices
    dropped, duplicates collapsed, order forced, and the first section pinned
    to paragraph 1 so no paragraph can fall off the front.
    """
    items: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        for key in ("sections", "topics", "boundaries", "items"):
            if isinstance(raw.get(key), list):
                items = [x for x in raw[key] if isinstance(x, dict)]
                break
    elif isinstance(raw, list):
        items = [x for x in raw if isinstance(x, dict)]
    if not items:
        return []

    seen: Dict[int, str] = {}
    for item in items:
        value = item.get("start", item.get("paragraph", item.get("index")))
        try:
            start = int(str(value).strip())
        except (TypeError, ValueError):
            continue
        if not 1 <= start <= n:
            continue
        title = re.sub(r"\s+", " ", str(item.get("title") or "")).strip().strip('"')
        if len(title.split()) > 12:
            title = ""
        seen.setdefault(start, title)
    if not seen:
        return []
    marks = sorted(seen.items())
    if marks[0][0] != 1:
        marks.insert(0, (1, ""))
    return marks


# --------------------------------------------------------------------------
# the size band
# --------------------------------------------------------------------------


def _enforce_max(seg: Segment, max_words: int) -> List[Segment]:
    """Split an oversized segment at block boundaries, never inside one."""
    if seg.words <= max_words:
        return [seg]
    blocks = pieces(seg.body)
    if len(blocks) < 2:
        return [seg]  # one indivisible block; see HARD_MAX_WORDS
    parts: List[List[Piece]] = []
    current: List[Piece] = []
    count = 0
    for block in blocks:
        if current and count + block.words > max_words:
            parts.append(current)
            current, count = [], 0
        current.append(block)
        count += block.words
    if current:
        parts.append(current)
    if len(parts) < 2:
        return [seg]
    total = len(parts)
    # The parts are children of the section, so the section's own title joins
    # their path. That keeps the breadcrumb honest ("Ch 4 › FIFO vs LIFO") and
    # keeps `check_lossless` true for a heading that had to be split.
    path = list(seg.heading_path) + ([seg.title] if seg.title else [])
    return [
        Segment(
            title=f"{seg.title} ({i}/{total})" if seg.title else "",
            body=_render(part),
            heading_path=path,
            boundary=seg.boundary + "+size",
        )
        for i, part in enumerate(parts, start=1)
    ]


def _merge_small(segments: List[Segment], min_words: int, max_words: int) -> List[Segment]:
    """Absorb fragments into the note before them.

    A heading with one line under it is not a note; it is a line under a
    heading. Merging keeps its heading text, so the line still says what it
    was about and nothing is lost.
    """
    def floor_for(seg: Segment) -> int:
        return MIN_HEADING_WORDS if seg.boundary.startswith("heading") else min_words

    out: List[Segment] = []
    for seg in segments:
        if (
            out
            and seg.words < floor_for(seg)
            and out[-1].words + seg.words <= max_words
        ):
            head = f"## {seg.title}\n\n" if seg.title else ""
            out[-1].body = f"{out[-1].body}\n\n{head}{seg.body}".strip()
            continue
        out.append(seg)
    # A leading fragment has nothing before it, so it merges forward instead.
    if len(out) > 1 and out[0].words < floor_for(out[0]) and out[1].words + out[0].words <= max_words:
        head = f"## {out[0].title}\n\n" if out[0].title else ""
        out[1].body = f"{head}{out[0].body}\n\n{out[1].body}".strip()
        out = out[1:]
    return out


# --------------------------------------------------------------------------
# the guarantee
# --------------------------------------------------------------------------


def check_lossless(text: str, segments: Sequence[Segment]) -> List[str]:
    """Return the input lines that no segment contains. Empty means lossless.

    Compared on stripped, whitespace-collapsed lines because segmentation
    re-joins blocks with a single blank line and may re-render a setext
    heading as an ATX one. Heading *text* must still survive: `Topic\\n=====`
    becomes `## Topic`, and "Topic" is what this checks for.
    """
    have: List[str] = []
    for seg in segments:
        have.extend(_comparable(seg.body))
        have.extend(_comparable(seg.title))
        # A document title with no prose under it survives as every child's
        # `heading_path`, and the engine writes that onto the note as a
        # breadcrumb. It is carried, not dropped, so it counts as covered.
        for parent in seg.heading_path:
            have.extend(_comparable(parent))
    pool = set(have)
    missing: List[str] = []
    for line in _comparable(strip_frontmatter(text or "")):
        if line not in pool:
            missing.append(line)
    return missing


def _comparable(text: str) -> List[str]:
    out = []
    for line in (text or "").split("\n"):
        flat = re.sub(r"\s+", " ", line).strip()
        flat = re.sub(r"^#{1,6}\s+", "", flat).rstrip("#").strip()
        if flat and not RULE.match(flat) and not SETEXT_H1.match(flat) and not SETEXT_H2.match(flat):
            out.append(flat.lower())
    return out
