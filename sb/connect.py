"""Smart connections — find related notes, and link them.

The graph is the point of keeping notes in Obsidian at all (blueprint §5), but
nothing was building it: links only ever appeared where a template put them or
where lj typed one by hand.

**Runs when asked, never otherwise**, and **spends a model call only on links
nothing cheaper could find**. The first version of this module asked the model
about every note; at two hundred notes that is two hundred calls and roughly
twenty minutes on a local 8B, repeated in full on every re-run. Most links in
this vault do not need a model at all — they are either facts already sitting
in the frontmatter, or a note's title appearing verbatim in another note.

Four tiers, cheapest first. Each tier only sees what the tiers above it did
not already claim, and the run stops as soon as `max_links` is full — so a
note whose connections are obvious costs nothing but arithmetic.

    tier 0a  structural   frontmatter facts         ~0s     certain
    tier 0b  title        literal name mentions     ~0s     near-certain
    tier 1   similar      embeddings already built  <1s     good
    tier 2   judged       one model call          ~3-5s     the ambiguous band

Tier 1 is split by confidence: a candidate above `AUTO_FLOOR` is accepted
without asking, and only the uncertain middle band reaches tier 2. That band
is usually a fraction of the candidates, which is where the saving comes from.

Re-runs are gated on a content fingerprint kept in `_system/connect.json`, so
connecting an unchanged vault a second time costs one hash per note.

Links land in a `## Related` section, which the preserve pass in
`engine._project_body` carries through untouched — before that fix this
section could not have existed on a Project note at all.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .config import Config
from .llm import resolve_provider
from .models import Bucket, Note, slugify

SYSTEM_PROMPT = """You decide which notes in someone's personal knowledge \
base are genuinely related, and say why.

You are given one note and a list of candidate notes that a search ranked as \
similar but not conclusively so. Similarity is not relatedness: two notes can \
share vocabulary and have nothing to do with each other.

