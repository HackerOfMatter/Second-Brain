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

import copy
import datetime as dt
import os
import re
import uuid
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
        self._stamps: Optional[Tuple[int, Dict[str, List[Path]], List[Path]]] = None

    # -- cache control ------------------------------------------------------

    def invalidate(self) -> None:
        """Drop every cache. Only needed when something outside this class
        rearranges the vault; ordinary writes invalidate themselves."""
        self._listings.clear()
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
            if all(_mtime(d) == m for d, m in stamps.items()):
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
                for name in filenames:
                    if name.endswith(".md"):
                        paths.append(d / name)
            paths.sort()

        self._listings[bucket] = (stamps, paths)
        self._walks += 1
        self._title_index = None
        return paths

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
        return Note.from_frontmatter(_clone(meta), body)

    def iter_paths(self, bucket: Optional[Bucket] = None) -> Iterator[Path]:
        buckets = [bucket] if bucket else list(BUCKET_DIRS)
        for b in buckets:
            yield from self._listing(b)

    def notes(self, bucket: Optional[Bucket] = None) -> List[Tuple[Path, Note]]:
        """All managed notes. Files without our frontmatter are skipped, so an
        existing vault full of hand-written notes coexists with the system."""
        out: List[Tuple[Path, Note]] = []
        for p in self.iter_paths(bucket):
            try:
                out.append((p, self.read(p)))
            except (VaultError, UnicodeDecodeError, OSError):
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
        content = frontmatter.dump(note.frontmatter(), note.body)
        tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            atomic.replace(tmp, target)
        except Exception:
            tmp.unlink(missing_ok=True)  # never leave debris in the vault
            raise
        finally:
            # Both outcomes changed the directory: a stale listing would hide
            # a new note, and a stale parse would serve the pre-write body.
            self._forget(target)
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

    def counts(self) -> Dict[str, int]:
        counts = {b.value: 0 for b in Bucket}
        for bucket in BUCKET_DIRS:
            # Count per bucket directory so a note only has to be parsed to
            # confirm it is ours, not to discover where it lives.
            for path in self._listing(bucket):
                note = self._try_read(path)
                if note is not None:
                    counts[note.bucket.value] += 1
        return counts

    def log_line(self, name: str, message: str) -> None:
        d = self.root / "_system" / "logs"
        d.mkdir(parents=True, exist_ok=True)
        stamp = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        with (d / f"{name}.log").open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp}  {message}\n")
