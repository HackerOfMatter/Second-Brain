"""Capture -> Project metadata (blueprint §3).

The strategy is *rules first, model second, rules again as a guard*:

  1. `extract.project_prior` computes what deterministic code can know --
     above all the deadline and the duration.
  2. The model is shown today's date and the prior, and asked to fill in the
     judgement calls: a step breakdown, the skills involved, the difficulty
     level, the ideal end state.
  3. The result is validated against `ProjectMeta`, and any field the model
     mangled falls back to the prior.

Step 3 matters more than it looks. An 8B local model will occasionally return
`"deadline": "next Friday"` or a date in the past; the guard means the worst
case is a Project with the rule-derived deadline rather than a broken note.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from typing import Any, Dict, List, Optional

from . import extract
from .config import Config
from .llm import resolve_provider
from .models import MaterialKind, Note, ProjectMeta, Step

SYSTEM_PROMPT = """You are the metadata parser for a personal PARA system. \
You convert a raw, messy capture into structured project metadata.

Rules:
- Output JSON only. No prose, no code fence.
- Never invent a deadline. If the capture does not state or imply one, use null.
- Steps must be concrete actions the person can start, in the order they should \
be done. 3-7 steps. Each has a realistic minute estimate.
- Materials are only things actually named in the capture, plus at most two \
obvious canonical sources. Do not pad the list. Give each one a kind: \
"hardware" for physical equipment, "software" for apps, accounts or licences, \
"material" for anything else (books, docs, links, supplies).
- Skills are the transferable capabilities the work builds, not restatements \
of the title.
- level: 1 trivial, 2 easy, 3 moderate, 4 hard, 5 expert.
- ideal_end: one sentence describing what "done" looks like, concretely enough \
that you could check it."""

SCHEMA_HINT = """{
  "title": "short noun phrase naming the work, no dates or durations",
  "deadline": "YYYY-MM-DD or null",
  "estimate_minutes": 240,
  "level": 3,
  "skills": ["..."],
  "materials": [{"text": "...", "kind": "material|hardware|software"}],
  "steps": [{"text": "...", "minutes": 45}],
  "learning": true,
  "ideal_end": "..."
}"""


class ParseResult:
    def __init__(
        self,
        meta: ProjectMeta,
        provider: str,
        degraded: bool,
        note: str = "",
        title: Optional[str] = None,
    ):
        self.meta = meta
        self.provider = provider
        self.degraded = degraded  # True when no model contributed
        self.note = note
        self.title = title  # the model's suggested title, if it gave a good one


def parse_project(text: str, cfg: Config, today: Optional[dt.date] = None) -> ParseResult:
    today = today or dt.date.today()
    prior = extract.project_prior(text, today)

    provider = resolve_provider(cfg.llm, "parse")
    if not getattr(provider, "is_llm", False):
        return ParseResult(
            _from_prior(prior),
            provider.name,
            degraded=True,
            note="No model reachable — used rule-based extraction. Re-parse later to enrich.",
        )

    try:
        raw = provider.complete_json(
            _user_prompt(text, prior, today), system=SYSTEM_PROMPT, schema_hint=SCHEMA_HINT
        )
    except Exception as exc:  # network, JSON, model, all the same to the caller
        if not cfg.llm.fallback_to_heuristic:
            raise
        return ParseResult(
            _from_prior(prior),
            provider.name,
            degraded=True,
            note=f"Model call failed ({type(exc).__name__}); used rule-based extraction.",
        )

    return ParseResult(
        _merge(raw, prior, today),
        provider.name,
        degraded=False,
        title=_clean_title(raw.get("title")),
    )


def _clean_title(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    title = value.strip().strip("\"'").rstrip(".")
    return title[:80] if 3 <= len(title) <= 120 else None


# --------------------------------------------------------------------------
# prompt
# --------------------------------------------------------------------------


def _user_prompt(text: str, prior: Dict[str, Any], today: dt.date) -> str:
    hints: List[str] = [f"Today is {today.isoformat()} ({today.strftime('%A')})."]
    if prior["deadline"]:
        hints.append(
            f"A date parser read the deadline as {prior['deadline'].isoformat()}. "
            "Use it unless the capture clearly means another date."
        )
    else:
        hints.append("A date parser found no deadline. Prefer null unless one is explicit.")
    if prior["steps"]:
        hints.append(
            "The capture already lists these steps; keep them, refine wording and "
            f"add estimates: {json.dumps(prior['steps'])}"
        )
    return (
        "\n".join(hints)
        + "\n\nRaw capture:\n\"\"\"\n"
        + text.strip()
        + "\n\"\"\"\n\nReturn the project metadata as JSON."
    )


# --------------------------------------------------------------------------
# validation / merge
# --------------------------------------------------------------------------


def _from_prior(prior: Dict[str, Any]) -> ProjectMeta:
    steps = [
        Step(id=f"s{i+1}", text=t, minutes=_split_minutes(prior["estimate_minutes"], len(prior["steps"])))
        for i, t in enumerate(prior["steps"])
    ]
    return ProjectMeta(
        deadline=prior["deadline"],
        deadline_confirmed=bool(prior.get("deadline_confirmed")),
        deadline_source=str(prior.get("deadline_kind") or ""),
        deadline_phrase=str(prior.get("deadline_phrase") or ""),
        estimate_minutes=int(prior["estimate_minutes"]),
        level=int(prior["level"]),
        skills=list(prior["skills"]),
        materials=list(prior["materials"]),
        steps=steps,
        learning=bool(prior["learning"]),
    )


def _split_minutes(total: int, n: int) -> int:
    return max(15, int(total / n)) if n else 30


def _merge(raw: Dict[str, Any], prior: Dict[str, Any], today: dt.date) -> ProjectMeta:
    deadline, source, phrase, confirmed = _settle_deadline(raw, prior, today)

    steps: List[Step] = []
    for i, item in enumerate(raw.get("steps") or []):
        if isinstance(item, str):
            text, minutes = item, 30
        elif isinstance(item, dict):
            text = str(item.get("text") or item.get("step") or "").strip()
            minutes = _coerce_int(item.get("minutes"), 30, lo=5, hi=480)
        else:
            continue
        if text:
            steps.append(Step(id=f"s{i+1}", text=text[:200], minutes=minutes))
    if not steps:
        return _from_prior({
            **prior,
            "deadline": deadline,
            "deadline_kind": source,
            "deadline_phrase": phrase,
            "deadline_confirmed": confirmed,
        })

    estimate = _coerce_int(raw.get("estimate_minutes"), 0, lo=0, hi=100_000)
    step_total = sum(s.minutes for s in steps)
    # trust the sum of the steps when the headline estimate disagrees wildly
    if estimate <= 0 or not (0.4 * step_total <= estimate <= 2.5 * step_total):
        estimate = step_total

    return ProjectMeta(
        deadline=deadline,
        deadline_confirmed=confirmed,
        deadline_source=source,
        deadline_phrase=phrase,
        estimate_minutes=estimate,
        level=_coerce_int(raw.get("level"), prior["level"], lo=1, hi=5),
        skills=_coerce_list(raw.get("skills")) or list(prior["skills"]),
        materials=_coerce_materials(raw.get("materials")) or list(prior["materials"]),
        steps=steps,
        learning=bool(raw.get("learning", prior["learning"])),
        ideal_end=(str(raw["ideal_end"])[:300] if raw.get("ideal_end") else None),
    )


def _settle_deadline(raw: Dict[str, Any], prior: Dict[str, Any], today: dt.date):
    """Decide the deadline, and record how sure we are of it.

    Three outcomes, and the middle one is the reason this function exists:

      * The model returned nothing usable — the rules' answer stands, with the
        confidence the rules assigned it.
      * The model agreed with the rules — likewise. Agreement is not extra
        evidence, but it is not a reason to downgrade either.
      * **The model returned a different date from the rules.** That is
        `llm`, and it is never confirmed. An 8B model quietly rewriting
        "sept 8" into a different Tuesday is the exact failure this catches;
        it is also occasionally the model correctly reading a phrase the rules
        missed, which is why the date is kept rather than discarded — but lj
        gets asked.
    """
    rules_date = prior["deadline"]
    kind = str(prior.get("deadline_kind") or extract.NONE)
    phrase = str(prior.get("deadline_phrase") or "")
    model_date = _coerce_date(raw.get("deadline"), today)

    if model_date and model_date != rules_date:
        raw_phrase = str(raw.get("deadline") or "")[:60]
        return model_date, extract.LLM, raw_phrase, False
    date = rules_date or model_date
    if not date:
        return None, "", "", False
    return date, kind, phrase, kind in extract.CONFIRMED_KINDS


def _coerce_date(value: Any, today: dt.date) -> Optional[dt.date]:
    if not value or not isinstance(value, str):
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", value)
    if m:
        try:
            d = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        # a model that returns a date in the distant past has hallucinated
        return d if d >= today - dt.timedelta(days=1) else None
    return extract.parse_deadline(value, today)


def _coerce_int(value: Any, default: int, lo: int, hi: int) -> int:
    try:
        n = int(float(value))
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def _coerce_materials(value: Any) -> List[Dict[str, str]]:
    """Materials, as either the old bare strings or the new {text, kind}.

    The model is asked for the second shape and a small one will sometimes
    still return the first, so both are accepted here rather than letting an
    unrecognised kind throw away a material lj will actually need. An
    unparseable kind falls back to "material" — a miscategorised entry is a
    cosmetic problem, a dropped one is not.
    """
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    kinds = {k.value for k in MaterialKind}
    out: List[Dict[str, str]] = []
    for item in value:
        if isinstance(item, dict):
            text = str(item.get("text") or item.get("name") or "").strip()
            kind = str(item.get("kind") or "").strip().lower()
        else:
            text, kind = str(item).strip(), ""
        if not text or text.lower() in ("none", "n/a", "null"):
            continue
        out.append(
            {
                "text": text[:120],
                "kind": kind if kind in kinds else MaterialKind.MATERIAL.value,
            }
        )
    return out[:12]


def _coerce_list(value: Any) -> List[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        s = str(item).strip()
        if s and s.lower() not in ("none", "n/a", "null"):
            out.append(s[:120])
    return out[:12]


# --------------------------------------------------------------------------
# note-level helper
# --------------------------------------------------------------------------


def apply_to_note(note: Note, cfg: Config, today: Optional[dt.date] = None) -> ParseResult:
    """Parse the note's body and attach the metadata, preserving any step
    completions already recorded (so re-parsing never loses your progress)."""
    result = parse_project(note.body, cfg, today)
    done_texts = {
        s.text.strip().lower() for s in (note.project.steps if note.project else []) if s.done
    }
    for step in result.meta.steps:
        if step.text.strip().lower() in done_texts:
            step.done = True
    if note.project:
        result.meta.status = note.project.status
        # A date lj picked by hand survives a re-parse. Re-reading the note is
        # a request to re-read the *note*, not to overrule a choice already
        # made — and the text it would re-read is the same text that produced
        # the guess lj corrected in the first place.
        if note.project.deadline_source == extract.MANUAL and note.project.deadline:
            result.meta.deadline = note.project.deadline
            result.meta.deadline_source = extract.MANUAL
            result.meta.deadline_confirmed = True
            result.meta.deadline_phrase = note.project.deadline_phrase
    note.project = result.meta
    if result.title:
        note.title = result.title
    note.log("parsed", f"provider={result.provider}{' (degraded)' if result.degraded else ''}")
    return result
