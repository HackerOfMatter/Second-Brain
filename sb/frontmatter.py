"""Minimal YAML-frontmatter markdown reader/writer.

Deliberately dependency-light: the only requirement is PyYAML. Obsidian's
frontmatter is a `---` fenced YAML block at the very top of the file.
"""

from __future__ import annotations

import io
from typing import Any, Dict, Tuple

import yaml

FENCE = "---"


def parse(text: str) -> Tuple[Dict[str, Any], str]:
    """Split raw markdown into (frontmatter dict, body).

    A file with no frontmatter returns ({}, text).
    """
    if not text.startswith(FENCE):
        return {}, text

    lines = text.split("\n")
    # find the closing fence, starting after line 0
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == FENCE:
            end = i
            break
    if end is None:
        return {}, text

    raw_meta = "\n".join(lines[1:end])
    body = "\n".join(lines[end + 1 :])
    try:
        meta = yaml.safe_load(raw_meta) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body.lstrip("\n")


def dump(meta: Dict[str, Any], body: str) -> str:
    """Render frontmatter + body back to a markdown string."""
    buf = io.StringIO()
    buf.write(FENCE + "\n")
    if meta:
        yaml.safe_dump(
            meta,
            buf,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        )
    buf.write(FENCE + "\n\n")
    buf.write(body.rstrip("\n") + "\n")
    return buf.getvalue()
