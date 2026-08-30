"""Keep the search index current with what Obsidian just saved (sprint 4, K3).

The index used to be rebuilt on demand: `Engine.ask` builds it if it does not
exist, `reindex` runs when somebody presses the button. Between those two
moments a note lj edited in Obsidian was invisible to search — not stale, not
partially right, *absent*, because the passage the answer needed still held
last week's text. The Drop folder already solved the same shape of problem by
polling itself; this is that pattern pointed at the notes.

## Why polling, and why a stat sweep rather than a parse

Same reason as `sb/api.py:start_drop_watcher`: the vault sits in OneDrive,
where a synced file arrives as a rename of a temporary file and fires
filesystem events that do not correspond to a finished note. And unlike Drop,
this watcher must be cheap enough to run every few seconds forever, so it may
not read notes to find out whether they changed.

What it does instead is stat every indexable file — `30-Resources` and
`40-Archive`, the two buckets `Index.build` actually indexes — and hash
`(path, mtime_ns, size)`. That is one syscall per file and no `open()` at all.
The quantity is deliberately the same `(mtime_ns, size)` pair that keys the
parse cache in `sb/vault.py`: the two agree by construction, so the watcher
can never decide a note changed while the cache goes on serving the old parse,
and it can never miss an edit the cache would have noticed.

## Why it waits a tick before rebuilding

A save in progress is not a save. Obsidian, OneDrive and `git checkout` all
produce moments where a file exists at a size it will not keep, and reading a
note mid-write means either a YAML error — which `Vault.notes` skips silently,
dropping the note out of the index until the *next* time it is edited — or a
correct index of half a note. So a changed signature is not acted on until it
comes back identical on the following sweep. It costs one poll of latency and
removes a whole class of torn reads.

## Incremental, or it is not worth having

`Index.build` reuses the chunks and vectors of every note whose fingerprint is
unchanged, so a rebuild after one edit sends one note to the model. This
watcher does nothing to weaken that: it calls the same incremental build the
button calls, and its own job is only to decide *when*.

## When there is no model

An embedding-less build still writes the chunk file, so an edited note is
findable by keyword within the same latency — which is why this story could be
finished and measured with Ollama unreachable. But a search running on
keywords is a worse search, and a system that quietly runs degraded for a week
is the exact failure `sb/incidents.py` exists for. So a build that comes back
without embeddings files an incident naming what is missing, and the first
build that gets them back clears it.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import incidents as incidentsmod
from .models import Bucket
from .vault import BUCKET_DIRS

#: Seconds between sweeps. Not in `config.yaml` on purpose: it is not a dial
#: lj benefits from turning. The story's acceptance is "searchable within 60
#: seconds", the sweep is one stat per indexable file, and the settle rule
#: doubles the worst case — five seconds leaves an order of magnitude of
#: headroom on both, and anything faster only burns battery.
POLL_SECONDS = 5.0

#: The buckets `Index.build` indexes. Watching the others would rebuild the
#: index every time a project step was ticked off, for nothing.
WATCHED = (Bucket.RESOURCE, Bucket.ARCHIVE)

INCIDENT_KEY_EMBEDDINGS = "embeddings"
INCIDENT_KEY_WATCH = "freshness"


class FreshnessWatcher:
    """Notices that a note changed, and rebuilds the index when it settles.

    Split from the thread that runs it so the whole decision — has anything
    changed, has it stopped changing, should this rebuild — is a plain
    function a test can call four times in a row without sleeping.
    """

    def __init__(self, engine, poll_seconds: float = POLL_SECONDS):
        self.engine = engine
        self.poll_seconds = max(1.0, float(poll_seconds))
        #: The signature the index was last built from. None means "not yet
        #: established", which is why the first tick always rebuilds: the
        #: vault may well have been edited while the app was closed, and that
        #: rebuild is incremental and therefore nearly free when it was not.
        self.indexed: Optional[str] = None
        #: A change seen once, waiting to be seen again unchanged.
        self.pending: Optional[str] = None
        self.rebuilds = 0
        self.last: Dict[str, Any] = {}

    # -- what changed -------------------------------------------------------

    def _dirs(self) -> List[Path]:
        root = self.engine.vault.root
        return [root / BUCKET_DIRS[b] for b in WATCHED]

    def signature(self) -> str:
        """A hash of every indexable file's identity, size and mtime.

        One `stat` per file, no reads, no parsing. Sorted before hashing so
        the value depends on the vault rather than on directory order, and
        stable across machines.
        """
        rows: List[str] = []
        for base in self._dirs():
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames.sort()
                for name in sorted(filenames):
                    if not name.endswith(".md"):
                        continue
                    path = Path(dirpath) / name
                    try:
                        st = os.stat(path)
                    except OSError:
                        # A file that vanished between listing and stat is a
                        # change like any other; recording its absence is what
                        # makes the next sweep see it.
                        continue
                    rows.append(f"{path}|{st.st_mtime_ns}|{st.st_size}")
        digest = hashlib.blake2b(digest_size=16)
        digest.update("\n".join(rows).encode("utf-8", "replace"))
        return f"{len(rows)}:{digest.hexdigest()}"

    # -- one poll -----------------------------------------------------------

    def tick(self) -> Dict[str, Any]:
        """Sweep once. Rebuilds only when the vault has stopped moving.

        Returns what it decided, so the loop can log it and a test can assert
        it without a stopwatch.
        """
        sig = self.signature()
        if sig == self.indexed:
            self.pending = None
            return self.__record({"changed": False, "rebuilt": False})
        if self.indexed is not None and sig != self.pending:
            # Seen once. Give it one more sweep to prove it has landed.
            self.pending = sig
            return self.__record({"changed": True, "rebuilt": False, "settling": True})

        self.pending = None
        try:
            stats = self.engine.reindex()
        except Exception as exc:  # never take the thread down with it
            self.__log(f"freshness: rebuild failed: {exc!r}")
            self.__incident(
                INCIDENT_KEY_WATCH,
                f"The search index stopped following your edits ({type(exc).__name__}).",
                "Run `python run.py doctor`. `_system/index/` can be deleted "
                "safely — it rebuilds from the notes.",
                str(exc)[:400],
            )
            return self.__record({"changed": True, "rebuilt": False, "error": repr(exc)})

        # Only now: a signature recorded before a failed build would make the
        # next sweep believe the index was current.
        self.indexed = sig
        self.rebuilds += 1
        self.__clear(INCIDENT_KEY_WATCH)

        if stats.get("semantic"):
            self.__clear(INCIDENT_KEY_EMBEDDINGS)
        else:
            self.__incident(
                INCIDENT_KEY_EMBEDDINGS,
                "Your notes are being indexed, but search is matching words "
                "rather than meaning — no embedding model is reachable.",
                f"Start Ollama and run `ollama pull "
                f"{self.engine.cfg.llm.embed_model}`; the next edit reindexes "
                "and this clears itself.",
                str(stats.get("warning", ""))[:400],
                bump=False,
            )
        self.__log(
            f"freshness: rebuilt — {stats.get('embedded', 0)} embedded, "
            f"{stats.get('reused', 0)} reused, {stats.get('chunks', 0)} passages"
        )
        return self.__record(
            {
                "changed": True,
                "rebuilt": True,
                "embedded": stats.get("embedded", 0),
                "reused": stats.get("reused", 0),
                "chunks": stats.get("chunks", 0),
                "semantic": bool(stats.get("semantic")),
            }
        )

    # -- the thread ---------------------------------------------------------

    def run_forever(self) -> None:
        while True:
            time.sleep(self.poll_seconds)
            try:
                self.tick()
            except Exception as exc:  # a sweep that cannot stat is not fatal
                self.__log(f"freshness: sweep failed: {exc!r}")

    # -- plumbing -----------------------------------------------------------

    def __record(self, result: Dict[str, Any]) -> Dict[str, Any]:
        self.last = result
        return result

    def __log(self, line: str) -> None:
        try:
            self.engine.vault.log_line("index", line)
        except Exception:
            pass

    def __incident(self, key: str, message: str, hint: str, detail: str,
                   bump: bool = True) -> None:
        try:
            self.engine.incidents.record(
                incidentsmod.INDEX, message, hint=hint, key=key, detail=detail,
                bump=bump,
            )
        except Exception:
            pass

    def __clear(self, key: str) -> None:
        try:
            self.engine.incidents.clear(incidentsmod.INDEX, key)
        except Exception:
            pass


def start(engine, poll_seconds: float = POLL_SECONDS) -> Optional[FreshnessWatcher]:
    """Run one watcher per engine, on a daemon thread. Idempotent."""
    existing = getattr(engine, "_freshness", None)
    if existing is not None:
        return existing
    watcher = FreshnessWatcher(engine, poll_seconds)
    engine._freshness = watcher
    threading.Thread(
        target=watcher.run_forever, name="index-freshness", daemon=True
    ).start()
    return watcher
