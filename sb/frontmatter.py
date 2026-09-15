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

    Tolerates the two things Windows editors add: a UTF-8 byte-order mark
    (Notepad, Word exports) — which used to hide the fence entirely, so the
    note read as "no frontmatter, no id" — and CRLF line endings. The opening
    fence must be exactly `---` on its own line; `----` is a horizontal rule.
    The closing fence is found with `str.find` rather than by splitting the
    whole file into lines, so a long note costs one scan of its header.
    """
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.startswith(FENCE):
        return {}, text
    if "\r" in text[:4096]:
        text = text.replace("\r\n", "\n")

    first = text.find("\n")
    if first == -1 or text[:first].strip() != FENCE:
        return {}, text

    # find the closing fence: a line that is `---` (surrounding blanks allowed)
    pos = first + 1
    n = len(text)
    while pos <= n:
        nl = text.find("\n", pos)
        line_end = n if nl == -1 else nl
        if text[pos:line_end].strip() == FENCE:
            break
        if nl == -1:
            return {}, text
        pos = nl + 1
    else:
        return {}, text

    raw_meta = text[first + 1 : pos]
    body = text[line_end + 1 :]
    try:
        meta = yaml.load(raw_meta, Loader=_Loader) or {}
    except yaml.YAMLError:
        return {}, text
    if not isinstance(meta, dict):
        return {}, text
    return meta, body.lstrip("\n")


def strip(text: str) -> str:
    """`text` without a leading frontmatter block.

    The one copy of this: generate, segment and index each had their own,
    and all three cut at the first line *starting* with `---`, so a note
    whose body opened with a horizontal rule lost everything up to the next
    one. Only a real fenced YAML mapping is removed now.
    """
    text = text or ""
    if not text.lstrip("\ufeff").startswith(FENCE):
        return text
    meta, body = parse(text)
    if meta:
        return body
    # An empty `---\n---` block is still frontmatter; anything else is prose.
    head = text.lstrip("\ufeff").replace("\r\n", "\n")
    if head.startswith(FENCE + "\n" + FENCE):
        rest = head[len(FENCE) * 2 + 1 :]
        if rest == "" or rest.startswith("\n"):
            return rest.lstrip("\n")
    return text


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
    if "\r" in body:
        body = body.replace("\r\n", "\n").replace("\r", "\n")
    buf.write(body.rstrip("\n") + "\n")
    return buf.getvalue()
