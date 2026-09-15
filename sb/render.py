"""Rendering a note body from its template.

The templates in `_templates/` were, until this module existed, documentation:
they described the shape a note ought to have, and the engine went off and built
its bodies in Python from scratch. Two sources of truth for one thing, and the
predictable result — a note written by hand and a note written by the capture
box did not look alike, and neither matched the template that claimed to define
both.

So the template is now the shape, and this is what makes that literal. The
engine hands over what it *knows* — a definition, an image embed, a list of
terms — and the template decides which headings exist, in what order, and what
prose sits between them. Editing `_templates/Term.md` changes what the capture
box writes, immediately, with no code change.

## What is taken from the template, and what is not

**Taken:** the `##` headings, their order, the preamble under `# Title`, and any
plain text the template puts inside a section. That is the shape and the look.

**Not taken:** the frontmatter's structural fields. `id`, `title`, `bucket`,
`created` and `updated` are the system's, always, because a note whose bucket
came from a template lj was halfway through editing is a note in the wrong
folder. What *is* read from the template's frontmatter is the small set of
per-objective defaults that are genuinely the template's business — `tags`,
`category`, and the review cycle — which is what makes "a Term is on a 90-day
cycle and tagged `term`" a fact about the Term template rather than a constant
buried in `engine.py`.

## HTML comments are stripped from generated notes

The templates carry a lot of guidance, and it is good guidance — it is the
reason lj wrote them. It belongs in a note being filled in by hand. It does not
belong six hundred times over in a vault of term notes, where it would outweigh
the content by an order of magnitude and make every card generated from the note
worse.

The rule is therefore: **`<!-- ... -->` is for the human at the template; plain
text is for every note made from it.** One rule, visible in the file, and it
gives a place to put an explanation without paying for it forever.

## Nothing here may break a capture

A template is a file lj edits, which means at some point it will be broken, or
half-saved, or missing. `TemplateStore.get` falls back to the built-in copy in
`sb/templates.py` and records that it did; `body_for` falls back again to a bare
`# Title` if even that fails. A thought must not be lost because a template was
being edited at the time — the same rule the Drop folder fallback follows, for
the same reason.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import frontmatter
from .templates import FOR_OBJECTIVE, TEMPLATES

#: Frontmatter the template is allowed to decide. Everything else about a note's
#: identity is the system's — see the module docstring.
TEMPLATE_DEFAULTS = ("tags", "category", "review")

_HEADING = re.compile(r"^(#{2,6})\s+(.*?)\s*$")
_TITLE = re.compile(r"^#\s+.*$", re.M)
_COMMENT = re.compile(r"<!--.*?-->", re.S)

#: Obsidian's core-template placeholders. Rendered here so a generated note and
#: a note lj made with Obsidian's own Insert Template command come out the same.
_PLACEHOLDER = re.compile(r"\{\{\s*(title|date|time)(?::([^}]*))?\s*\}\}")

_STRFTIME = [
    ("YYYY", "%Y"), ("MM", "%m"), ("DD", "%d"),
    ("HH", "%H"), ("mm", "%M"), ("ss", "%S"),
]


def _moment(fmt: str, at: dt.datetime) -> str:
    """Obsidian uses moment.js tokens; Python uses strftime. Only the six that
    appear in these templates are translated — an unrecognised format is left
    as typed rather than guessed at, so a mistake is visible in the note
    instead of silently becoming the wrong date."""
    out = fmt
    for token, code in _STRFTIME:
        out = out.replace(token, code)
    try:
        return at.strftime(out)
    except ValueError:
        return fmt


def fill_placeholders(text: str, title: str, at: Optional[dt.datetime] = None) -> str:
    at = at or dt.datetime.now()

    def sub(m: "re.Match") -> str:
        kind, fmt = m.group(1), m.group(2)
        if kind == "title":
            return title
        if kind == "date":
            return _moment(fmt or "YYYY-MM-DD", at)
        return _moment(fmt or "HH:mm:ss", at)

    return _PLACEHOLDER.sub(sub, text)


def strip_comments(text: str) -> str:
    """Drop `<!-- ... -->` and tidy up the blank lines they leave behind."""
    out = _COMMENT.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", out).strip()


@dataclass
class Template:
    """One template, parsed into the parts the engine can fill in."""

    name: str
    meta: Dict[str, Any] = field(default_factory=dict)
    preamble: str = ""
    #: (heading text, template content) in the order the template lists them.
    sections: List[Tuple[str, str]] = field(default_factory=list)
    source: str = "builtin"   # builtin | file
    error: str = ""

    @property
    def headings(self) -> List[str]:
        return [h for h, _ in self.sections]

    def section(self, name: str) -> str:
        want = name.strip().lower().rstrip(":")
        for heading, content in self.sections:
            if heading.strip().lower().rstrip(":") == want:
                return content
        return ""

    def defaults(self) -> Dict[str, Any]:
        """The per-objective frontmatter this template is allowed to set."""
        return {k: v for k, v in self.meta.items()
                if k in TEMPLATE_DEFAULTS and v not in (None, "", [])}


def parse(name: str, text: str, source: str = "file") -> Template:
    meta, body = frontmatter.parse(text or "")
    body = _TITLE.sub("", body, count=1)

    preamble_lines: List[str] = []
    sections: List[Tuple[str, str]] = []
    heading: Optional[str] = None
    buf: List[str] = []
    for line in body.splitlines():
        m = _HEADING.match(line)
        # `###` and deeper belong to the section above them — they are
        # structure *within* a section (a Project's Hardware subsection, an
        # Assignment's per-question answers), not sections of their own.
        if m and len(m.group(1)) == 2:
            if heading is None:
                preamble_lines = buf
            else:
                sections.append((heading, "\n".join(buf)))
            heading, buf = m.group(2).strip(), []
        else:
            buf.append(line)
    if heading is None:
        preamble_lines = buf
    else:
        sections.append((heading, "\n".join(buf)))

    return Template(
        name=name,
        meta=meta if isinstance(meta, dict) else {},
        preamble="\n".join(preamble_lines),
        sections=sections,
        source=source,
    )


class TemplateStore:
    """The templates on disk, cached until they change.

    Keyed on (mtime_ns, size) like `Vault._parsed`, and for the same reason: lj
    edits these in Obsidian while the app is running, and an edit must take
    effect on the next capture rather than on the next restart — that is most
    of what makes "the template is the shape" worth having.
    """

    def __init__(self, vault_root: Path):
        self.root = Path(vault_root) / "_templates"
        self._cache: Dict[str, Tuple[Optional[Tuple[int, int]], Template]] = {}

    def path_for(self, name: str) -> Path:
        return self.root / f"{name}.md"

    def get(self, name: str) -> Template:
        """The template by name, from disk when it is readable and from the
        built-in copy when it is not. Never raises."""
        path = self.path_for(name)
        try:
            st = path.stat()
            key: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None

        hit = self._cache.get(name)
        if hit is not None and hit[0] == key:
            return hit[1]

        tpl: Optional[Template] = None
        if key is not None:
            try:
                tpl = parse(name, path.read_text(encoding="utf-8"), source="file")
                if not tpl.meta and not tpl.sections:
                    # No frontmatter *and* no headings is not a template lj
                    # wrote that way — it is a file that is empty, half-saved,
                    # or whose fence never closed. A template with headings and
                    # no frontmatter is honoured as written; the fallback is
                    # for files that carry no shape at all.
                    tpl = None
            except Exception as exc:  # noqa: BLE001 — a capture must not fail
                tpl = parse(name, TEMPLATES.get(f"{name}.md", ""), source="builtin")
                tpl.error = f"{type(exc).__name__}: {exc}"
        if tpl is None:
            tpl = parse(name, TEMPLATES.get(f"{name}.md", ""), source="builtin")

        self._cache[name] = (key, tpl)
        return tpl

    def for_objective(self, objective: str) -> Template:
        """The template for an objective ("term") or named outright
        ("Atomic Note").

        Both spellings are accepted because both are used: the engine asks by
        objective, and `capture.default_template` in config.yaml names a
        template directly — lj should be able to write the name of a file they
        can see in `_templates/` rather than look up an internal key.
        """
        name = str(objective or "").strip()
        if f"{name}.md" in TEMPLATES or self.path_for(name).exists():
            return self.get(name)
        return self.get(FOR_OBJECTIVE.get(name.lower(), FOR_OBJECTIVE["capture"]))

    def body_for(
        self,
        objective: str,
        title: str,
        filled: Optional[Dict[str, str]] = None,
        *,
        preamble: str = "",
        extra: Optional[Sequence[Tuple[str, str]]] = None,
        at: Optional[dt.datetime] = None,
        keep_empty: bool = True,
    ) -> str:
        """Render a note body in the template's shape.

        `filled` maps heading name (case-insensitively) to the content the
        engine has for it. A heading with nothing to put in it is still
        written, empty — that is the entire point of a template: the empty
        `## In my own words` on six hundred notes is what turns "did I ever
        process this?" into something the system can answer.

        `extra` appends sections the template does not name — a session's
        chapter-specific lists, say — after the ones it does.
        """
        tpl = self.for_objective(objective)
        filled = {k.strip().lower().rstrip(":"): v for k, v in (filled or {}).items()}

        lines: List[str] = [f"# {title}", ""]
        head = preamble or strip_comments(fill_placeholders(tpl.preamble, title, at))
        if head:
            lines += [head, ""]

        for heading, template_content in tpl.sections:
            key = heading.strip().lower().rstrip(":")
            given = (filled.get(key) or "").strip()
            if not given:
                # Plain text in the template is part of the note; the comments
                # around it are instructions for whoever fills it in by hand.
                given = strip_comments(fill_placeholders(template_content, title, at))
            if not given and not keep_empty:
                continue
            lines += [f"## {heading}", ""]
            if given:
                lines += [given, ""]

        for heading, content in (extra or []):
            if not (content or "").strip() and not keep_empty:
                continue
            lines += [f"## {heading}", ""]
            if (content or "").strip():
                lines += [content.strip(), ""]

        return "\n".join(lines).rstrip() + "\n"


def wants_shape(text: str) -> bool:
    """Does this text need a template's structure, or does it have its own?

    A line typed into the capture box has no structure and is exactly what a
    template is for. A nine-thousand-word document dropped into `Drop/` already
    has its own headings, and folding it under a template's `## Definition`
    would bury an author's structure inside ours — which is the opposite of
    legible.

    So the rule is: **the template supplies structure to text that has none,
    and never overrides structure someone already wrote.**
    """
    body = strip_comments(text or "")
    if not body:
        return True
    if any(_HEADING.match(line) for line in body.splitlines()):
        return False
    return True


def check(store: TemplateStore) -> List[Dict[str, Any]]:
    """One row per template: does it still parse, and does it still describe a
    note this system can read?

    The check `doctor` runs. It is not about style — it is about the specific
    failure that took this vault out for three weeks, where the templates went
    on looking fine in Obsidian while producing notes with no id and no bucket.
    So it does the only test that would have caught that: build a real `Note`
    from the template and see what happens.
    """
    from .models import Note  # local: keeps this module importable standalone

    rows: List[Dict[str, Any]] = []
    for filename in sorted(TEMPLATES):
        name = filename[:-3]
        path = store.path_for(name)
        row: Dict[str, Any] = {
            "template": name,
            "exists": path.exists(),
            "ok": False,
            "problems": [],
        }
        if not path.exists():
            row["problems"].append("missing — `run.py templates` writes it back")
            rows.append(row)
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            row["problems"].append(f"unreadable: {exc}")
            rows.append(row)
            continue

        meta, body = frontmatter.parse(raw)
        filled_meta = {
            k: fill_placeholders(v, "Example", None) if isinstance(v, str) else v
            for k, v in (meta or {}).items()
        }
        if not meta:
            row["problems"].append("no frontmatter the parser can read")
        # The signature of the Properties-editor corruption: it stops where it
        # cannot parse and leaves the rest of the YAML in the body.
        if re.search(r"^---\s*$", body, re.M):
            row["problems"].append(
                "a stray `---` in the body — frontmatter was truncated, and the "
                "rest of it is sitting in the note"
            )
        block = raw.split("---", 2)[1] if raw.startswith("---") and raw.count("---") >= 2 else ""
        if re.search(r"(^|\s)#", block):
            row["problems"].append(
                "a `#` comment inside the frontmatter — Obsidian's Properties "
                "editor will swallow it into a value"
            )
        try:
            note = Note.from_frontmatter(dict(filled_meta), body)
            row["bucket"] = note.bucket.value
        except Exception as exc:
            row["problems"].append(f"does not build a note: {type(exc).__name__}")

        row["headings"] = parse(name, raw).headings
        row["ok"] = not row["problems"]
        rows.append(row)
    return rows
