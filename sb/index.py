"""The retrieval index — the info-manager's memory (blueprint §7).

Everything lj has filed, searchable by meaning rather than by keyword, so the
tutor can answer a question about a note written six months ago and say which
note it came from.

## What lives where

`_system/index/` holds three files, and — unlike `_decks/` — this directory
*is* disposable. Every byte of it derives from the notes, so deleting it costs
one rebuild and nothing else. That is the same rule as the calendar, and it is
why the index is allowed to live under `_system/` at all.

    manifest.json   model, dimension, count, when it was built
    chunks.jsonl    one record per chunk: note, heading trail, text
    vectors.f32     raw little-endian float32, row-major, row i ↔ line i

Two files rather than one because the vectors are ten times the size of the
text and are never read by a human. Keeping them separate means the metadata
stays greppable, and a corrupt vector file costs a rebuild rather than the
whole index.

## Pure Python, on purpose

The whole system is five packages, and numpy is not one of them. Cosine
similarity over a few thousand chunks is a few million multiply-adds — tens of
milliseconds in plain Python, which is nothing next to the model call that
follows it. The scale this comfortably handles is roughly 5,000 chunks (a few
hundred notes); past that the honest answer is to add numpy rather than to
pretend a list comprehension is a vector database.

## Incremental by fingerprint

Re-indexing after editing one note re-embeds one note. Each chunk carries the
fingerprint of the note body it came from; on rebuild, chunks whose note is
unchanged are kept verbatim, chunks whose note has been edited or deleted are
dropped, and only the difference is sent to the model. A full rebuild of a
large vault is minutes; the incremental case is under a second.

## When there is no model

Retrieval degrades to keyword scoring rather than failing. Worse results,
clearly labelled, but a question asked with Ollama closed still gets an answer
out of the right notes — the same rule that governs capture and card
generation.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import struct
from array import array
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Config
from .llm import resolve_provider
from .models import Bucket, Note

MANIFEST = "manifest.json"
CHUNKS = "chunks.jsonl"
VECTORS = "vectors.f32"

#: Retrieval wants larger windows than card generation does — a question is
#: answered by a paragraph in context, while a flashcard comes from a sentence.
CHUNK_CHARS = 900
CHUNK_OVERLAP = 150
#: A floor for fragments, not for content. It exists to skip "---" and stray
#: words, so it is deliberately low: a passage under a heading is worth
#: indexing however short — "## Dosage / 400mg" is precisely the kind of thing
#: you go looking for, and dropping it because it is forty characters long
#: would make the index useless exactly where it should be sharpest.
MIN_CHUNK = 40

#: Below this, a "match" is noise. The tutor says it found nothing rather than
#: answering from general knowledge, because an info manager that quietly
#: stops using your notes is worse than one that admits it has nothing.
SCORE_FLOOR = 0.28

#: Exact terms matter even when the embedding shrugs — a course code or a
#: person's name carries more signal than its vector suggests. Small, so it
#: breaks ties rather than driving the ranking.
KEYWORD_WEIGHT = 0.15

#: The floor for a keyword-only search, where `SCORE_FLOOR` does not apply
#: because there are no cosines to compare it against.
#:
#: Sprint 4 lane K measured the old value (0.08) against twenty real questions
#: on lj's accounting notes: it let a single common word through, so "what are
#: the three sections of the statement of cash flows?" — a subject lj has never
#: written a note on — came back with ten passages whose only claim was the
#: word "cash". With scores now weighted by term rarity (`_query_weights`), a
#: score reads as "the share of the question's *information* this passage
#: covers", and a third of it is the point below which a match is one common
#: word rather than a subject. Tuned on twenty questions, not two hundred: it
#: is a floor that stops nonsense, not a precision dial.
KEYWORD_FLOOR = 0.34

STOPWORDS = {
    "the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "it", "that",
    "this", "for", "on", "with", "as", "by", "be", "was", "were", "at", "from",
    "what", "how", "why", "when", "which", "who", "do", "does", "did", "i",
    "my", "me", "you", "your", "about", "can", "should", "would", "there",
}


# --------------------------------------------------------------------------
# chunking
# --------------------------------------------------------------------------


def chunk_note(note: Note) -> List[Dict[str, Any]]:
    """Split one note into retrievable passages.

    Each chunk is prefixed with its heading trail. That costs a few tokens and
    buys a lot: "Dosage" under "## Ibuprofen" is a useless chunk on its own and
    a good one with its heading attached, and the embedding sees the
    difference.
    """
    body = _strip_frontmatter(note.body or "")
    out: List[Dict[str, Any]] = []
    heading = ""
    buffer: List[str] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n\n".join(buffer).strip()
        buffer.clear()
        # A short passage under a heading keeps its place: the heading is the
        # context that makes it findable, and it is about to be prefixed on.
        if len(text) < MIN_CHUNK and not heading:
            return
        if not text:
            return
        for piece in _window(text):
            prefix = f"{note.title} — {heading}\n\n" if heading else f"{note.title}\n\n"
            out.append({"heading": heading, "text": prefix + piece})

    for block in re.split(r"\n\s*\n", body):
        block = block.strip()
        if not block:
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", block)
        if m:
            flush()
            heading = m.group(2).strip()
            continue
        if sum(len(b) for b in buffer) + len(block) > CHUNK_CHARS:
            flush()
        buffer.append(block)
    flush()

    # A note whose whole body is shorter than one chunk still deserves to be
    # findable — a two-line capture is often exactly what you are looking for.
    if not out and body.strip():
        out.append({"heading": "", "text": f"{note.title}\n\n{body.strip()}"})
    if not out:
        out.append({"heading": "", "text": note.title})

    for i, chunk in enumerate(out):
        chunk["ord"] = i
    return out


def _window(text: str) -> List[str]:
    """Slide a window over an over-long passage, overlapping so a sentence
    split across the boundary survives in one piece somewhere."""
    if len(text) <= CHUNK_CHARS:
        return [text]
    pieces, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):  # prefer to break at a sentence
            dot = text.rfind(". ", start + MIN_CHUNK, end)
            if dot != -1:
                end = dot + 1
        pieces.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(start + 1, end - CHUNK_OVERLAP)
    return [p for p in pieces if len(p) >= MIN_CHUNK]


def _strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4 :]
    return text


def fingerprint(note: Note) -> str:
    """Changes when anything worth re-embedding changes — and not when only
    the scheduler touched the note."""
    raw = f"{note.title}\n{note.body}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# the store
# --------------------------------------------------------------------------


class Index:
    """The on-disk index, plus an in-process cache keyed by file mtime."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.dir = cfg.system_dir / "index"
        self._cache: Optional[Tuple[float, List[Dict[str, Any]], List[array], int]] = None
        #: (chunk-file stamp, per-passage term sets, document frequencies)
        self._terms_cache: Optional[Tuple[float, List[set], Dict[str, int]]] = None

    # -- paths ---------------------------------------------------------------

    @property
    def manifest_path(self) -> Path:
        return self.dir / MANIFEST

    @property
    def chunks_path(self) -> Path:
        return self.dir / CHUNKS

    @property
    def vectors_path(self) -> Path:
        return self.dir / VECTORS

    def exists(self) -> bool:
        return self.chunks_path.exists()

    # -- read ----------------------------------------------------------------

    def manifest(self) -> Dict[str, Any]:
        if not self.manifest_path.exists():
            return {}
        try:
            return json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def load(self) -> Tuple[List[Dict[str, Any]], List[array], int]:
        """Chunks and their vectors, cached until the file changes on disk."""
        if not self.exists():
            return [], [], 0
        stamp = self.chunks_path.stat().st_mtime
        if self._cache and self._cache[0] == stamp:
            return self._cache[1], self._cache[2], self._cache[3]

        chunks: List[Dict[str, Any]] = []
        for line in self.chunks_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                chunks.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        dim = int(self.manifest().get("dim") or 0)
        vectors: List[array] = []
        if dim and self.vectors_path.exists():
            raw = self.vectors_path.read_bytes()
            row = dim * 4
            for i in range(len(chunks)):
                block = raw[i * row : (i + 1) * row]
                if len(block) != row:
                    break
                vec = array("f")
                vec.frombytes(block)
                vectors.append(vec)
        # A vector file that does not line up with the chunk file is a broken
        # index, not a half-usable one. Drop to keyword scoring and say so in
        # `status()` rather than returning silently wrong neighbours.
        if len(vectors) != len(chunks):
            vectors = []

        self._cache = (stamp, chunks, vectors, dim)
        return chunks, vectors, dim

    def _corpus_terms(self) -> Tuple[List[set], Dict[str, int]]:
        """Every passage's terms, and how many passages each term appears in.

        Two things at once, both cached with the chunk file. The term sets
        mean a search tokenises the corpus once per *index*, where it used to
        re-tokenise every candidate passage once per *question*. The document
        frequencies are what make a rare word count for more than a common one
        — without them "shrinkage", which appears in one note, scores exactly
        as much as "cash", which appears in twenty.
        """
        chunks, _, _ = self.load()
        stamp = self.chunks_path.stat().st_mtime if self.exists() else 0.0
        if self._terms_cache and self._terms_cache[0] == stamp \
                and len(self._terms_cache[1]) == len(chunks):
            return self._terms_cache[1], self._terms_cache[2]

        sets = [_terms(c.get("text", "")) for c in chunks]
        df: Dict[str, int] = {}
        for terms in sets:
            for term in terms:
                df[term] = df.get(term, 0) + 1
        self._terms_cache = (stamp, sets, df)
        return sets, df

    def status(self) -> Dict[str, Any]:
        chunks, vectors, dim = self.load()
        man = self.manifest()
        notes = len({c.get("note_id") for c in chunks})
        return {
            "built": bool(chunks),
            "chunks": len(chunks),
            "notes": notes,
            "vectors": len(vectors),
            "dim": dim,
            "model": man.get("model", ""),
            "built_at": man.get("built_at", ""),
            "semantic": bool(vectors),
            # Which scoring path a search will take, so a slow vault is
            # diagnosable from `doctor` rather than from a stopwatch.
            "accelerated": bool(vectors) and len(vectors) >= NUMPY_AT_CHUNKS and _np() is not None,
            "numpy_available": _np() is not None,
            "numpy_at": NUMPY_AT_CHUNKS,
            "path": str(self.dir),
            "buckets": sorted({c.get("bucket", "") for c in chunks if c.get("bucket")}),
        }

    # -- write ---------------------------------------------------------------

    def build(
        self, notes: Sequence[Note], *, force: bool = False, progress=None
    ) -> Dict[str, Any]:
        """Bring the index in line with the vault.

        Incremental unless `force`: a note whose fingerprint is unchanged keeps
        its existing chunks and vectors untouched, so editing one note out of
        two hundred re-embeds one note.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        wanted = [n for n in notes if n.bucket in (Bucket.RESOURCE, Bucket.ARCHIVE)]

        old_chunks, old_vectors, old_dim = ([], [], 0) if force else self.load()
        keep_by_note: Dict[str, List[Tuple[Dict[str, Any], Optional[array]]]] = {}
        for i, chunk in enumerate(old_chunks):
            vec = old_vectors[i] if i < len(old_vectors) else None
            keep_by_note.setdefault(chunk.get("note_id", ""), []).append((chunk, vec))

        fresh: List[Dict[str, Any]] = []      # chunks needing an embedding
        reused: List[Tuple[Dict[str, Any], array]] = []
        stats = {"notes": len(wanted), "reused": 0, "embedded": 0, "removed": 0}

        for note in wanted:
            fp = fingerprint(note)
            cached = keep_by_note.pop(note.id, [])
            if cached and all(c.get("fingerprint") == fp for c, _ in cached) \
                    and all(v is not None for _, v in cached):
                reused.extend((c, v) for c, v in cached if v is not None)
                stats["reused"] += len(cached)
                continue
            for piece in chunk_note(note):
                fresh.append(
                    {
                        "id": f"{note.id}#{piece['ord']}",
                        "note_id": note.id,
                        "title": note.title,
                        "bucket": note.bucket.value,
                        "heading": piece["heading"],
                        "ord": piece["ord"],
                        "text": piece["text"],
                        "fingerprint": fp,
                    }
                )
        stats["removed"] = sum(len(v) for v in keep_by_note.values())

        # -- embed the difference
        provider = resolve_provider(self.cfg.llm)
        embedder = getattr(provider, "embed", None)
        dim = old_dim
        vectors_for_fresh: List[Optional[array]] = [None] * len(fresh)

        if fresh and callable(embedder):
            try:
                batch = 16
                done = 0
                for start in range(0, len(fresh), batch):
                    window = fresh[start : start + batch]
                    raw = embedder([c["text"] for c in window])
                    for offset, values in enumerate(raw):
                        vec = array("f", [float(x) for x in values])
                        _normalize(vec)
                        vectors_for_fresh[start + offset] = vec
                        dim = dim or len(vec)
                    done += len(window)
                    if progress:
                        progress(done, len(fresh))
                stats["embedded"] = sum(1 for v in vectors_for_fresh if v is not None)
            except Exception as exc:
                # `warning`, not `error`: chunks were still written and the
                # index still answers questions by keyword. The API client
                # treats an `error` key as a failed call, and a build that
                # degraded gracefully is not a failed call.
                stats["warning"] = f"embedding stopped ({type(exc).__name__}: {exc})"

        embedded = [
            (chunk, vec)
            for chunk, vec in zip(fresh, vectors_for_fresh)
            if vec is not None and len(vec) == dim
        ]
        unembedded = [
            chunk for chunk, vec in zip(fresh, vectors_for_fresh)
            if vec is None or len(vec) != dim
        ]

        # Everything that has a vector is written with its vector; anything the
        # model could not reach is still written, so it is findable by keyword
        # and picked up by the next build.
        rows: List[Tuple[Dict[str, Any], Optional[array]]] = list(reused) + list(embedded)
        rows.sort(key=lambda r: (r[0]["note_id"], r[0]["ord"]))
        rows += [(c, None) for c in unembedded]

        # Mixing rows with and without vectors would break the row-i-to-line-i
        # contract, so a partial embed writes no vector file at all and the
        # index runs on keywords until the next successful build.
        complete = all(v is not None for _, v in rows) and bool(rows) and bool(dim)

        with self.chunks_path.open("w", encoding="utf-8") as fh:
            for chunk, _ in rows:
                fh.write(json.dumps(chunk, ensure_ascii=False) + "\n")

        if complete:
            with self.vectors_path.open("wb") as fh:
                for _, vec in rows:
                    fh.write(struct.pack(f"<{len(vec)}f", *vec))
        elif self.vectors_path.exists():
            self.vectors_path.unlink()

        self.manifest_path.write_text(
            json.dumps(
                {
                    "model": self.cfg.llm.embed_model if complete else "",
                    "dim": dim if complete else 0,
                    "count": len(rows),
                    "semantic": complete,
                    "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        self._cache = None
        self._terms_cache = None
        stats["chunks"] = len(rows)
        stats["semantic"] = complete
        if not complete and "warning" not in stats:
            stats["warning"] = (
                "no embeddings — search is keyword-only until Ollama is running "
                f"with {self.cfg.llm.embed_model!r} pulled"
            )
        return stats

    # -- search --------------------------------------------------------------

    def search(
        self,
        question: str,
        *,
        k: int = 6,
        include_archive: bool = False,
        per_note: int = 2,
    ) -> List[Dict[str, Any]]:
        """Rank chunks against a question.

        Resources only unless asked otherwise (§7): day-to-day retrieval stays
        scoped to what is currently relevant, and reaching into Archive is a
        deliberate act rather than a silent default.

        `per_note` caps how much of the answer one note can supply. Without it
        a single long note wins every slot and the answer reads like a summary
        of that note rather than of what lj knows.
        """
        chunks, vectors, dim = self.load()
        if not chunks:
            return []

        allowed = {"resource"} | ({"archive"} if include_archive else set())
        candidates = [
            i for i, c in enumerate(chunks) if c.get("bucket", "resource") in allowed
        ]
        if not candidates:
            return []

        term_sets, doc_freq = self._corpus_terms()
        weights = _query_weights(_terms(question), doc_freq, len(term_sets))
        query_vec = self._embed_query(question) if vectors else None

        # One vectorised pass over the whole matrix when the vault is big
        # enough to justify it, one Python loop otherwise. Same numbers.
        cosine_by_index: List[float] = (
            cosines(query_vec, vectors, dim) if query_vec is not None else []
        )

        scored: List[Tuple[float, float, int]] = []
        for i in candidates:
            found = term_sets[i] if i < len(term_sets) else _terms(chunks[i].get("text", ""))
            keyword = _keyword_score(weights, found)
            cosine = cosine_by_index[i] if i < len(cosine_by_index) else 0.0
            score = cosine + KEYWORD_WEIGHT * keyword if query_vec is not None else keyword
            scored.append((score, cosine, i))

        scored.sort(key=lambda s: -s[0])
        floor = SCORE_FLOOR if query_vec is not None else KEYWORD_FLOOR

        out: List[Dict[str, Any]] = []
        per_note_count: Dict[str, int] = {}
        for score, cosine, i in scored:
            if score < floor:
                break
            chunk = chunks[i]
            note_id = chunk.get("note_id", "")
            if per_note_count.get(note_id, 0) >= per_note:
                continue
            per_note_count[note_id] = per_note_count.get(note_id, 0) + 1
            out.append(
                {
                    **chunk,
                    "score": round(float(score), 4),
                    "cosine": round(float(cosine), 4),
                    "semantic": query_vec is not None,
                }
            )
            if len(out) >= k:
                break
        return out

    def _embed_query(self, question: str) -> Optional[array]:
        provider = resolve_provider(self.cfg.llm)
        embedder = getattr(provider, "embed", None)
        if not callable(embedder):
            return None
        try:
            values = embedder([question])[0]
        except Exception:
            return None
        vec = array("f", [float(x) for x in values])
        _normalize(vec)
        return vec


# --------------------------------------------------------------------------
# maths
# --------------------------------------------------------------------------


def _normalize(vec: array) -> None:
    """Unit-length in place, so similarity is a plain dot product later.

    Normalising once at write time turns every subsequent comparison from a
    cosine into a dot — worth it when the dot is running in interpreted Python
    a few thousand times per question.
    """
    norm = math.sqrt(sum(x * x for x in vec)) or 1.0
    for i in range(len(vec)):
        vec[i] = vec[i] / norm


#: Roadmap Tier 4: numpy, on a trigger rather than on principle.
#:
#: Scoring a query is one dot product per chunk. In pure Python that is a
#: `sum(zip(...))` over 768 floats — a few hundred microseconds each, which is
#: nothing at 500 chunks and is most of a second at 5,000. The trigger the
#: phase 3 docs named is 5,000 chunks, roughly a few hundred notes, and it is
#: a real trigger rather than a guess: below it the import of numpy costs more
#: than the arithmetic it saves.
#:
#: numpy is deliberately **not** a requirement. The whole system runs on the
#: standard library plus four small packages, and a vault that never reaches
#: 5,000 chunks should never be asked to install a 60MB dependency. If it is
#: absent the pure-Python path runs, at any size, and `status()` says which
#: one is in use.
NUMPY_AT_CHUNKS = 5000

_numpy = None
_numpy_checked = False


def _np():
    """numpy if it is installed, else None. Imported once, lazily."""
    global _numpy, _numpy_checked
    if not _numpy_checked:
        _numpy_checked = True
        try:
            import numpy as np  # type: ignore

            _numpy = np
        except Exception:  # noqa: BLE001 — a missing optional dep is not an error
            _numpy = None
    return _numpy


def _dot(a: array, b: array) -> float:
    return sum(x * y for x, y in zip(a, b))


def cosines(query: array, vectors: Sequence[array], dim: int) -> List[float]:
    """Every chunk's cosine against the query, the fastest available way.

    Both sides are already unit vectors, so the cosine *is* the dot product —
    there is no normalisation step to get wrong, and the numpy path and the
    Python path compute the identical quantity rather than approximations of
    each other. Rows whose length does not match the query are scored 0, the
    same rule both paths follow, because a stale row is not a similar row.
    """
    if not vectors:
        return []
    np = _np() if len(vectors) >= NUMPY_AT_CHUNKS else None
    if np is not None:
        try:
            matrix = np.frombuffer(
                b"".join(v.tobytes() for v in vectors), dtype=np.float32
            ).reshape(len(vectors), dim)
            q = np.frombuffer(query.tobytes(), dtype=np.float32)
            if q.shape[0] == dim:
                return [float(x) for x in matrix @ q]
        except Exception:  # noqa: BLE001 — fall back rather than fail a search
            pass
    return [
        _dot(query, v) if len(v) == len(query) else 0.0
        for v in vectors
    ]


def _terms(text: str) -> set:
    return {
        w for w in re.findall(r"[a-z0-9]{2,}", (text or "").lower()) if w not in STOPWORDS
    }


def _query_weights(
    terms: Iterable[str], doc_freq: Dict[str, int], passages: int
) -> Dict[str, float]:
    """How much each word of the question is worth, by how rare it is.

    Plain inverse document frequency, smoothed. Two consequences, and both
    were measured on lj's accounting notes rather than assumed
    (`sb/reteval.py`):

    **A rare word decides the ranking.** "what is shrinkage and what causes
    it?" used to score the one note about shrinkage exactly level with two
    passages whose only match was the word "causes", and the tie was broken by
    note id — so the right answer came third. Weighted by rarity it comes
    first, because "shrinkage" is worth twenty times "causes" here.

    **A word the corpus has never seen is dropped rather than counted
    against.** It cannot be matched by any passage, so including it in the
    denominator would penalise every question containing a word lj happens not
    to have written down — which is most real questions. Dropped from both
    sides, the score keeps its meaning: the share of the *findable* part of
    the question that this passage covers. A question made entirely of unknown
    words weighs nothing at all, scores zero everywhere, and returns nothing,
    which is the correct answer to a question about a subject with no notes.
    """
    n = max(1, int(passages))
    out: Dict[str, float] = {}
    for term in set(terms):
        df = doc_freq.get(term, 0)
        if not df:
            continue
        out[term] = math.log((n + 1) / (df + 1)) + 1.0
    return out


def _keyword_score(weights: Dict[str, float], found: Iterable[str]) -> float:
    """The share of the question's weight this passage covers, 0..1."""
    total = sum(weights.values())
    if not total:
        return 0.0
    found = set(found)
    return sum(w for term, w in weights.items() if term in found) / total
