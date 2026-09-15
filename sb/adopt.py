"""Adopting notes lj already wrote — the ones sitting outside the schema.

The Drop folder (`sb/intake.py`) answers "a file arrived, where does it go?".
This module answers a different question: **"674 notes have been sitting in
`Drop/_Past/` for three sprints, already sorted by hand into subject folders,
and the vault can see them but cannot read them."** They are plain markdown —
sometimes with a little of lj's own frontmatter, usually with none — so
`vault.notes()` skips every one of them and every surface downstream (decks,
the tutor, the index, `doctor`) reports zero.

Intake is the wrong tool for that pile, for three reasons:

  * it *classifies*, and these are already classified — by lj, into folders,
    which is better evidence than any keyword table;
  * it moves the original into `Drop/_filed/`, and these are lj's only copy;
  * it only ever looks at the top level of `Drop/`, deliberately (see
    `intake.candidates`), so a sorted pile inside a subfolder is invisible to
    it by design. Nothing here changes that: adoption reads a subfolder that
    the watcher does not touch, and never writes into `Drop/` at all.

## The rules this module is held to

**Copy, never move.** The originals stay exactly where they are, byte for
byte, and every one is hashed before and after so "the originals are intact"
is a checked claim rather than a hope. Reversal deletes copies only.

**Derive, never invent.** Every field written here can be pointed at
something in the file or in the folder path it came from. Where a field
cannot be derived — and in this corpus that is mostly `created`, because a
third of the notes carry `Date: '[[<% tp.date.now("YYYY-MM-DD") %>]]'`, an
Obsidian Templater placeholder that never rendered and is not a date — the
value falls back to the filesystem and is *named* as provisional in
`adopted.provisional`. A guessed date that looks like a real one is worse
than no date, because nothing downstream can tell the difference.

**Refuse rather than fabricate.** An empty file has no title, no body and no
idea in it. There is no honest note to write, so it is skipped and reported,
not adopted as a stub that would make the resource count look better.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import frontmatter
from .models import Bucket, Note, ReviewMeta, now, slugify
from .vault import Vault

#: Where the undo record for each run is written, under the vault root.
MANIFEST_DIR = "_system/adopt"

#: Only markdown is adopted. Images and canvases are *carried* (see
#: `_referenced_assets`) because notes link to them, but they never become
#: notes themselves.
NOTE_SUFFIX = ".md"
ASSET_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".canvas", ".webp", ".svg"}

#: Frontmatter keys this system owns. Whatever lj put under these names is
#: read for evidence and then dropped, because writing it through unchecked is
#: how a note ends up with `created:` set to nothing and failing to parse.
OWNED_KEYS = {
    "id", "title", "bucket", "created", "updated", "tags", "source", "history",
    "category", "project", "srs", "habit", "schedule", "review", "intake",
    "body", "adopted",
}

#: An unrendered template placeholder. Templater (`<% ... %>`) and the core
#: Templates plugin (`{{date}}`) both leave these behind when a note is made
#: from a template outside the plugin's own flow. They are not values.
_PLACEHOLDER = re.compile(r"<%|{{")

#: `202507051439.md` — Obsidian's "unique note creator" stamp. The only
#: filename shape in this corpus that carries a real timestamp.
_STAMP_NAME = re.compile(r"^(\d{4})(\d{2})(\d{2})(\d{2})(\d{2})$")

_DATE_IN_TEXT = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

#: A drawing payload, not prose. Adopting one produces a Resource whose body
#: is a base64 blob.
_NOT_PROSE_KEYS = ("excalidraw-plugin",)


# --------------------------------------------------------------------------
# what one file would become
# --------------------------------------------------------------------------


@dataclass
class Decision:
    """What adoption would do with one source file, and why.

    Every field a note is written from is recorded next to the reason it holds
    that value, because the dry-run is the whole review surface: if the diff
    cannot be read, nobody can catch a wrong mapping before 674 notes carry
    it.
    """

    source: Path
    rel: str                       # vault-relative, POSIX, for reports
    action: str                    # "adopt" | "skip"
    reason: str = ""
    title: str = ""
    note_id: str = ""
    dest_rel: str = ""
    created: Optional[dt.datetime] = None
    created_from: str = ""         # frontmatter:<key> | filename | file-mtime
    provisional: List[str] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    kept: Dict[str, Any] = field(default_factory=dict)
    dropped: Dict[str, str] = field(default_factory=dict)
    words: int = 0

    @property
    def adopting(self) -> bool:
        return self.action == "adopt"


@dataclass
class Plan:
    """A whole run, before anything is written."""

    source_rel: str
    dest_rel: str
    decisions: List[Decision] = field(default_factory=list)
    assets: List[Tuple[Path, str]] = field(default_factory=list)  # (src, dest_rel)
    unreferenced_assets: List[str] = field(default_factory=list)
    missing_assets: List[str] = field(default_factory=list)
    limited: bool = False

    @property
    def adopting(self) -> List[Decision]:
        return [d for d in self.decisions if d.adopting]

    @property
    def skipping(self) -> List[Decision]:
        return [d for d in self.decisions if not d.adopting]

    def skips_by_reason(self) -> Dict[str, int]:
        out: Dict[str, int] = {}
        for d in self.skipping:
            out[d.reason] = out.get(d.reason, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))


# --------------------------------------------------------------------------
# derivation — every rule here has to point at something in the file
# --------------------------------------------------------------------------


def _usable(value: Any) -> bool:
    """Is this frontmatter value a value, or a hole?

    `tags:` with nothing after it parses as None; `Reviewed: "## Review"` is a
    heading someone pasted into a YAML field; `<% tp.date.now(...) %>` is a
    template that never ran. All three are holes, and all three are common
    here.
    """
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        return bool(text) and not _PLACEHOLDER.search(text)
    if isinstance(value, (list, dict)):
        return any(_usable(v) for v in (value if isinstance(value, list) else value.values()))
    return True


def derive_title(meta: Dict[str, Any], path: Path) -> str:
    """The filename, unless the note names itself.

    In this corpus the filename *is* the term — "Accounts recievable.md",
    "Cost of Goods Sold.md" — which is exactly the atomic-title shape
    `sb/lint.py` and the title-matching link tier want. A body heading is not
    used: "9 Steps of Accounting.md" opens with `## 1 [[General Journal]]`,
    which names its first step, not itself.
    """
    fm_title = meta.get("title")
    if isinstance(fm_title, str) and _usable(fm_title):
        return fm_title.strip()[:120]
    return path.stem.strip()[:120] or "Untitled"


def derive_created(meta: Dict[str, Any], path: Path) -> Tuple[dt.datetime, str, bool]:
    """(created, where it came from, provisional?).

    Order of evidence: a date lj wrote in the frontmatter, then a timestamp in
    the filename, then the file's own mtime. Only the first two are things lj
    stated; the mtime is the filesystem's opinion and is flagged as such, so
    nothing downstream mistakes it for a date the note claims.
    """
    for key in ("created", "Created", "date", "Date"):
        raw = meta.get(key)
        if not _usable(raw):
            continue
        parsed = _as_datetime(raw)
        if parsed:
            return parsed, f"frontmatter:{key}", False

    stamp = _STAMP_NAME.match(path.stem)
    if stamp:
        y, mo, d, h, mi = (int(g) for g in stamp.groups())
        try:
            return dt.datetime(y, mo, d, h, mi).astimezone(), "filename", False
        except ValueError:
            pass

    mtime = dt.datetime.fromtimestamp(path.stat().st_mtime).astimezone()
    return mtime.replace(microsecond=0), "file-mtime", True


def _as_datetime(raw: Any) -> Optional[dt.datetime]:
    if isinstance(raw, dt.datetime):
        return raw.astimezone() if raw.tzinfo is None else raw
    if isinstance(raw, dt.date):
        return dt.datetime(raw.year, raw.month, raw.day).astimezone()
    if isinstance(raw, str):
        hit = _DATE_IN_TEXT.search(raw)
        if hit:
            try:
                d = dt.date(int(hit.group(1)), int(hit.group(2)), int(hit.group(3)))
            except ValueError:
                return None
            return dt.datetime(d.year, d.month, d.day).astimezone()
    return None


def derive_tags(meta: Dict[str, Any], folder_parts: List[str]) -> List[str]:
    """lj's own tags, plus the folders lj filed the note under.

    The folder path is the strongest metadata in this pile — it is the one
    piece of classification a human did on purpose — so it becomes tags, in
    order, outermost first. Not `category`: that key is a colour keyword
    (`sb/taxonomy.py`) and setting it by hand overrides detection for good.
    """
    out: List[str] = []
    for part in folder_parts:
        tag = slugify(part, 40)
        if tag and tag not in out:
            out.append(tag)
    raw = meta.get("tags") if _usable(meta.get("tags")) else meta.get("tag")
    if isinstance(raw, str):
        raw = re.split(r"[,\s]+", raw)
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, (str, int, float)):
                continue
            tag = slugify(str(item).lstrip("#"), 40)
            if tag and tag not in out:
                out.append(tag)
    return out


def _partition_own_keys(meta: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """lj's non-schema keys: the ones worth keeping, and why the rest went.

    `type: Transaction` is lj's own vocabulary and survives as an extra key
    (`Note` is `extra="allow"` precisely so a human's keys are never
    clobbered). `Reviewed:` holding nothing does not survive — an empty key
    copied forward is noise that looks like data.
    """
    kept: Dict[str, Any] = {}
    dropped: Dict[str, str] = {}
    for key, value in meta.items():
        if key in OWNED_KEYS or key.lower() in OWNED_KEYS:
            dropped[key] = "read for evidence; this system owns the key"
        elif not _usable(value):
            dropped[key] = "empty or an unrendered template placeholder"
        else:
            kept[key] = value
    return kept, dropped


def _not_prose(meta: Dict[str, Any]) -> bool:
    return any(k in meta for k in _NOT_PROSE_KEYS)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------


def _already_adopted(vault: Vault, bucket: Bucket) -> Dict[str, str]:
    """source path -> the note already holding it.

    This is what makes a second run a no-op. It reads the destination bucket
    rather than trusting the manifest, because the manifest is a file lj can
    delete and the vault is the source of truth (blueprint §6) — and because a
    note moved or renamed in Obsidian after adoption is still adopted.
    """
    out: Dict[str, str] = {}
    for path, note in vault.notes(bucket):
        record = getattr(note, "adopted", None)
        if isinstance(record, dict) and record.get("source"):
            out[str(record["source"])] = note.id
    return out


def plan(
    vault: Vault,
    source: Path,
    bucket: Bucket = Bucket.RESOURCE,
    limit: Optional[int] = None,
) -> Plan:
    """Work out what adoption would do, touching nothing."""
    source = Path(source)
    if not source.is_absolute():
        source = vault.root / source
    source = source.resolve()
    if not source.is_dir():
        raise ValueError(f"{source} is not a folder")

    try:
        source_rel = source.relative_to(vault.root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError(f"{source} is outside the vault") from exc

    folder_parts = _subject_parts(source_rel)
    dest_rel = "/".join([_bucket_dir(bucket)] + folder_parts)
    seen_adopted = _already_adopted(vault, bucket)
    taken: set[str] = set()

    files = sorted(p for p in source.iterdir()
                   if p.is_file() and p.suffix.lower() == NOTE_SUFFIX)
    limited = limit is not None and limit < len(files)
    if limit is not None:
        files = files[:limit]

    decisions = [
        _decide(p, source_rel, dest_rel, folder_parts, seen_adopted, taken)
        for p in files
    ]
    assets, unreferenced, missing = _referenced_assets(source, decisions, dest_rel)
    return Plan(
        source_rel=source_rel,
        dest_rel=dest_rel,
        decisions=decisions,
        assets=assets,
        unreferenced_assets=unreferenced,
        missing_assets=missing,
        limited=limited,
    )


def _bucket_dir(bucket: Bucket) -> str:
    from .vault import BUCKET_DIRS

    return BUCKET_DIRS[bucket]


def _subject_parts(source_rel: str) -> List[str]:
    """"Drop/_Past/Accounting/Study Terms" -> ["Accounting", "Study Terms"].

    The staging prefix is scaffolding; the subject folders under it are lj's
    filing and are reproduced verbatim under the bucket, which is what makes
    `study --folder "30-Resources/Accounting"` select this pile.
    """
    parts = [p for p in source_rel.split("/") if p]
    while parts and (parts[0] in ("Drop",) or parts[0].startswith("_")):
        parts.pop(0)
    return parts


def _decide(
    path: Path,
    source_rel: str,
    dest_rel: str,
    folder_parts: List[str],
    seen_adopted: Dict[str, str],
    taken: set,
) -> Decision:
    rel = f"{source_rel}/{path.name}"
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return Decision(path, rel, "skip", f"unreadable ({type(exc).__name__})")

    meta, body = frontmatter.parse(raw)

    if not body.strip():
        return Decision(path, rel, "skip", "no body — nothing to adopt")
    if meta.get("id"):
        return Decision(path, rel, "skip", "already a Second Brain note (has an id)")
    if _not_prose(meta):
        return Decision(path, rel, "skip", "a drawing, not prose")
    if rel in seen_adopted:
        return Decision(path, rel, "skip", "already adopted", note_id=seen_adopted[rel])

    title = derive_title(meta, path)
    created, created_from, provisional = derive_created(meta, path)
    tags = derive_tags(meta, folder_parts)
    kept, dropped = _partition_own_keys(meta)

    note_id = _unique_id(created, title, taken, seen_adopted.values())
    return Decision(
        source=path,
        rel=rel,
        action="adopt",
        reason="markdown with a body, not yet in the vault",
        title=title,
        note_id=note_id,
        dest_rel=f"{dest_rel}/{slugify(title)}--{note_id[:15]}.md",
        created=created,
        created_from=created_from,
        provisional=["created"] if provisional else [],
        tags=tags,
        kept=kept,
        dropped=dropped,
        words=len(body.split()),
    )


def _unique_id(created: dt.datetime, title: str, taken: set, existing) -> str:
    """A stable id built from the note's own date and title.

    Deterministic on purpose. `models.new_id` stamps *now*, so adopting 93
    notes in one run would give all 93 the same `id[:15]`, and `Vault.find`
    resolves a stamp collision by opening every file that shares it — one
    "open a note" would read the whole pilot. Their own dates spread across a
    year, so this costs nothing and is reproducible besides.
    """
    base = f"{created.strftime('%Y%m%dT%H%M%S')}-{slugify(title, 40)}"
    candidate, n = base, 2
    used = set(taken) | set(existing)
    while candidate in used:
        candidate = f"{base}-{n}"
        n += 1
    taken.add(candidate)
    return candidate


def _referenced_assets(
    source: Path, decisions: List[Decision], dest_rel: str
) -> Tuple[List[Tuple[Path, str]], List[str], List[str]]:
    """Images a note links to travel with it.

    `[[Credit Terms.png]]` resolves by filename in Obsidian. Adopting the note
    without the image leaves a link pointing at nothing — a note that used to
    show a worked example and now shows a dead link is a note that got worse
    by being adopted.
    """
    wanted: set[str] = set()
    for d in decisions:
        if not d.adopting:
            continue
        text = d.source.read_text(encoding="utf-8", errors="replace")
        for target in re.findall(r"\[\[([^\]|#\n]+)", text):
            name = target.strip()
            if Path(name).suffix.lower() in ASSET_SUFFIXES:
                wanted.add(Path(name).name)

    on_disk = {p.name: p for p in source.iterdir()
               if p.is_file() and p.suffix.lower() in ASSET_SUFFIXES}
    assets = [(on_disk[n], f"{dest_rel}/{n}") for n in sorted(wanted) if n in on_disk]
    unreferenced = sorted(set(on_disk) - wanted)
    # Links that already pointed nowhere in this folder. Adoption does not
    # break them and cannot fix them, but it should say they exist rather
    # than let them look like damage it caused.
    missing = sorted(wanted - set(on_disk))
    return assets, unreferenced, missing


# --------------------------------------------------------------------------
# writing
# --------------------------------------------------------------------------


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_note(decision: Decision, run_id: str, cycle_days: int) -> Note:
    """The `Note` one decision becomes. Pure — writes nothing."""
    body = frontmatter.parse(
        decision.source.read_text(encoding="utf-8", errors="replace")
    )[1]
    stamp = now()
    record: Dict[str, Any] = {
        "source": decision.rel,
        "sha256": sha256(decision.source),
        "at": stamp.isoformat(),
        "run": run_id,
        "created_from": decision.created_from,
    }
    if decision.provisional:
        # Named, not implied. Anything reading this note can tell that its
        # `created` is the filesystem's answer rather than lj's.
        record["provisional"] = list(decision.provisional)

    note = Note(
        id=decision.note_id,
        title=decision.title,
        bucket=Bucket.RESOURCE,
        created=decision.created,
        updated=stamp,
        tags=list(decision.tags),
        source="import",
        body=body.strip(),
        review=ReviewMeta(
            cycle_days=cycle_days,
            next=dt.date.today() + dt.timedelta(days=cycle_days),
        ),
        adopted=record,
        **decision.kept,
    )
    note.log("adopted", f"from {decision.rel}")
    note.updated = stamp
    return note


def apply(
    vault: Vault,
    plan_: Plan,
    cycle_days: int = 90,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Write the plan, then prove the originals survived it.

    The verification is not decoration. Copying is the only step here that
    could touch lj's only copy of a note, so every source is hashed before the
    read and again after the write, and a mismatch is reported as a failure of
    the run rather than discovered later by a person opening a note.
    """
    run_id = run_id or now().strftime("%Y%m%dT%H%M%S")
    written: List[Dict[str, Any]] = []
    failures: List[Dict[str, str]] = []

    for d in plan_.adopting:
        before = sha256(d.source)
        try:
            note = build_note(d, run_id, cycle_days)
            target = vault.root / d.dest_rel
            target.parent.mkdir(parents=True, exist_ok=True)
            vault.write(note, target)
        except Exception as exc:  # one bad note must not abort the run
            failures.append({"source": d.rel, "error": f"{type(exc).__name__}: {exc}"})
            continue
        after = sha256(d.source)
        if after != before:
            failures.append({"source": d.rel, "error": "source changed during adoption"})
        written.append({
            "source": d.rel,
            "sha256": before,
            "dest": d.dest_rel,
            "id": d.note_id,
            "created_from": d.created_from,
            "provisional": d.provisional,
        })

    copied_assets: List[Dict[str, str]] = []
    for src, dest_rel in plan_.assets:
        target = vault.root / dest_rel
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            shutil.copy2(src, target)
        copied_assets.append({"source": _rel(vault, src), "dest": dest_rel})

    manifest = {
        "run": run_id,
        "at": now().isoformat(),
        "source": plan_.source_rel,
        "dest": plan_.dest_rel,
        "notes": written,
        "assets": copied_assets,
        "skipped": [
            {"source": d.rel, "reason": d.reason} for d in plan_.skipping
        ],
        "failures": failures,
    }
    # A run that adopted nothing has nothing to undo, and a folder of empty
    # manifests would make `--undo latest` ambiguous.
    manifest_path = (
        write_manifest(vault, manifest) if (written or copied_assets or failures) else None
    )
    vault.invalidate()
    return {
        "run": run_id,
        "adopted": len(written),
        "skipped": len(plan_.skipping),
        "assets": len(copied_assets),
        "failures": failures,
        "manifest": _rel(vault, manifest_path) if manifest_path else "",
        "originals_intact": verify_originals(vault, manifest),
    }


