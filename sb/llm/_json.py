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

    # widest {...} span
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

    # The first complete object, ignoring whatever prose follows it — a model
    # that adds "Note: {x} is optional" after its JSON defeats the widest span.
    if start != -1:
        decoder = json.JSONDecoder()
        for attempt in (text[start:], _repair(text[start:])):
            try:
                parsed, _ = decoder.raw_decode(attempt)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(parsed, dict):
                return parsed
    raise ValueError(f"could not parse JSON from model response: {text[:200]!r}")


def _repair(raw: str) -> str:
    """Drop `//` comments and trailing commas — outside strings only.

    The old regexes ran over the whole text, so a URL in a material
    (`"https://…"`) was cut at `//`, which broke the very string the repair
    was meant to save.
    """
    out = []
    i, n = 0, len(raw)
    in_str = False
    while i < n:
        c = raw[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(raw[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
        elif c == '"':
            in_str = True
            out.append(c)
        elif c == "/" and raw.startswith("//", i):
            nl = raw.find("\n", i)
            i = n if nl == -1 else nl
            continue
        elif c == "," and _TRAILING.match(raw, i):
            pass
        else:
            out.append(c)
        i += 1
    return "".join(out)


_TRAILING = re.compile(r",\s*(?://[^\n]*\s*)*[}\]]")
