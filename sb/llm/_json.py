"""Getting JSON out of a small local model, reliably.

An 8B model asked for JSON will sometimes wrap it in prose or a code fence, or
emit trailing commas. This module is the tolerant reader that copes with that,
so callers can treat every provider as if it returned clean JSON.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict

_FENCE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract(text: str) -> Dict[str, Any]:
    """Best-effort parse of a model response into a dict. Raises ValueError."""
    if not text or not text.strip():
        raise ValueError("empty model response")

    candidates = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)

    # widest balanced {...} span
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        candidates.append(text[start : end + 1])

    for raw in candidates:
        raw = raw.strip()
        for attempt in (raw, _repair(raw)):
            try:
                parsed = json.loads(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError(f"could not parse JSON from model response: {text[:200]!r}")


def _repair(raw: str) -> str:
    raw = re.sub(r",\s*([}\]])", r"\1", raw)  # trailing commas
    raw = re.sub(r"//[^\n]*", "", raw)  # line comments
    return raw
