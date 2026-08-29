"""Problems that happened while nobody was looking.

Everything in this system that can fail quietly fails on a background thread:
the Drop watcher polls on a timer, the calendar resyncs after every write, card
generation talks to a model that may not be running, the index rebuilds itself
on the first question. Each of those already writes a line to `_system/logs/`
when it goes wrong — and a log file nobody opens is the same as no report at
all. Sprint 2 found three separate `doctor` lines making claims their evidence
did not support; this is the other half of that problem. A failure lj cannot
see is a failure lj keeps paying for.

So: one small durable store of *currently open* problems, written by whichever
job noticed, read by `/api/health` and `/api/problems`, rendered as a banner.

## What is and is not an incident

An incident is a **standing condition**, not an event. "Ollama was unreachable
at 14:02" is a log line. "Card generation has had no model since 14:02" is an
incident, and it stops being one the moment a generation succeeds. That is why
`record()` is idempotent on `(kind, key)` — the tenth failed poll in a row does
not produce a tenth banner, it bumps `count` and leaves `since` alone, so the
banner can say how long this has been going on.

The corollary matters more: **whoever files an incident must clear it.** A
store that only accumulates would be a second log file with a worse format.
Every `record()` call site in this codebase has a matching `clear()` on its
success path, and `Engine.health()` reconciles the model-outage incident on
every call — reading is how a stale problem gets noticed, so reading is where
it gets retired.

## Shape on disk

`_system/logs/incidents.jsonl`, one JSON object per line, rewritten whole on
every change. That is not a performance mistake: incidents are rare (an empty
file is the normal state), the file is capped at a few dozen lines, and a
rewrite through `sb/atomic.py` is the only way to edit a record in place
without inventing a compaction pass. Resolved incidents are kept for a short
while so "it broke overnight and fixed itself" is answerable, then dropped.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List

from . import atomic

FILENAME = "incidents.jsonl"

#: How many resolved incidents to keep after they stop being problems. Enough
#: to answer "did something break overnight?"; not so many that the file grows
#: without bound on a machine where Ollama is started and stopped all day.
KEEP_RESOLVED = 40

#: Kinds used by this codebase. Not enforced — a caller may invent one — but
#: listed so the set is greppable and the banner copy has something to key on.
OLLAMA = "ollama"
CALENDAR = "calendar"
DROP = "drop"
INDEX = "index"
CARD_QUEUE = "card_queue"


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _ident(kind: str, key: str = "") -> str:
    return f"{kind}/{key}" if key else kind


class IncidentStore:
    """The open-problems file, with a lock around read-modify-write.

    The lock is per-store and therefore per-Engine, which is the right scope:
    the Drop watcher thread and a request thread share one `Engine`, and they
    are exactly the two writers that can collide. A second process (the CLI
    running `doctor` while the server is up) can still interleave, and the
    worst case there is one lost `count` bump — deliberately not worth a
    lockfile, because the *condition* is still recorded either way.
    """

    def __init__(self, log_dir: Path):
        self.dir = Path(log_dir)
        self.path = self.dir / FILENAME
        self._lock = threading.Lock()

    # -- read ---------------------------------------------------------------

    def _read(self) -> List[Dict[str, Any]]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return []
        out: List[Dict[str, Any]] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn write is one lost incident, not a crash
            if isinstance(item, dict) and item.get("kind"):
                out.append(item)
        return out

    def all(self) -> List[Dict[str, Any]]:
        """Every record still on disk, open and resolved, newest problem first."""
        return sorted(self._read(), key=lambda i: i.get("since") or "", reverse=True)

    def open(self) -> List[Dict[str, Any]]:
        """The problems that are still true right now.

        This is what the banner renders and what `/api/problems` returns.
        Oldest first: a thing that has been broken since Tuesday deserves to
        be read before a thing that broke a minute ago.
        """
        return sorted(
            (i for i in self._read() if not i.get("resolved_at")),
            key=lambda i: i.get("since") or "",
        )

    def count(self) -> int:
        return len(self.open())

    # -- write --------------------------------------------------------------

    def _write(self, items: List[Dict[str, Any]]) -> None:
        """Rewrite the file. Resolved records past `KEEP_RESOLVED` are dropped.

        Atomic because the reader is `/api/health`, which is polled: a partial
        file read mid-write would blank the banner for one tick and put it back
        on the next, which looks exactly like a bug in the thing the banner is
        reporting.
        """
        live = [i for i in items if not i.get("resolved_at")]
        done = sorted(
            (i for i in items if i.get("resolved_at")),
            key=lambda i: i.get("resolved_at") or "",
            reverse=True,
        )[:KEEP_RESOLVED]
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{FILENAME}.{uuid.uuid4().hex[:8]}.tmp"
        body = "".join(
            json.dumps(i, ensure_ascii=False, default=str) + "\n" for i in live + done
        )
        tmp.write_text(body, encoding="utf-8")
        try:
            atomic.replace(tmp, self.path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    def record(
        self,
        kind: str,
        message: str,
        *,
        hint: str = "",
        key: str = "",
        detail: str = "",
        bump: bool = True,
    ) -> Dict[str, Any]:
        """File a problem, or bump the one already open for `(kind, key)`.

        `since` is the first time this condition was seen and never moves
        while it stays broken — that is the whole point of the store, and it
        is what lets the banner say "since 02:14" rather than "just now" on a
        failure that has been repeating every twenty seconds since 2am.

        `bump=False` is for the callers that merely *observe* a standing
        condition rather than trip over it — `Engine.health()` noticing that
        the Google token is stale, on an endpoint the dashboard polls every
        two minutes. Re-reporting an unchanged condition then costs no disk
        write at all, so a read path stays a read path.
        """
        ident = _ident(kind, key)
        stamp = _now()
        with self._lock:
            items = self._read()
            for item in items:
                if item.get("id") == ident and not item.get("resolved_at"):
                    if not bump and item.get("message") == message:
                        return dict(item)
                    item["message"] = message
                    item["hint"] = hint or item.get("hint", "")
                    item["detail"] = detail or item.get("detail", "")
                    item["last_seen"] = stamp
                    item["count"] = int(item.get("count") or 1) + 1
                    self._write(items)
                    return dict(item)
            fresh = {
                "id": ident,
                "kind": kind,
                "key": key,
                "message": message,
                "hint": hint,
                "detail": detail,
                "since": stamp,
                "last_seen": stamp,
                "count": 1,
                "resolved_at": None,
            }
            items.append(fresh)
            self._write(items)
            return dict(fresh)

    def clear(self, kind: str, key: str = "") -> bool:
        """Retire a problem. Returns whether anything was actually open.

        The return value is load-bearing for callers on hot paths: a success
        that had nothing to clear must not cost a disk write, so `False` means
        "nothing happened, and no file was touched".
        """
        ident = _ident(kind, key)
        with self._lock:
            items = self._read()
            hit = False
            for item in items:
                if item.get("id") == ident and not item.get("resolved_at"):
                    item["resolved_at"] = _now()
                    hit = True
            if hit:
                self._write(items)
            return hit

    def clear_all(self) -> int:
        with self._lock:
            items = self._read()
            n = 0
            for item in items:
                if not item.get("resolved_at"):
                    item["resolved_at"] = _now()
                    n += 1
            if n:
                self._write(items)
            return n