Rules:
- Output JSON only. No prose, no code fence.
- Keep a candidate only if someone reading the first note would actually want \
to open it. When in doubt, drop it.
- "why" is at most 12 words, concrete, and names the actual relationship — \
"prerequisite for this", "the worked example of it", "contradicts this on \
timing". Never "related to" or "similar topic".
- Prefer few strong links. An empty list is a correct answer.
- Never invent a note. Only use titles from the candidate list, copied \
exactly."""

SCHEMA_HINT = """{
  "links": [
    {"title": "exact candidate title", "why": "short reason, max 12 words"}
  ]
}"""

RELATED_HEADING = "## Related"
MARKER = "<!-- suggested by connect.py — re-run to refresh, delete to reject -->"

#: Above this cosine a candidate is accepted without asking. Set from what the
#: middle band is actually for: up here retrieval is already confident, and a
#: call would almost always agree at the cost of several seconds.
AUTO_FLOOR = 0.62

#: Below this, retrieval is noise and the model would only be adjudicating
#: randomness.
SIMILARITY_FLOOR = 0.35

MAX_CANDIDATES = 12
MAX_LINKS = 6

#: Titles shorter than this are never matched as literal mentions. lj's vault
#: contains a note titled "1"; without this guard it would be linked from
#: every note containing that digit.
MIN_TITLE_CHARS = 5

WIKILINK = re.compile(r"\[\[([^\]\|#]+)(?:#[^\]\|]+)?(?:\|[^\]]+)?\]\]")
FROM_LINE = re.compile(r"^\s*\*From:\*\s*(.*)$", re.M)
CODE_FENCE = re.compile(r"```.*?```", re.S)


@dataclass
class Link:
    title: str
    why: str = ""
    score: float = 0.0
    #: Which tier produced this: structural | title | similar | judged. Kept
    #: so a link's provenance is visible in the note, and so a later run can
    #: tell a free link from one that cost a model call.
    source: str = ""


@dataclass
class ConnectResult:
    note_id: str = ""
    title: str = ""
    links: List[Link] = field(default_factory=list)
    candidates: int = 0
    provider: str = ""
    degraded: bool = False
    changed: bool = False
    skipped: bool = False
    #: True when the model was actually called. The number to watch.
    used_model: bool = False
    note: str = ""

    @property
    def as_dict(self) -> Dict[str, Any]:
        return {
            "note_id": self.note_id,
            "title": self.title,
            "links": [
                {"title": l.title, "why": l.why, "score": round(l.score, 3), "source": l.source}
                for l in self.links
            ],
            "candidates": self.candidates,
            "provider": self.provider,
            "degraded": self.degraded,
            "changed": self.changed,
            "skipped": self.skipped,
            "used_model": self.used_model,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------


def existing_links(body: str) -> set:
    """Every `[[title]]` already in the note, slugified."""
    return {slugify(m.group(1).strip()) for m in WIKILINK.finditer(body or "")}


def parent_of(note: Note) -> Optional[str]:
    """The `*From:*` backlink an atomized note carries to its guide note."""
    match = FROM_LINE.search(note.body or "")
    if not match:
        return None
    link = WIKILINK.search(match.group(1))
    return slugify(link.group(1)) if link else None


def strip_related(body: str) -> str:
    """Remove a previously written `## Related` section.

    Only ours: the section must carry `MARKER`. A `## Related` lj wrote by
    hand is left where it is — this module suggests links, it does not take
    ownership of a heading someone else is using.
    """
    body = body or ""
    start = body.find(RELATED_HEADING)
    if start == -1:
        return body
    after = body[start + len(RELATED_HEADING) :]
    nxt = re.search(r"^##\s", after, re.M)
    end = len(body) if not nxt else start + len(RELATED_HEADING) + nxt.start()
    if MARKER not in body[start:end]:
        return body
    return (body[:start].rstrip("\n") + "\n\n" + body[end:].lstrip("\n")).rstrip() + "\n"


def content_text(note: Note) -> str:
    """The note's own words, without our own previous suggestions.

    Feeding one run's output into the next run's input is how a suggestion
    engine slowly starts agreeing only with itself.
    """
    return f"{note.title}\n\n{strip_related(note.body or '')}"


# --------------------------------------------------------------------------
# tier 0a — structural. Facts, not guesses. No model, no vectors.
# --------------------------------------------------------------------------


def structural_links(note: Note, others: Sequence[Note]) -> List[Link]:
    """Links implied by data the vault already stores.

    The strongest links in the system, and they were being ignored entirely
    while a language model was asked to rediscover them from prose. Three
    kinds:

      * **Siblings** — two atomic notes atomized out of the same guide note.
        Same assignment, same reading, by construction.
      * **Backlinks** — a note that already links here. If a Quiz points at a
        concept, that concept should point back at the Quiz.

    The parent itself is deliberately absent: `parent_of` reads it out of the
    note's own `*From:*` line, so the link is already in the body by
    definition and re-proposing it would only duplicate what is there.

    Shared tags are deliberately *not* used either: `atomic` is on every
    atomic note and says what a note **is**, not what it is **about**.
    """
    own = slugify(note.title)
    seen = existing_links(note.body) | {own}
    mine = parent_of(note)
    out: List[Link] = []

    def add(other: Note, why: str) -> None:
        key = slugify(other.title)
        if key in seen or key == own or other.id == note.id:
            return
        seen.add(key)
        out.append(Link(title=other.title, why=why, score=1.0, source="structural"))

    if mine:
        for other in others:
            if parent_of(other) == mine:
                add(other, "atomized from the same source")

    for other in others:
        if own in existing_links(other.body):
            add(other, "links here")

    return out


# --------------------------------------------------------------------------
# tier 0b — literal title mentions. One regex, no model, no vectors.
# --------------------------------------------------------------------------


def _matchable(title: str) -> bool:
    title = (title or "").strip()
    if len(title) < MIN_TITLE_CHARS:
        return False
    if not re.search(r"[A-Za-z]", title):
        return False  # "1", "2026" — a title with no letters is not a term
    return True


def title_matcher(others: Sequence[Note]) -> Optional[re.Pattern]:
    """One compiled alternation over every linkable note title.

    Cheap, and unusually precise *in this vault*: atomic notes are named after
    the exact terms that appear in assignment and quiz prose, so a literal
    mention is nearly always a real reference. A vault of generically-titled
    notes would not get this for free.

    Longest first, so "Opportunity Cost of Capital" wins over "Opportunity
    Cost" where both exist.
    """
    titles = sorted(
        {n.title.strip() for n in others if _matchable(n.title)}, key=len, reverse=True
    )
    if not titles:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(t) for t in titles) + r")\b", re.I)


def title_links(
    note: Note, others: Sequence[Note], matcher: Optional[re.Pattern] = None
) -> List[Link]:
    """Notes whose title appears verbatim in this note's text."""
    matcher = title_matcher(others) if matcher is None else matcher
    if matcher is None:
        return []

    # Prose only: text already inside a `[[link]]` is linked, and a code block
    # naming a note is not a reference to it.
    haystack = CODE_FENCE.sub(" ", strip_related(note.body or ""))
    haystack = WIKILINK.sub(" ", haystack)

    own = slugify(note.title)
    seen = existing_links(note.body) | {own}
    by_slug = {slugify(n.title): n for n in others}

    out: List[Link] = []
    for hit in matcher.finditer(haystack):
        key = slugify(hit.group(1))
        if key in seen:
            continue
        other = by_slug.get(key)
        if other is None or other.id == note.id:
            continue
        seen.add(key)
        out.append(
            Link(title=other.title, why="named in this note", score=0.95, source="title")
        )
    return out


