"""Google Calendar sink.

Optional: needs `pip install google-api-python-client google-auth-oauthlib` and
a one-time OAuth consent. See docs/google-calendar.md. Until then the .ics sink
covers the same ground offline.

Sync model is *upsert by stable id*. Every event carries the UID from
events.py as its Google event id (lowercased and stripped to the allowed
charset), so re-running sync updates events instead of duplicating them, and
events whose source step was completed or deleted get removed.

This sink carries *events only* — things that occupy time: Project work blocks,
recurring Area blocks, Resource review prompts and the weekly schedule review.
Project due dates go to Google Tasks instead (see gtasks.py), which is why the
old `deadline` and `habit` kinds are still swept below: any all-day banner left
over from the previous model is no longer wanted, and the sweep deletes it.
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List

from .. import taxonomy
from ..config import Config
from ..models import Note
from ._google_auth import SCOPES, service as _service
from .events import CalEvent, events_for_vault

_ID_SAFE = re.compile(r"[^a-v0-9]")  # Google event ids: base32hex, 5-1024 chars

#: Kinds this sink owns now, plus the two it used to own. Listing the retired
#: kinds is what migrates an existing calendar: they are swept, found absent
#: from `wanted`, and deleted.
KINDS = ("block", "area", "review", "schedule-review", "study", "deadline", "habit")


class GoogleSink:
    name = "google"

    def sync(self, notes: List[Note], cfg: Config, decks: int | None = None) -> dict:
        service = _service(cfg)
        cal_id = cfg.calendar.google_calendar_id
        wanted = {_gid(e.uid): e for e in events_for_vault(notes, cfg, decks)}
        existing = _existing(service, cal_id)

        created = updated = deleted = 0
        for gid, ev in wanted.items():
            body = _to_google(ev, cfg)
            if gid in existing:
                service.events().update(calendarId=cal_id, eventId=gid, body=body).execute()
                updated += 1
            else:
                body["id"] = gid
                try:
                    service.events().insert(calendarId=cal_id, body=body).execute()
                    created += 1
                except Exception:
                    # id already used by a deleted event -> revive it
                    service.events().update(
                        calendarId=cal_id, eventId=gid, body=body
                    ).execute()
                    updated += 1

        for gid in existing:
            if gid not in wanted:
                service.events().delete(calendarId=cal_id, eventId=gid).execute()
                deleted += 1

        return {"created": created, "updated": updated, "deleted": deleted}


# --------------------------------------------------------------------------


def _gid(uid: str) -> str:
    """Google event ids allow only base32hex characters."""
    base = _ID_SAFE.sub("", uid.lower().replace("@secondbrain.local", ""))
    return ("sb" + base)[:1000].ljust(5, "0")


def _to_google(ev: CalEvent, cfg: Config) -> Dict:
    body: Dict = {
        "summary": ev.summary,
        "description": ev.description,
        "source": {"title": "Second Brain", "url": f"http://{cfg.host}:{cfg.port}/#{ev.note_id}"},
        "extendedProperties": {
            "private": {
                "sb_note_id": ev.note_id,
                "sb_kind": ev.kind,
                "sb_category": ev.category,
            }
        },
        "reminders": {
            "useDefault": False,
            "overrides": [
                {"method": "popup", "minutes": max(0, m)} for m in (ev.reminders or [])
            ][:5],
        },
    }
    if cfg.calendar.color_events:
        body["colorId"] = taxonomy.get(ev.category, cfg).google_color_id
    if ev.rrule:
        body["recurrence"] = [f"RRULE:{ev.rrule}"]
    if ev.all_day:
        start = ev.start if isinstance(ev.start, dt.date) else ev.start.date()
        body["start"] = {"date": start.isoformat()}
        body["end"] = {"date": (start + dt.timedelta(days=1)).isoformat()}
    else:
        # A recurring event must carry a named zone; Google rejects a floating
        # start on a series, so fall back to the machine's own zone name.
        tz = cfg.calendar.timezone or (cfg.tzname() if ev.rrule else None)
        body["start"] = {"dateTime": ev.start.isoformat()}
        body["end"] = {"dateTime": ev.end_or_default.isoformat()}
        if tz:
            body["start"]["timeZone"] = tz
            body["end"]["timeZone"] = tz
    return body


def _existing(service, cal_id: str) -> Dict[str, dict]:
    """Every event this system owns, found by its private marker property."""
    out: Dict[str, dict] = {}
    # the property filter matches one kind at a time, so sweep each in turn
    for kind in KINDS:
        token = None
        while True:
            resp = (
                service.events()
                .list(
                    calendarId=cal_id,
                    privateExtendedProperty=f"sb_kind={kind}",
                    maxResults=2500,
                    pageToken=token,
                    showDeleted=False,
                    singleEvents=False,  # a series is one row, not 200
                )
                .execute()
            )
            for item in resp.get("items", []):
                out[item["id"]] = item
            token = resp.get("nextPageToken")
            if not token:
                break
    return out


__all__ = ["GoogleSink", "KINDS", "SCOPES"]
