"""Google Tasks sink — where Project due dates live.

A deadline is a task, so it goes to the task system: Google Tasks, which
renders in the Tasks strip of Google Calendar, on the day it is due, and stays
there until it is ticked. That is the behaviour an all-day event never had.

Sync is stateless, like the event sinks. Google assigns task ids, so instead of
keeping a mapping file the sink writes its own uid into the task's notes as a
trailing `[sb:…]` marker and finds its work by reading it back. Nothing outside
the vault is a source of truth, and a task lj created by hand has no marker, so
the sink never touches it.

Colour: the Tasks API has none — every task renders in one colour, and there is
no field to change it. The category emoji in the title is the whole colour
channel here, which is exactly why taxonomy.decorate() exists.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

from ..config import Config
from ..models import Note
from ._google_auth import service as _google_service
from .events import CalTask, tasks_for_vault

MARKER = re.compile(r"\[sb:([^\]]+)\]")


class GoogleTasksSink:
    name = "gtasks"

    def sync(self, notes: List[Note], cfg: Config, decks: int | None = None) -> dict:
        svc = _google_service(cfg, "tasks", "v1")
        list_id = ensure_tasklist(svc, cfg)

        wanted: Dict[str, CalTask] = {t.uid: t for t in tasks_for_vault(notes, cfg)}
        existing = _existing(svc, list_id)

        created = updated = deleted = 0
        for uid, task in wanted.items():
            body = to_google(task)
            found = existing.get(uid)
            if found:
                svc.tasks().patch(
                    tasklist=list_id, task=found["id"], body=body
                ).execute()
                updated += 1
            else:
                svc.tasks().insert(tasklist=list_id, body=body).execute()
                created += 1

        for uid, found in existing.items():
            if uid not in wanted:
                svc.tasks().delete(tasklist=list_id, task=found["id"]).execute()
                deleted += 1

        return {
            "tasklist": list_id,
            "created": created,
            "updated": updated,
            "deleted": deleted,
        }


# --------------------------------------------------------------------------


def ensure_tasklist(svc, cfg: Config) -> str:
    """Find the configured list by title, or create it. `@default` uses
    whatever list Google considers primary."""
    title = (cfg.calendar.google_tasklist or "").strip()
    if not title or title == "@default":
        return "@default"
    token = None
    while True:
        resp = svc.tasklists().list(maxResults=100, pageToken=token).execute()
        for item in resp.get("items", []):
            if item.get("title", "").strip().lower() == title.lower():
                return item["id"]
        token = resp.get("nextPageToken")
        if not token:
            break
    made = svc.tasklists().insert(body={"title": title}).execute()
    return made["id"]


def to_google(task: CalTask) -> dict:
    """The emoji is already in `summary`; it is the only colour a task gets."""
    notes = task.description or ""
    if task.percent:
        notes = f"{task.percent}% of steps done\n\n{notes}" if notes else f"{task.percent}% done"
    return {
        "title": task.summary,
        "notes": f"{notes}\n\n[sb:{task.uid}]".strip(),
        # Tasks stores a timestamp but only ever shows the date.
        "due": dt.datetime.combine(task.due, dt.time.min).strftime(
            "%Y-%m-%dT00:00:00.000Z"
        ),
        "status": "needsAction",
    }


def uid_of(item: dict) -> Optional[str]:
    match = MARKER.search(item.get("notes") or "")
    return match.group(1) if match else None


def _existing(svc, list_id: str) -> Dict[str, dict]:
    """Every task this system owns, found by its `[sb:…]` marker."""
    out: Dict[str, dict] = {}
    token = None
    while True:
        resp = (
            svc.tasks()
            .list(
                tasklist=list_id,
                maxResults=100,
                pageToken=token,
                showCompleted=True,
                showHidden=True,
            )
            .execute()
        )
        for item in resp.get("items", []):
            uid = uid_of(item)
            if uid:
                out[uid] = item
        token = resp.get("nextPageToken")
        if not token:
            break
    return out