# --------------------------------------------------------------------------
# tier 1 — embeddings. Already built, already incremental.
# --------------------------------------------------------------------------


def candidates(
    note: Note,
    index,
    *,
    limit: int = MAX_CANDIDATES,
    include_archive: bool = False,
    exclude: Optional[Iterable[str]] = None,
) -> List[Link]:
    """Rank other notes against this one using the existing RAG index.

    `per_note=1` because the goal is *distinct notes*, not best passages — the
    default of 2 would spend half the slots proving one note matches twice.
    """
    query = content_text(note)
    if not query.strip():
        return []
    hits = index.search(query, k=limit * 2, include_archive=include_archive, per_note=1)

    own = slugify(note.title)
    seen = existing_links(note.body) | {own} | {slugify(t) for t in (exclude or [])}

    out: List[Link] = []
    for hit in hits:
        title = (hit.get("title") or "").strip()
        key = slugify(title)
        if not title or key in seen or hit.get("note_id") == note.id:
            continue
        score = float(hit.get("cosine") or hit.get("score") or 0.0)
        if score < SIMILARITY_FLOOR:
            continue
        seen.add(key)
        out.append(Link(title=title, score=score, source="similar"))
        if len(out) >= limit:
            break
    return out


def split_by_confidence(found: Sequence[Link]) -> tuple:
    """(confident enough to keep, ambiguous enough to be worth asking about).

    This split is the whole efficiency argument: only the second list costs a
    model call.
    """
    sure = [l for l in found if l.score >= AUTO_FLOOR]
    for link in sure:
        link.why = link.why or "closely related"
    return sure, [l for l in found if l.score < AUTO_FLOOR]


# --------------------------------------------------------------------------
# tier 2 — the model, on the ambiguous band only
# --------------------------------------------------------------------------


def judge(
    note: Note, found: Sequence[Link], cfg: Config, *, max_links: int = MAX_LINKS
) -> ConnectResult:
    """Ask the model which of the uncertain candidates are genuinely related."""
    result = ConnectResult(note_id=note.id, title=note.title, candidates=len(found))
    if not found or max_links <= 0:
        return result

    provider = resolve_provider(cfg.llm, "connect")
    result.provider = provider.name

    if not getattr(provider, "is_llm", False):
        # No model: the ambiguous band stays ambiguous, and dropping it is the
        # honest choice. These are exactly the candidates that needed
        # judgement; guessing would write weak links into the graph for good.
        result.degraded = True
        result.note = "No model reachable — kept only the confident links."
        return result

    listing = "\n".join(f"- {l.title}" for l in found)
    prompt = (
        f'Note: "{note.title}"\n\n'
        f"Its content:\n\"\"\"\n{content_text(note)[:2500]}\n\"\"\"\n\n"
        f"Candidates:\n{listing}\n\n"
        f"Which of these are genuinely worth linking from that note, and why? "
        f"At most {max_links}. Return JSON."
    )
    try:
        raw = provider.complete_json(prompt, system=SYSTEM_PROMPT, schema_hint=SCHEMA_HINT)
    except Exception as exc:
        result.degraded = True
        result.note = f"{type(exc).__name__} from the model — kept only the confident links."
        return result

    result.used_model = True
    by_title = {l.title.lower(): l for l in found}
    for item in _coerce_links(raw):
        if len(result.links) >= max_links:
            break
        title = str(item.get("title") or "").strip()
        match = by_title.get(title.lower())
        if match is None:
            continue  # a title not on the candidate list was invented
        why = re.sub(r"\s+", " ", str(item.get("why") or "")).strip().strip('".')
        result.links.append(
            Link(title=match.title, why=why[:80], score=match.score, source="judged")
        )
    return result


def _coerce_links(raw: Any) -> List[Dict[str, Any]]:
    if isinstance(raw, dict):
        for key in ("links", "related", "items"):
            if isinstance(raw.get(key), list):
                return [i for i in raw[key] if isinstance(i, dict)]
        if raw.get("title"):
            return [raw]
    if isinstance(raw, list):
        return [i for i in raw if isinstance(i, dict)]
    return []


