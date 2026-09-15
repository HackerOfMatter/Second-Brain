"""Minimal YAML-frontmatter markdown reader/writer.

Deliberately dependency-light: the only requirement is PyYAML. Obsidian's
frontmatter is a `---` fenced YAML block at the very top of the file.
"""

from __future__ import annotations

import io
from typing import Any, Dict, Tuple

import yaml

FENCE = "---"

# libyaml when the wheel has it, PyYAML's pure-Python scanner when it does not.
#
# This is the single hottest line in the system. Opening the dashboard on a
# cold cache parses the frontmatter of every note in the vault, and a profile
# of that walk is ~90% PyYAML's Python scanner — `need_more_tokens`,
# `forward`, `check_token`, none of which are ours. `CSafeLoader` is the same
# grammar implemented in C and is several times faster; the PyPI wheels for
# Windows and Linux both ship it. Where they don't, `SafeLoader` is the same
# parser the system has always used, so the fallback costs speed and nothing
# else. `CSafeDumper` likewise for the write path, which runs on every save.
try:  # pragma: no cover - depends on how PyYAML was built, not on our code
    from yaml import CSafeDumper as _Dumper, CSafeLoader as _Loader
    ACCELERATED = True
except ImportError:  # pragma: no cover
    from yaml import SafeDumper as _Dumper, SafeLoader as _Loader
    ACCELERATED = False


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
        meta = yaml.load(raw_meta, Loader=_Loader) or {}
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
        yaml.dump(
            meta,
            buf,
            Dumper=_Dumper,
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=1000,
        )
    buf.write(FENCE + "\n\n")
    buf.write(body.rstrip("\n") + "\n")
    return buf.getvalue()
