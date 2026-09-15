"""Obsidian vault I/O.

The vault is the single source of truth (blueprint §6). Nothing in this system
keeps a second authoritative copy of a note: the dashboard, the calendar and
the RAG index are all derived views that can be rebuilt by re-reading these
files. That is what makes the vault safe to edit by hand in Obsidian.

Folder layout, numbered so Obsidian's file explorer sorts them in PARA order:

    00-Inbox/      unclassified captures (should stay near-empty)
    10-Areas/      ongoing responsibilities; habits live here
    20-Projects/   deadline-bound work
    30-Resources/  reference material; the default RAG corpus
    40-Archive/    retired material; searched only on request
    _system/       machine state: calendar/, index/, logs/  (underscore-prefixed
                   so it sorts out of the way; add to Obsidian's excluded files)
    _templates/    Obsidian templates matching the schema in models.py

Caching (see `_listing` and `_parsed`)
-------------------------------------
Every read path here used to be a fresh filesystem walk plus a full YAML parse
of every note. One dashboard render calls `notes()` around a dozen times, so a
700-note vault meant ~8,000 file reads and YAML parses per page load. Two
caches remove that without weakening the "vault is the truth" rule, because
both are validated against the filesystem rather than trusted blindly:

  * the path listing is keyed on the mtime of every directory it walked, so an
    added, deleted or renamed file invalidates it the moment it happens —
    including edits made in Obsidian, outside this process;
  * the parsed frontmatter is keyed on (mtime_ns, size) of the file itself, so
    a note edited anywhere is re-read on its next access.

Neither cache ever hands out a shared `Note`: callers mutate notes in place
(`note.bucket = ...`, `note.log(...)`), so `read()` always returns a freshly
constructed object built from a deep copy of the cached frontmatter.
"""

from __future__ import annotations

import bisect
import copy
import datetime as dt
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from pydantic import ValidationError

from . import atomic, frontmatter
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


#: "this file is not a note we can read" — every way that can be true.
#:
#: `VaultError` is ours (no frontmatter, no id). `UnicodeDecodeError` is a
#: file that is not text. `pydantic.ValidationError` is frontmatter that is
#: text and is YAML and still is not a note — `bucket: nonsense`, typed by
#: hand in Obsidian. That last one used to escape: a single mistyped bucket
#: anywhere in the vault turned `/api/health` into a 500 and took the whole
#: dashboard footer with it, which is the opposite of the documented rule
#: that a file we cannot read is skipped rather than fatal.
UNREADABLE = (VaultError, ValidationError, UnicodeDecodeError, OSError)


#: Frontmatter is YAML, so its values are drawn from a closed set of types.
#: Everything here is immutable and can be shared between copies; only dicts
#: and lists have to be rebuilt.
_ATOMIC = (str, bytes, int, float, bool, dt.date, dt.time, dt.timedelta, type(None))


def _clone(value: Any) -> Any:
    """A copy of one frontmatter value, cheap enough to run per read.

    `copy.deepcopy` costs ~30 recursive calls plus a memo dict per note, which
    made it the single largest line item once the YAML parse was cached. This
    knows what YAML can produce and copies only what is actually mutable,
    falling back to `deepcopy` for anything unexpected.
    """
    if isinstance(value, _ATOMIC):
        return value
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_clone(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_clone(v) for v in value)
    return copy.deepcopy(value)


#: Top-level frontmatter keys that `Note` validates into objects of its own.
#: Pydantic rebuilds every list, dict and sub-model for these, so the built
#: note shares nothing mutable with the cached dict and cloning them first is
#: pure waste — it was the largest line item of a vault read. Everything else
#: (extra keys a human added, and `srs`/`habit`, whose `Dict[str, Any]`
#: entries pydantic stores by reference) is still cloned.
_REBUILT_KEYS = frozenset({
    "id", "title", "bucket", "created", "updated", "tags", "source",
    "history", "category", "project", "schedule", "review", "intake",
})


def _build(meta: Dict[str, Any], body: str) -> Note:
    """A fresh `Note` from a cached frontmatter dict (which is never mutated)."""
    return Note.from_frontmatter(
        {k: (v if k in _REBUILT_KEYS else _clone(v)) for k, v in meta.items()},
        body,
    )


