"""Does retrieval actually find the right note? (sprint 4, lane K1)

Every other quality dial in this system is measured — card quality has
`sb/quality.py`, the classifier has `thresholds`, the scheduler has `fit`.
Retrieval had nothing: `sb/index.py` was tested for *mechanism* (chunks are
written, vectors line up, archive stays out) and never once for *result*. So
"search works" meant "search returns rows", which is exactly the claim a
broken retriever also satisfies.

This is the missing half: a fixed set of questions, each naming the note that
genuinely answers it, run against the live index, scored.

## Why the question set is data and not code

The set outlives every number it produces. Today the index is keyword-only
(Ollama is not running here), the corpus is 67 accounting notes, and the
scores below are the keyword baseline. When embeddings come back the same
twenty questions re-run unchanged and the two columns are comparable — that
comparison is the entire point, and it only exists if the questions were
written down before the fix rather than after it.

So: `sb/evalsets/*.json`, one file per corpus, listing question, kind and the
note ids that answer it. Ids rather than titles because lj's own titles carry
typos ("Perpetual inventory Systerm") and a title match would score a hit as a
miss without saying why.

## Why some questions have no answer

A retriever that returns its best guess for every question looks perfect on a
set where every question has an answer. It is not perfect; it is unable to say
"I have nothing", which in a personal knowledge base is the failure that
matters — `sb/ask.py` refuses to answer when retrieval comes back empty, and
that refusal is only as good as the floor underneath it. Four of the twenty
questions are about things lj has never written a note on. Returning anything
for those is a false positive, and it is scored as one.

## What it measures

  hit@1 / hit@3 / hit@5   was an expected note in the top n
  MRR                     1/rank of the first expected note, averaged
  restraint               share of the unanswerable questions answered with
                          nothing at all

Rank is over the returned rows, using the same `k` and `per_note` the tutor
uses, because the number worth knowing is what `ask` would have seen — not
what a friendlier setting would have shown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .index import Index

EVALSETS = Path(__file__).resolve().parent / "evalsets"

#: How deep to look when ranking. Deeper than the six passages `ask` hands the
#: model, so a near-miss is reported as "rank 8" rather than as a flat miss —
#: the difference between a ranking problem and a recall problem, and they
#: have different fixes.
DEPTH = 10

#: Ranks the report calls out. hit@1 is what a one-shot answer sees; hit@5 is
#: what a model with room to read sees.
AT = (1, 3, 5)

ANSWERABLE = "answerable"
UNANSWERABLE = "unanswerable"


def load_set(name: str = "accounting_retrieval") -> Dict[str, Any]:
    """A question set by name (from `sb/evalsets/`) or by path."""
    path = Path(name)
    if not path.suffix:
        path = EVALSETS / f"{name}.json"
    if not path.exists():
        raise FileNotFoundError(f"no evaluation set at {path}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or not isinstance(doc.get("questions"), list):
        raise ValueError(f"{path} is not an evaluation set (no 'questions' list)")
    return doc


def run(
    index: Index,
    qset: Dict[str, Any],
    *,
    depth: int = DEPTH,
    per_note: int = 2,
    include_archive: bool = False,
) -> Dict[str, Any]:
    """Score one question set against one index.

    Every expectation is checked against what is actually indexed first. An
    expected note that is not in the index is a broken *question*, not a failed
    retrieval, and scoring it as a miss would quietly lower the number every
    time a note is renamed. Those questions are reported separately and left
    out of the totals.
    """
    chunks, vectors, _ = index.load()
    indexed = {c.get("note_id") for c in chunks}
    semantic = bool(vectors)

    rows: List[Dict[str, Any]] = []
    unknown: List[Dict[str, Any]] = []

    for q in qset.get("questions", []):
        expect = list(q.get("expect") or [])
        missing = [e for e in expect if e not in indexed]
        kind = q.get("kind", ANSWERABLE)
        hits = index.search(
            q.get("question", ""),
            k=depth,
            per_note=per_note,
            include_archive=include_archive,
        )
        rank: Optional[int] = None
        for i, hit in enumerate(hits, start=1):
            if hit.get("note_id") in expect:
                rank = i
                break
        row = {
            "id": q.get("id", ""),
            "question": q.get("question", ""),
            "kind": kind,
            "expect": expect,
            "rank": rank,
            "returned": len(hits),
            "top": (hits[0].get("title", "") if hits else ""),
            "top_score": (hits[0].get("score") if hits else None),
            "titles": [h.get("title", "") for h in hits[:5]],
            "note": q.get("note", ""),
        }
        if missing:
            row["unindexed_expectations"] = missing
            unknown.append(row)
            continue
        # An unanswerable question is answered correctly by returning nothing.
        row["hit"] = (rank is not None) if kind == ANSWERABLE else (len(hits) == 0)
        rows.append(row)

    answerable = [r for r in rows if r["kind"] == ANSWERABLE]
    unanswerable = [r for r in rows if r["kind"] != ANSWERABLE]

    summary: Dict[str, Any] = {
        "set": qset.get("name", ""),
        "semantic": semantic,
        "mode": "semantic" if semantic else "keyword-only",
        "chunks": len(chunks),
        "notes": len(indexed),
        "depth": depth,
        "answerable": len(answerable),
        "unanswerable": len(unanswerable),
        "mrr": round(
            sum(1.0 / r["rank"] for r in answerable if r["rank"]) / len(answerable), 3
        ) if answerable else 0.0,
        "restraint": round(
            sum(1 for r in unanswerable if r["hit"]) / len(unanswerable), 3
        ) if unanswerable else 0.0,
        "false_positives": sum(1 for r in unanswerable if not r["hit"]),
        "unknown_expectations": len(unknown),
    }
    for n in AT:
        summary[f"hit@{n}"] = sum(
            1 for r in answerable if r["rank"] and r["rank"] <= n
        )
    return {"summary": summary, "rows": rows, "unknown": unknown}


def format_report(result: Dict[str, Any], *, width: int = 62) -> str:
    """The table, as text. Same shape whether it ran on keywords or vectors."""
    s = result["summary"]
    out: List[str] = []
    out.append(f"{s['set']}  —  {s['mode']}, {s['chunks']} passages / {s['notes']} notes")
    out.append("")
    out.append(f"  {'id':4} {'rank':>5}  {'hit':3}  question")
    for row in result["rows"]:
        mark = "ok " if row["hit"] else "MISS"
        rank = str(row["rank"]) if row["rank"] else ("-" if row["kind"] == ANSWERABLE else "")
        if row["kind"] != ANSWERABLE:
            rank = f"{row['returned']}r"
        q = row["question"]
        if len(q) > width:
            q = q[: width - 1] + "…"
        out.append(f"  {row['id']:4} {rank:>5}  {mark:4} {q}")
    out.append("")
    at = "  ".join(f"hit@{n} {s[f'hit@{n}']}/{s['answerable']}" for n in AT)
    out.append(f"  answerable    {at}   MRR {s['mrr']}")
    out.append(
        f"  unanswerable  said nothing {s['unanswerable'] - s['false_positives']}"
        f"/{s['unanswerable']}   false positives {s['false_positives']}"
    )
    if s["unknown_expectations"]:
        out.append("")
        out.append(f"  {s['unknown_expectations']} question(s) name a note that is not indexed:")
        for row in result["unknown"]:
            out.append(f"    {row['id']}  {', '.join(row['unindexed_expectations'])}")
    return "\n".join(out)