def _rel(vault: Vault, path: Path) -> str:
    try:
        return Path(path).resolve().relative_to(vault.root.resolve()).as_posix()
    except ValueError:
        return str(path)


def write_manifest(vault: Vault, manifest: Dict[str, Any]) -> Path:
    d = vault.root / MANIFEST_DIR
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"adopt-{manifest['run']}.json"
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return path


def load_manifest(vault: Vault, run: str = "latest") -> Dict[str, Any]:
    d = vault.root / MANIFEST_DIR
    runs = sorted(d.glob("adopt-*.json")) if d.is_dir() else []
    if not runs:
        raise ValueError(f"no adoption manifests in {MANIFEST_DIR}")
    path = runs[-1] if run in ("latest", "", None) else d / f"adopt-{run}.json"
    if not path.exists():
        raise ValueError(f"no manifest for run {run!r}")
    return json.loads(path.read_text(encoding="utf-8"))


def verify_originals(vault: Vault, manifest: Dict[str, Any]) -> Dict[str, Any]:
    """Re-hash every source the manifest names. Nothing here trusts memory."""
    missing: List[str] = []
    changed: List[str] = []
    for row in manifest.get("notes", []):
        src = vault.root / row["source"]
        if not src.exists():
            missing.append(row["source"])
        elif sha256(src) != row["sha256"]:
            changed.append(row["source"])
    return {
        "checked": len(manifest.get("notes", [])),
        "missing": missing,
        "changed": changed,
        "ok": not missing and not changed,
    }


