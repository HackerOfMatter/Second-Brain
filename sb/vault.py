"""Obsidian vault I/O.

The vault is the single source of truth (blueprint §6). Nothing in this system
keeps a second authoritative copy of a note: the dashboard, the calendar and
(later) the RAG index are all derived views that can be rebuilt by re-reading
these files. That is what makes the vault safe to edit by hand in Obsidian.

Folder layout, numbered so Obsidian's file explorer sorts them in PARA order:

    00-Inbox/      unclassified captures (should stay near-empty)
    10-Areas/      ongoing responsibilities; habits live here
    20-Projects/   deadline-bound work
    30-Resources/  reference material; the default RAG corpus
    40-Archive/    retired material; searched only on request
    _system/       machine state: calendar/, index/, logs/  (underscore-prefixed
                   so it sorts out of the way; add to Obsidian's excluded files)
    _templates/    Obsidian templates matching the schema in models.py
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import uuid
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

from . import frontmatter
from .models import Bucket, Note

BUCKET_DIRS: Dict[Bucket, str] = {
    Bucket.INBOX: "00-Inbox",
    Bucket.AREA: "10-Areas",
    Bucket.PROJECT: "20-Projects",
    Bucket.RESOURCE: "30-Resources",
    Bucket.ARCHIVE: "40-Archive",
}

SYSTEM_DIRS = ["_system", "_system/calendar", "_system/index", "_system/logs", "_templates"]

#: The `--20260822T110348` suffix `path_for` stamps onto every managed file.
#: Its presence means the filename still names which note this is; its absence
#: means a human renamed the file and only the frontmatter knows.
_STAMPED = re.compile(r"--\d{8}T\d{6}$")


class VaultError(ValueError):
    """Subclasses ValueError so the API layer reports it as a 400, not a 500:
    'no note with that id' is a bad request, not a broken server."""


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()

    # -- structure ----------------------------------------------------------

    def ensure_structure(self) -> None:
        """Create the PARA folders. Safe to call on an existing vault: it only
        ever adds directories, never touches or moves existing notes."""
        self.root.mkdir(parents=True, exist_ok=True)
        for name in list(BUCKET_DIRS.values()) + SYSTEM_DIRS:
            (self.root / name).mkdir(parents=True, exist_ok=True)
        gitignore = self.root / "_system" / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text("token.json\n*.log\nindex/\n", encoding="utf-8")

    def dir_for(self, bucket: Bucket) -> Path:
        return self.root / BUCKET_DIRS[bucket]

    # -- read ---------------------------------------------------------------

    def read(self, path: Path) -> Note:
        raw = Path(path).read_text(encoding="utf-8")
        meta, body = frontmatter.parse(raw)
        if not meta.get("id"):
            raise VaultError(f"{path} has no Second Brain frontmatter (missing id)")
        return Note.from_frontmatter(meta, body)

    def iter_paths(self, bucket: Optional[Bucket] = None) -> Iterator[Path]:
        buckets = [bucket] if bucket else list(BUCKET_DIRS)
        for b in buckets:
            d = self.dir_for(b)
            if not d.exists():
                continue
            for p in sorted(d.rglob("*.md")):
                yield p

    def notes(self, bucket: Optional[Bucket] = None) -> List[Tuple[Path, Note]]:
        """All managed notes. Files without our frontmatter are skipped, so an
        existing vault full of hand-written notes coexists with the system."""
        out: List[Tuple[Path, Note]] = []
        for p in self.iter_paths(bucket):
            try:
                out.append((p, self.read(p)))
            except (VaultError, UnicodeDecodeError):
                continue
        return out

    def find(self, note_id: str) -> Optional[Tuple[Path, Note]]:
        """Locate a note by id, reading one file where possible.

        This is the hottest path in the system — `note()`, `save()` and every
        mutation go through it — and it used to parse every file in the vault
        to check an id that `path_for` had already written into the filename
        (`{slug}--{id[:15]}.md`). Matching the filename first turns "open
        one note" from a whole-vault cost into a directory listing plus one
        read.

        The full scan survives as a fallback for files renamed by hand in
        Obsidian, which no longer carry the id. Correctness first: the id in
        the frontmatter is still what decides, the filename only proposes.
        """
        stamp = note_id[:15]
        paths = list(self.iter_paths())

        if stamp:
            for path in paths:
                if path.stem.endswith(f"--{stamp}"):
                    note = self._try_read(path)
                    if note is not None and note.id == note_id:
                        return path, note

        # Nothing matched by name. Rather than parsing the whole vault, read
        # only the files that *could* have been renamed by hand — the ones
        # carrying no id stamp at all. A file stamped with somebody else's id
        # is somebody else's note, and opening it proves nothing.
        for path in paths:
            if _STAMPED.search(path.stem):
                continue
            note = self._try_read(path)
            if note is not None and note.id == note_id:
                return path, note

        # Last resort: a stamped file whose stamp disagrees with its own
        # frontmatter. Shouldn't happen, but the id in the file is the
        # authority and a note that exists must always be findable.
        for p, n in self.notes():
            if n.id == note_id:
                return p, n
        return None

    def _try_read(self, path: Path) -> Optional[Note]:
        try:
            return self.read(path)
        except (VaultError, UnicodeDecodeError, OSError):
            return None

    def get(self, note_id: str) -> Tuple[Path, Note]:
        hit = self.find(note_id)
        if not hit:
            raise VaultError(f"no note with id {note_id!r}")
        return hit

    # -- link resolution ----------------------------------------------------
    #
    # `[[Some Note]]` has to become a real note without reading the vault.
    # Parsing every file to build a title index is what turns an innocuous
    # operation into a whole-vault cost, and it is pure waste here: `path_for`
    # names every file `{slugify(title)}--{id[:15]}.md`, so the title is
    # already in the filename. Listing paths is one directory walk with no
    # reads, and only the notes actually linked are ever opened.

    def title_index(self) -> Dict[str, Path]:
        """Map slugified title → path, from filenames alone. No file reads.

        Two keys per file, because a vault is edited by a human as well as by
        this app: the slug this system would have generated, and the slug of
        whatever the file happens to be called now. Renaming a note in
        Obsidian renames its file, and its links should still resolve.
        """
        from .models import slugify

        index: Dict[str, Path] = {}
        for path in self.iter_paths():
            stem = path.stem
            managed = stem.rsplit("--", 1)[0] if "--" in stem else stem
            for key in (managed, slugify(stem)):
                if key:
                    index.setdefault(key, path)
        return index

    def resolve_title(
        self, title: str, index: Optional[Dict[str, Path]] = None
    ) -> Optional[Note]:
        """`[[Some Note]]` → that note, or None if it points nowhere yet.

        An unresolved link is normal rather than an error: a roadmap names its
        steps as empty links long before those notes exist. Pass `index` to
        resolve many links against one directory walk.
        """
        from .models import slugify

        key = slugify((title or "").strip())
        if not key:
            return None
        path = (self.title_index() if index is None else index).get(key)
        if path is None:
            return None
        try:
            return self.read(path)
        except (VaultError, UnicodeDecodeError, OSError):
            return None

    # -- write --------------------------------------------------------------

    def path_for(self, note: Note) -> Path:
        from .models import slugify

        return self.dir_for(note.bucket) / f"{slugify(note.title)}--{note.id[:15]}.md"

    def write(self, note: Note, path: Optional[Path] = None) -> Path:
        """Write a note atomically: temp file, then replace.

        The temp name carries a unique suffix rather than a fixed `.md.tmp`.
        Two writes to the same note can genuinely overlap — the server runs
        engine calls in a threadpool, and a UI that fires a change event per
        keystroke-ish interaction (the date pickers do) will send two requests
        for one note within milliseconds. With a shared temp name, both
        writers create the same file, the first `replace` consumes it, and the
        second fails with FileNotFoundError *after* the first has already
        succeeded — a write that reports failure having actually happened,
        which is the worst kind.

        Both writers still race for the final `replace`, and that is fine:
        `os.replace` is atomic, so the loser is simply overwritten. The
        invariant that matters is that the note on disk is always one complete
        version of itself, never a truncated blend and never absent.
        """
        self.ensure_structure()
        target = Path(path) if path else self.path_for(note)
        target.parent.mkdir(parents=True, exist_ok=True)
        content = frontmatter.dump(note.frontmatter(), note.body)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            tmp.unlink(missing_ok=True)  # never leave debris in the vault
            raise
        return target

    def save(self, note: Note) -> Path:
        """Update an existing note in place, following it if it has been moved
        or renamed by hand in Obsidian.

        A title change renames the file too. Links resolve by filename — the
        same rule Obsidian itself uses — so letting a title drift away from
        its filename would silently break every `[[link]]` pointing at it.
        This mirrors the existing "bucket change = file move" rule: the
        filename and the frontmatter are never allowed to disagree.

        Only a title change in *this* save triggers it. A file lj renamed by
        hand in Obsidian is left where it is, because fighting a deliberate
        rename would be worse than the drift.
        """
        hit = self.find(note.id)
        note.touch()
        if hit is None:
            return self.write(note)
        old_path, old_note = hit
        expected_dir = self.dir_for(note.bucket)
        renamed = old_note.title.strip() != note.title.strip()
        if old_path.parent != expected_dir or renamed:
            new_path = self.path_for(note)
            # Never move onto a file that already exists. Two notes sharing an
            # id is invalid input, but the failure mode without this check is
            # silent: one note's file overwrites another's and the second note
            # stops existing. Staying put is always recoverable.
            if new_path != old_path and not new_path.exists():
                self.write(note, old_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(new_path))
                return new_path
        return self.write(note, old_path)

    def repair_filenames(self) -> List[str]:
        """Rename managed files whose name no longer matches their title.

        For notes whose titles were edited before `save` learned to rename.
        Called from `reindex`, which already walks the whole vault, so it
        costs nothing extra there — and never on the capture path.

        Only touches files still in this system's `slug--id` shape. A file lj
        renamed by hand does not match that pattern and is left alone.
        """
        from .models import slugify

        fixed: List[str] = []
        for path, note in self.notes():
            stem = path.stem
            if "--" not in stem:
                continue  # hand-renamed; not ours to correct
            slug, _, suffix = stem.rpartition("--")
            if slug == slugify(note.title):
                continue
            target = path.with_name(f"{slugify(note.title)}--{suffix}.md")
            if target.exists() or target == path:
                continue
            shutil.move(str(path), str(target))
            fixed.append(f"{path.name} -> {target.name}")
        return fixed

    def move(self, note: Note, bucket: Bucket, event: str, detail: str = "") -> Path:
        """Bucket transition with an audit trail. Resource<->Archive uses this
        in both directions (§2), which is why nothing is ever deleted."""
        note.bucket = bucket
        note.log(event, detail or f"-> {bucket.value}")
        return self.save(note)

    # -- misc ---------------------------------------------------------------

    def counts(self) -> Dict[str, int]:
        counts = {b.value: 0 for b in Bucket}
        for _, n in self.notes():
            counts[n.bucket.value] += 1
        return counts

    def log_line(self, name: str, message: str) -> None:
        d = self.root / "_system" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with (d / f"{name}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