# --------------------------------------------------------------------------
# writing it back
# --------------------------------------------------------------------------


def render_related(links: Sequence[Link]) -> str:
    lines = [RELATED_HEADING, "", MARKER, ""]
    for link in links:
        why = f" — {link.why}" if link.why else ""
        lines.append(f"- [[{link.title}]]{why}")
    lines.append("")
    return "\n".join(lines)


def apply(note: Note, links: Sequence[Link]) -> bool:
    """Write the `## Related` section into the note. Returns True if changed.

    Placed before `## Capture` where one exists, so the capture stays last and
    `_project_body`'s idempotent re-render keeps working; appended otherwise.
    """
    stripped = strip_related(note.body or "")
    if not links:
        changed = stripped != (note.body or "")
        note.body = stripped
        return changed

    section = render_related(links)
    marker = "\n## Capture"
    if marker in stripped:
        head, _, tail = stripped.partition(marker)
        rebuilt = f"{head.rstrip()}\n\n{section}\n## Capture{tail}"
    else:
        rebuilt = f"{stripped.rstrip()}\n\n{section}"

    changed = rebuilt.strip() != (note.body or "").strip()
    note.body = rebuilt
    return changed


# --------------------------------------------------------------------------
# the pipeline
# --------------------------------------------------------------------------


def connect_note(
    note: Note,
    index,
    cfg: Config,
    *,
    others: Sequence[Note] = (),
    matcher: Optional[re.Pattern] = None,
    max_links: int = MAX_LINKS,
    include_archive: bool = False,
    write: bool = True,
    allow_model: bool = True,
) -> ConnectResult:
    """Run the tiers in cost order, stopping as soon as `max_links` is full."""
    result = ConnectResult(note_id=note.id, title=note.title)
    links: List[Link] = []
    have = {slugify(note.title)}

    def room() -> int:
        return max_links - len(links)

    def take(new: Sequence[Link]) -> None:
        for link in new:
            if room() <= 0:
                return
            key = slugify(link.title)
            if key in have:
                continue
            have.add(key)
            links.append(link)

    # tier 0 — free. Facts first, then literal mentions.
    take(structural_links(note, others))
    if room() > 0:
        take(title_links(note, others, matcher))

    # tiers 1 and 2 — only if the free tiers left room to fill.
    if room() > 0 and index is not None and index.exists():
        found = candidates(
            note, index, include_archive=include_archive, exclude=[l.title for l in links]
        )
        result.candidates = len(found)
        sure, unsure = split_by_confidence(found)
        take(sure)
        if room() > 0 and unsure and allow_model:
            verdict = judge(note, unsure, cfg, max_links=room())
            result.provider = verdict.provider
            result.degraded = verdict.degraded
            result.used_model = verdict.used_model
            result.note = verdict.note
            take(verdict.links)

    result.links = links
    if write:
        result.changed = apply(note, links)
    return result


#: Buckets whose notes are worth *receiving* suggested links. Archive is
#: excluded deliberately: material lj retired should not grow new connections
#: on its own, and §7 keeps it out of default retrieval anyway.
CONNECTABLE = (Bucket.PROJECT, Bucket.RESOURCE)


def connectable(notes: Sequence[Note]) -> List[Note]:
    return [n for n in notes if n.bucket in CONNECTABLE]


# --------------------------------------------------------------------------
# re-run gating
# --------------------------------------------------------------------------


def fingerprint(note: Note) -> str:
    """Hash of a note's own content, ignoring the section we write into it.

    Including `## Related` would make every note look changed immediately
    after being connected, defeating the gate on the very next run.
    """
    from .index import fingerprint as note_fingerprint

    return note_fingerprint(
        Note(id=note.id, title=note.title, body=strip_related(note.body or ""))
    )


class ConnectState:
    """note_id → fingerprint at the time it was last connected.

    Lives in `_system/`, which is disposable by the rule set in phase 1: lose
    it and the next run reconnects everything, which is correct but slower.
    It is a cache, never a source of truth.
    """

    def __init__(self, cfg: Config):
        self.path = Path(cfg.vault) / "_system" / "connect.json"

    def load(self) -> Dict[str, str]:
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def save(self, state: Dict[str, str]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(state, indent=1), encoding="utf-8")
        except OSError:
            pass  # a cache that cannot be written is a slow run, not an error