def undo(vault: Vault, manifest: Dict[str, Any], dry_run: bool = False) -> Dict[str, Any]:
    """Remove exactly what one run wrote — and only if the originals are back.

    Three guards, because this is the one function here that takes a note
    away:

      * the original must still exist and still hash the same, so undo can
        never be the step that loses a note;
      * the file removed must be a note this run wrote, identified by the id
        in its own frontmatter, not by its path;
      * anything edited since adoption is left alone and reported, because an
        edit is lj's work and it is not this command's to throw away.

    Even past all three, the note is **retired rather than deleted** — moved
    to `40-Archive/_retired/` by `Vault.retire`. Nothing in this system
    deletes a note, and "this note is a duplicate of a file you still have"
    is an argument for getting it out of the way, not for destroying it. The
    adopted *assets* are still deleted outright: those are byte-identical
    copies, verified by hash against a source file that is right there, so
    keeping a third copy is hoarding rather than caution.
    """
    removed: List[str] = []
    kept: List[Dict[str, str]] = []
    intact = verify_originals(vault, manifest)
    if not intact["ok"]:
        return {"removed": [], "kept": [], "refused": intact, "dry_run": dry_run}

    for row in manifest.get("notes", []):
        target = vault.root / row["dest"]
        if not target.exists():
            kept.append({"dest": row["dest"], "why": "already gone"})
            continue
        meta, _ = frontmatter.parse(target.read_text(encoding="utf-8", errors="replace"))
        if meta.get("id") != row["id"]:
            kept.append({"dest": row["dest"], "why": "different note lives here now"})
            continue
        record = meta.get("adopted") or {}
        if record.get("run") != manifest["run"]:
            kept.append({"dest": row["dest"], "why": "written by another run"})
            continue
        if not dry_run:
            vault.retire(target, f"adopt undo, run {manifest['run']}")
        removed.append(row["dest"])

    for row in manifest.get("assets", []):
        target = vault.root / row["dest"]
        source = vault.root / row["source"]
        if target.exists() and source.exists() and sha256(target) == sha256(source):
            if not dry_run:
                target.unlink()
            removed.append(row["dest"])
        elif target.exists():
            kept.append({"dest": row["dest"], "why": "no longer matches the original"})

    if not dry_run:
        vault.invalidate()
    return {"removed": removed, "kept": kept, "refused": None, "dry_run": dry_run}
