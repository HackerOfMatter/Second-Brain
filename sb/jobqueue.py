"""Work that could not be done yet, kept until it can.

Exactly one job in this system is worth deferring rather than degrading, and
it is card generation. Everything else has an honest offline answer: a capture
falls back to the rule-based parser and still lands as a structured Project, a
question returns the passages instead of prose, a typed answer is marked by
word overlap, the ambiguous half of a link set is dropped rather than guessed.
Those are all *smaller* versions of the same result, produced now, and asking
lj to come back later for them would be worse.

Cards are different, for the reason at the top of `sb/generate.py`: a card is
permanent, and spaced repetition will drill whatever it says into you on an
optimal schedule. The offline generator makes cloze deletions out of definition
sentences, which is fine as a stopgap and is not what you want twenty of. So
when the model is down, the *request* is what gets kept — note id, how many
cards, any pasted source — and it is replayed against the real model the next
time one is reachable.

## Why on disk

Because the outage outlives the process. Ollama not running at 2am and the
dashboard being closed at 2am are the same event; an in-memory queue would
lose the request precisely in the case it exists for. `_system/queue/` is
disposable state — losing it costs a re-click, not data — which is why it
lives there and not beside the decks.

## Identity

One pending entry per note. Pressing Generate four times while the model is
down is one queued job, not four, and the newest request wins (it carries the
newest `max_cards` and the newest pasted source). Draining removes the entry
before running it, so a job that fails for a *different* reason — a note that
has since been deleted — cannot wedge the queue in a retry loop.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import atomic

QUEUE_DIRNAME = "queue"
CARDS_FILE = "cards.jsonl"

#: A pasted source is stored so the replay reproduces the request exactly.
#: Capped because this is a queue file, not a document store — beyond this the
#: note body is used instead, which is what the UI sends in the normal case.
MAX_SOURCE_CHARS = 20000


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="seconds")


class CardQueue:
    """Pending card-generation requests, one per note.

    Deliberately not generic. A queue that can hold any job needs a registry of
    handlers, a serialisation contract and a poison-message policy; this holds
    one shape of request, and `Engine.drain_card_queue` is its only consumer.
    """

    def __init__(self, system_dir: Path):
        self.dir = Path(system_dir) / QUEUE_DIRNAME
        self.path = self.dir / CARDS_FILE
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
                continue
            if isinstance(item, dict) and item.get("note_id"):
                out.append(item)
        return out

    def pending(self) -> List[Dict[str, Any]]:
        """Oldest request first — the queue drains in the order lj asked."""
        return sorted(self._read(), key=lambda i: i.get("queued_at") or "")

    def count(self) -> int:
        return len(self._read())

    def has(self, note_id: str) -> bool:
        return any(i.get("note_id") == note_id for i in self._read())

    # -- write --------------------------------------------------------------

    def _write(self, items: List[Dict[str, Any]]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f".{CARDS_FILE}.{uuid.uuid4().hex[:8]}.tmp"
        body = "".join(
            json.dumps(i, ensure_ascii=False, default=str) + "\n" for i in items
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

    def add(
        self,
        note_id: str,
        *,
        title: str = "",
        max_cards: Optional[int] = None,
        source: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Queue (or re-queue) generation for one note. Newest request wins.

        `queued_at` is preserved across a re-queue for the same reason
        `incidents.since` is: the honest answer to "how long has this been
        waiting?" is measured from the first ask, not the last.
        """
        with self._lock:
            items = self._read()
            existing = next((i for i in items if i.get("note_id") == note_id), None)
            entry = {
                "note_id": note_id,
                "title": title or (existing or {}).get("title", ""),
                "max_cards": max_cards,
                "source": (source or "")[:MAX_SOURCE_CHARS],
                "reason": reason,
                "queued_at": (existing or {}).get("queued_at") or _now(),
                "requested_at": _now(),
                "attempts": int((existing or {}).get("attempts") or 0),
            }
            items = [i for i in items if i.get("note_id") != note_id]
            items.append(entry)
            self._write(items)
            return dict(entry)

    def take(self, note_id: str) -> Optional[Dict[str, Any]]:
        """Remove and return one entry. Draining takes before it runs, so a
        request that fails for a new reason does not wedge the queue."""
        with self._lock:
            items = self._read()
            hit = next((i for i in items if i.get("note_id") == note_id), None)
            if hit is None:
                return None
            self._write([i for i in items if i.get("note_id") != note_id])
            return dict(hit)

    def remove(self, note_id: str) -> bool:
        return self.take(note_id) is not None