#: A directory whose mtime is this close to the moment we recorded it may
#: still change without its mtime moving: file timestamps come from a coarse
#: clock (~15 ms on NTFS, a jiffy on Linux), so a second change in the same
#: tick looks like no change. Such "racy" directories are re-listed — one
#: `scandir`, not a walk — until they have been quiet this long.
RACY_NS = 1_000_000_000


def _mtime(path: Path) -> Optional[int]:
    """Directory mtime, or None if it does not exist. A missing directory is a
    real state, not an error: creating 40-Archive later must invalidate the
    listing that was taken while it was absent."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return None


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser()
        # bucket -> (dir mtimes at walk time, sorted paths)
        self._listings: Dict[Bucket, Tuple[Dict[Path, Optional[int]], List[Path]]] = {}
        # path -> (mtime_ns, size, frontmatter dict, body)
        self._parsed: Dict[Path, Tuple[int, int, Dict[str, Any], str]] = {}
        # bumped on every rewalk; the derived indexes piggyback on it
        self._walks = 0
        self._title_index: Optional[Tuple[int, Dict[str, Path]]] = None
        # dir -> wall-clock ns when its mtime was recorded (see RACY_NS)
        self._stamp_at: Dict[Path, int] = {}
        # dir -> (names of the .md files in it, names of its subdirectories)
        self._children: Dict[Path, Tuple[set, set]] = {}
        self._stamps: Optional[Tuple[int, Dict[str, List[Path]], List[Path]]] = None

    # -- cache control ------------------------------------------------------

    def invalidate(self) -> None:
        """Drop every cache. Only needed when something outside this class
        rearranges the vault; ordinary writes invalidate themselves."""
        self._listings.clear()
        self._stamp_at.clear()
        self._children.clear()
        self._parsed.clear()
        self._title_index = None
        self._stamps = None
        self._walks += 1

    def _forget(self, *paths: Path) -> None:
        """Forget the listing (a file appeared, moved or vanished) and the
        parsed copy of the named files."""
        self._listings.clear()
        self._title_index = None
        self._walks += 1
        for p in paths:
            self._parsed.pop(Path(p), None)

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

    def _listing(self, bucket: Bucket) -> List[Path]:
        """Sorted `.md` paths in one bucket, cached until the tree changes.

        Validation stats only the directories, not the files. Adding, deleting
        or renaming an entry bumps the mtime of the directory holding it, and a
        new subdirectory bumps its parent's — so stat'ing every directory the
        previous walk saw catches every structural change at any depth, for a
        handful of syscalls instead of a full recursive walk.
        """
        cached = self._listings.get(bucket)
        if cached is not None:
            stamps, paths = cached
            if all(_mtime(d) == m for d, m in stamps.items()) and self._settled(stamps):
                return paths

        root = self.dir_for(bucket)
        stamps = {root: _mtime(root)}
        paths: List[Path] = []
        if stamps[root] is not None:
            # One walk collects both the files and the directories to watch;
            # rglob("*.md") alone would not tell us which directories exist.
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                stamps[d] = _mtime(d)
                self._stamp_at[d] = time.time_ns()
                md = {name for name in filenames if name.endswith(".md")}
                self._children[d] = (md, set(dirnames))
                paths.extend(d / name for name in md)
            paths.sort()

        self._listings[bucket] = (stamps, paths)
        self._walks += 1
        self._title_index = None
        return paths

    def _settled(self, stamps: Dict[Path, Optional[int]]) -> bool:
        """False if a directory whose mtime cannot be trusted yet has in fact
        changed. Re-lists only those directories."""
        now = time.time_ns()
        for d, m in stamps.items():
            if m is None or m + RACY_NS <= self._stamp_at.get(d, 0):
                continue
            known = self._children.get(d)
            if known is None:
                return False
            try:
                with os.scandir(d) as it:
                    files, subdirs = set(), set()
                    for entry in it:
                        if entry.is_dir():
                            subdirs.add(entry.name)
                        elif entry.name.endswith(".md"):
                            files.add(entry.name)
            except OSError:
                return False
            if files != known[0] or subdirs != known[1]:
                return False
            if now - m >= RACY_NS:
                self._stamp_at[d] = now   # quiet long enough: trust it again
        return True

    def _revalidate(self) -> int:
        """Re-check every bucket listing and return the walk generation.

        The derived indexes below need to know "has anything changed?" without
        paying to materialise every path just to find out it hasn't.
        """
        for bucket in BUCKET_DIRS:
            self._listing(bucket)
        return self._walks

    def _meta(self, path: Path) -> Tuple[Dict[str, Any], str]:
        """Cached (frontmatter, body) for one file. Never mutate the result —
        it is the shared cache entry. `read` is the public, copying door."""
        if type(path) is not type(self.root):
            path = Path(path)
        try:
            st = os.stat(path)
            key: Optional[Tuple[int, int]] = (st.st_mtime_ns, st.st_size)
        except OSError:
            key = None

        hit = self._parsed.get(path)
        if key is not None and hit is not None and (hit[0], hit[1]) == key:
            return hit[2], hit[3]

        raw = path.read_text(encoding="utf-8")
        meta, body = frontmatter.parse(raw)
        if key is not None:
            self._parsed[path] = (key[0], key[1], meta, body)
        return meta, body

    def _id_at(self, path: Path) -> Optional[str]:
        """The id in a file's frontmatter, without building a `Note`.

        `find` may have to check every file sharing an id stamp — a real case,
        because the stamp is second-resolution and one intake run files a whole
        Drop folder inside the same second. Comparing ids should not cost a
        pydantic model and a deep copy per candidate.
        """
        try:
            meta, _ = self._meta(path)
        except (UnicodeDecodeError, OSError, ValueError):
            return None
        got = meta.get("id")
        return got if isinstance(got, str) else None

    def read(self, path: Path) -> Note:
        """Parse one note. Returns a fresh `Note` every call — callers mutate
        what they get back, so a shared instance would leak edits between
        unrelated requests."""
        meta, body = self._meta(path)
        if not meta.get("id"):
            raise VaultError(f"{path} has no Second Brain frontmatter (missing id)")
        return _build(meta, body)

    def iter_paths(self, bucket: Optional[Bucket] = None) -> Iterator[Path]:
        buckets = [bucket] if bucket else list(BUCKET_DIRS)
        for b in buckets:
            yield from self._listing(b)

    def notes(self, bucket: Optional[Bucket] = None) -> List[Tuple[Path, Note]]:
        """All managed notes. Files without our frontmatter are skipped, so an
        existing vault full of hand-written notes coexists with the system."""
        out: List[Tuple[Path, Note]] = []
        for p in self.iter_paths(bucket):
            # Hand-written files carry no id; skip them without paying for an
            # exception each (a vault of loose notes is thousands per page).
            try:
                meta, body = self._meta(p)
                if not isinstance(meta.get("id"), str) or not meta["id"]:
                    continue
                out.append((p, _build(meta, body)))
            except UNREADABLE:
                continue
        return out

    def find(self, note_id: str) -> Optional[Tuple[Path, Note]]:
        """Locate a note by id, reading one file where possible.

        This is the hottest path in the system — `note()`, `save()` and every
        mutation go through it — and it used to parse every file in the vault
        to check an id that `path_for` had already written into the filename
        (`{slug}--{id[:15]}.md`). Matching the filename first turns "open one
        note" into a suffix match over a cached listing plus one read.

        The full scan survives as a fallback for files renamed by hand in
        Obsidian, which no longer carry the id. Correctness first: the id in
        the frontmatter is still what decides, the filename only proposes.
        """
        if not note_id:
            return None
        stamp = note_id[:15]
        by_stamp, unstamped = self._stamp_index()

        for path in by_stamp.get(stamp, ()):
            if self._id_at(path) == note_id:
                note = self._try_read(path)
                if note is not None:
                    return path, note

        # Nothing matched by name. Rather than parsing the whole vault, read
        # only the files that *could* have been renamed by hand — the ones
        # carrying no id stamp at all. A file stamped with somebody else's id
        # is somebody else's note, and opening it proves nothing.
        for path in unstamped:
            if self._id_at(path) == note_id:
                note = self._try_read(path)
                if note is not None:
                    return path, note

        # Last resort: a stamped file whose stamp disagrees with its own
        # frontmatter. Shouldn't happen, but the id in the file is the
        # authority and a note that exists must always be findable. Skip the
        # files the two passes above already proved wrong.
        checked = set(unstamped)
        checked.update(by_stamp.get(stamp, ()))
        for path in self.iter_paths():
            if path in checked or self._id_at(path) != note_id:
                continue
            note = self._try_read(path)
            if note is not None:
                return path, note
        return None

    def _stamp_index(self) -> Tuple[Dict[str, List[Path]], List[Path]]:
        """(id stamp -> files carrying it, files carrying none).

        `find` is on every mutation path, and scanning the whole listing for a
        filename suffix made "save one note" linear in the size of the vault.
        Grouping by the stamp once per walk makes the common case a dict hit.
        Values are lists, and in listing order, so a duplicated stamp is still
        resolved by reading the frontmatter — the same answer the scan gave.
        """
        gen = self._revalidate()
        if self._stamps is not None and self._stamps[0] == gen:
            return self._stamps[1], self._stamps[2]

        by_stamp: Dict[str, List[Path]] = {}
        unstamped: List[Path] = []
        for path in self.iter_paths():
            stem = path.stem
            cut = stem.rfind("--")
            if cut == -1 or not _STAMPED.search(stem):
                unstamped.append(path)
            else:
                by_stamp.setdefault(stem[cut + 2:], []).append(path)
        self._stamps = (gen, by_stamp, unstamped)
        return by_stamp, unstamped

    def folders_by_id(self) -> Dict[str, str]:
        """note id -> the folder it lives in, relative to the vault root.

        Vault-relative and POSIX-separated ("30-Resources/Statics"), so it
        reads the same on Windows as it does in a config file or a URL. A note
        sitting directly in a bucket gets the bucket ("20-Projects").

        Derived from the filesystem on every call rather than stored on the
        deck, so moving a note in Obsidian moves its cards with it. It costs a
        stat per note and nothing else: `_id_at` reads the cached frontmatter
        and never builds a `Note`.
        """
        out: Dict[str, str] = {}
        for path in self.iter_paths():
            note_id = self._id_at(path)
            if not note_id:
                continue
            try:
                rel = path.parent.relative_to(self.root)
            except ValueError:
                continue
            folder = rel.as_posix()
            out[note_id] = "" if folder == "." else folder
        return out

    def _try_read(self, path: Path) -> Optional[Note]:
        try:
            return self.read(path)
        except UNREADABLE:
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
    # already in the filename. Listing paths reads nothing, and only the notes
    # actually linked are ever opened.

    def title_index(self) -> Dict[str, Path]:
        """Map slugified title → path, from filenames alone. No file reads.

        Two keys per file, because a vault is edited by a human as well as by
        this app: the slug this system would have generated, and the slug of
        whatever the file happens to be called now. Renaming a note in
        Obsidian renames its file, and its links should still resolve.

        Cached against the listing generation, because `resolve_title` is
        called once per `[[link]]` and rebuilding this per link made link
        rewriting quadratic in the size of the vault.
        """
        from .models import slugify

        gen = self._revalidate()
        if self._title_index is not None and self._title_index[0] == gen:
            return self._title_index[1]

        index: Dict[str, Path] = {}
        for path in self.iter_paths():
            stem = path.stem
            managed = stem.rsplit("--", 1)[0] if "--" in stem else stem
            for key in (managed, slugify(stem)):
                if key:
                    index.setdefault(key, path)
        self._title_index = (gen, index)
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
        except UNREADABLE:
            return None

    # -- write --------------------------------------------------------------

    def path_for(self, note: Note) -> Path:
        from .models import slugify

        return self.dir_for(note.bucket) / f"{slugify(note.title)}--{note.id[:15]}.md"

    def path_in(self, note: Note, folder: str) -> Path:
        """Where this note goes inside a subfolder of its own bucket.

        Subfolders under a bucket are lj's, not ours — a note in
        `30-Resources/Federal Taxation/Ch 4` is still a Resource, and phase 12
        already reads them as study scopes. This is the write-side counterpart:
        a capture that knows which folder it belongs to (a note-taking session
        knows exactly that) can say so without anyone reaching for `path_for`
        and gluing paths together at the call site.

        The folder is taken apart and put back a segment at a time, so
        `..` cannot climb out of the bucket and a leading slash cannot
        reach the drive root. An empty folder means the bucket itself.
        """
        base = self.dir_for(note.bucket)
        for part in str(folder or "").replace("\\", "/").split("/"):
            part = part.strip()
            if not part or part in (".", ".."):
                continue
            base = base / part
        return base / self.path_for(note).name

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
        the replace is atomic, so the loser is simply overwritten. The
        invariant that matters is that the note on disk is always one complete
        version of itself, never a truncated blend and never absent.

        The replace goes through `sb/atomic.py` because on Windows it can fail
        outright while another handle — the other writer, a virus scanner,
        Obsidian — holds the destination for a few milliseconds. See that
        module for why that is a busy signal rather than an error.
        """
        target = Path(path) if path else self.path_for(note)
        # `ensure_structure` used to run on every write, which is six mkdir
        # syscalls per save on a vault that already exists. The one directory
        # that has to be there is the target's, and that is created below.
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self._free_name(target, note.id)
        content = frontmatter.dump(note.frontmatter(), note.body)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        before = _mtime(target.parent)
        try:
            tmp.write_text(content, encoding="utf-8")
            atomic.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)  # never leave debris in the vault
            self._forget(target)
            raise
        self._wrote(target, before)
        return target

    def _wrote(self, target: Path, before: Optional[int]) -> None:
        """Bring the caches up to date after this process wrote `target`.

        The temp-file-and-replace touches the directory, so the listing's
        mtime check would otherwise throw the whole walk away on every save —
        and the next `find` would re-walk the vault. When the directory's
        mtime *before* the write is the one the listing already recorded,
        nothing else changed in it, so the listing is patched in place: the
        new mtime recorded, the path inserted if it is new. Anything else (a
        new subfolder, an external edit the cache has not seen) falls back to
        forgetting the listing, which is always correct.
        """
        self._parsed.pop(target, None)
        parent = target.parent
        for stamps, paths in self._listings.values():
            if parent not in stamps:
                continue
            if before is None or stamps[parent] != before:
                break
            stamps[parent] = _mtime(parent)
            self._stamp_at[parent] = time.time_ns()
            i = bisect.bisect_left(paths, target)
            if i == len(paths) or paths[i] != target:
                paths.insert(i, target)
                known = self._children.get(parent)
                if known is not None:
                    known[0].add(target.name)
                self._walks += 1          # the set of notes changed
                self._title_index = None
            return
        self._forget(target)

    def _free_name(self, target: Path, note_id: str) -> Path:
        """`target`, or the next free name beside it if another note is there.

        A write never lands on top of a *different* note. It sounds like it
        could not happen, and it can: `new_id` is `{second}-{slug}`, and
        `path_for` names the file from the same two things, so two notes
        captured in the same second with the same title are the same id and
        the same path — and the second write destroyed the first, silently,
        with no error anywhere. Two terms typed quickly into a lecture is
        exactly the shape of capture that does it.

        Rewriting the *same* note is untouched — that is every save in the
        system, and it must stay a plain overwrite. This only diverts a write
        whose destination is somebody else.
        """
        if not target.exists() or self._id_at(target) in (None, note_id):
            return target
        n = 2
        while True:
            candidate = target.with_name(f"{target.stem} ({n}){target.suffix}")
            if not candidate.exists() or self._id_at(candidate) == note_id:
                self.log_line(
                    "vault",
                    f"name taken by another note, wrote {candidate.name} instead "
                    f"of {target.name}",
                )
                return candidate
            n += 1

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
        # A note filed into a sub-topic — 30-Resources/Accounting/Study Terms —
        # is still in its bucket, and saving it must not drag it up to the
        # bucket root. It used to: the test was `old_path.parent !=
        # expected_dir`, which is true of every subfolder, so the first save
        # after adoption flattened the whole subject tree and took the
        # study-by-folder grouping (`folders_by_id`) with it. Only a genuine
        # bucket change relocates a note now; a rename keeps it where it lives.
        in_bucket = expected_dir in old_path.parents
        if not in_bucket or renamed:
            new_path = (
                old_path.with_name(self.path_for(note).name)
                if in_bucket
                else self.path_for(note)
            )
            # Never move onto a file that already exists. Two notes sharing an
            # id is invalid input, but the failure mode without this check is
            # silent: one note's file overwrites another's and the second note
            # stops existing. Staying put is always recoverable.
            if new_path != old_path and not new_path.exists():
                self.write(note, old_path)
                new_path.parent.mkdir(parents=True, exist_ok=True)
                atomic.move(old_path, new_path)
                self._forget(old_path, new_path)
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
        # Snapshot first: each rename invalidates the listing, and iterating a
        # listing that is being rebuilt underneath skips files.
        for path, note in list(self.notes()):
            stem = path.stem
            if "--" not in stem:
                continue  # hand-renamed; not ours to correct
            slug, _, suffix = stem.rpartition("--")
            if slug == slugify(note.title):
                continue
            target = path.with_name(f"{slugify(note.title)}--{suffix}.md")
            if target.exists() or target == path:
                continue
            atomic.move(path, target)
            self._forget(path, target)
            fixed.append(f"{path.name} -> {target.name}")
        return fixed

    def move(self, note: Note, bucket: Bucket, event: str, detail: str = "") -> Path:
        """Bucket transition with an audit trail. Resource<->Archive uses this
        in both directions (§2), which is why nothing is ever deleted."""
        note.bucket = bucket
        note.log(event, detail or f"-> {bucket.value}")
        return self.save(note)

    # -- misc ---------------------------------------------------------------

    def _bucket_at(self, path: Path) -> Optional[str]:
        """The bucket in a file's frontmatter, without building a `Note`.

        The same argument as `_id_at`, and it matters more here because this
        one runs over *every* file: `counts()` used to construct a validated
        pydantic model and deep-copy its whole frontmatter for each note in
        the vault, in order to read a single enum off it. On a 600-note vault
        that was the entire cost of `/api/health` — which the dashboard calls
        on every load, so it was also most of the cost of opening the
        dashboard.

        The answer must match what `Note` would have said, or the footer and
        the dashboard disagree about how many notes exist: an unparseable or
        id-less file is not ours and counts nowhere, a missing or unknown
        bucket falls back to the model's own default rather than to the
        directory the file happens to sit in.
        """
        try:
            meta, _ = self._meta(path)
        except (UnicodeDecodeError, OSError, ValueError):
            return None
        if not isinstance(meta.get("id"), str) or not meta["id"]:
            return None
        raw = meta.get("bucket")
        if isinstance(raw, Bucket):
            return raw.value
        if raw is None:
            # `Note` supplies its own default for a missing bucket. The file
            # is still ours and still counts.
            return Note.model_fields["bucket"].default.value
        try:
            return Bucket(str(raw)).value
        except ValueError:
            # A bucket `Note` would refuse. Skipped here for exactly the same
            # reason `_try_read` skips it — see UNREADABLE.
            return None

    def relocate(self, path: Path, target: Path) -> Path:
        """Move a note's file, keeping the note itself untouched.

        A folder is not a property of a note — the bucket is, and that is in
        the frontmatter. So filing a note into `30-Resources/Federal Taxation`
        is a file move and nothing else: no rewrite, no `updated` bump, no
        history entry, and no chance of the round trip through YAML changing
        something on the way past. The note that comes back out is byte for
        byte the note that went in.

        `atomic.move` rather than `Path.replace` because on Windows the
        destination can be held for a moment by Obsidian or a scanner — see
        that module.
        """
        path, target = Path(path), Path(target)
        if path == target:
            return target
        target.parent.mkdir(parents=True, exist_ok=True)
        target = self._free_name(target, self._id_at(path) or "")
        atomic.move(path, target)
        self._forget(path, target)
        return target

    RETIRED_DIR = "_retired"

    def retire(self, path: Path, reason: str = "") -> Path:
        """Move a note out of the way. Nothing in this system deletes one.

        The rule is lj's and it is the right one: a note that was worth
        writing down is never worth destroying on a machine's judgement, and
        every automated "clean up" that has ever deleted someone's work
        believed at the time that it was removing something worthless.
        Undoing a bad classification costs a drag in Obsidian. Undoing a
        deletion costs the note.

        So the one operation is this: the file moves to
        `40-Archive/_retired/<YYYY-MM>/`, under a name that cannot collide,
        and the reason goes to the log. Obsidian's search still finds it,
        `40-Archive` is already the bucket the system searches only on
        request, and the note is one drag from being back.

        Deleting is left to lj, in Explorer, deliberately.
        """
        path = Path(path)
        target_dir = (self.dir_for(Bucket.ARCHIVE) / self.RETIRED_DIR
                      / dt.date.today().strftime("%Y-%m"))
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / path.name
        n = 2
        while target.exists():
            target = target_dir / f"{path.stem} ({n}){path.suffix}"
            n += 1
        atomic.move(path, target)
        self._forget(path, target)
        self.log_line("retire", f"{path.name}  ->  {target}  {reason}".rstrip())
        return target

    def counts(self) -> Dict[str, int]:
        counts = {b.value: 0 for b in Bucket}
        for bucket in BUCKET_DIRS:
            # Count per bucket directory so a note only has to be parsed to
            # confirm it is ours, not to discover where it lives.
            for path in self._listing(bucket):
                got = self._bucket_at(path)
                if got is not None:
                    counts[got] += 1
        return counts

    def log_line(self, name: str, message: str) -> None:
        d = self.root / "_system" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with (d / f"{name}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
