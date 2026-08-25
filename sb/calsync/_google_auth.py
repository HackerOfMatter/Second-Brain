"""One OAuth token, two Google APIs.

Calendar holds the events; Tasks holds the due dates. They are separate APIs
with separate scopes but the same consent screen, so one token file covers
both.

Everything below exists because a saved token can go stale in more ways than
"expired", and every one of them used to end the same way: a `RefreshError`
raised out of `sync()`, caught by the sink wrapper, written to the log, and
repeated forever. A token is a cache, not a source of truth — when it stops
matching reality the right move is to throw it away and ask the human once,
not to fail in a loop.

The three ways it goes stale, and what each needed:

1. **Fewer scopes than we now need.** `Credentials.from_authorized_user_file`
   takes the scopes you *ask for* and stamps them onto the object; the ones
   actually granted, sitting right there in the file, are ignored whenever
   `scopes` is not None. So `creds.has_scopes(...)` answers "did I ask for
   these", never "did the user grant these", and a calendar-only token sails
   past the check and then cannot create tasks. Read the file's own `scopes`
   list instead.

2. **A token from a different OAuth client.** `from_authorized_user_file`
   refreshes using the `client_id` inside *token.json*, not the one in
   credentials.json. Replace the OAuth client — new project, regenerated
   secret, deleted client — and the old refresh token keeps being sent to a
   client that no longer exists. Google answers `deleted_client`, which reads
   like a server problem and is really a stale file. Compare the two ids.

3. **A refresh that fails for any other reason** — revoked access, an account
   that changed password, a consent screen back in testing. Catch it, discard
   the token, re-consent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Tuple

from ..config import Config

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/tasks",
]


def service(cfg: Config, api: str = "calendar", version: str = "v3"):
    try:
        from google.auth.transport.requests import Request
        from google.oauth2.credentials import Credentials
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError(
            "Google sync needs: pip install google-api-python-client "
            "google-auth-oauthlib  (or keep calendar.sink: ics)"
        ) from exc

    token_path = Path(cfg.system_dir) / cfg.calendar.google_token_file
    creds_path = Path(cfg.system_dir) / cfg.calendar.google_credentials_file

    creds = None
    reason = usable_token(token_path, creds_path)
    if reason is None:
        try:
            creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (ValueError, json.JSONDecodeError):
            creds = None

    if creds and not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
            except Exception:
                # Dead refresh token. Fall through to a fresh consent rather
                # than raising the same error on every sync from now on.
                creds = None
        else:
            creds = None

    if not creds or not creds.valid:
        creds = _consent(creds_path, token_path, InstalledAppFlow)

    return build(api, version, credentials=creds, cache_discovery=False)


# --------------------------------------------------------------------------


def usable_token(token_path: Path, creds_path: Path) -> Optional[str]:
    """None if the saved token is worth trying, otherwise a plain-English
    reason it is not. Pure file inspection — no network, so `run.py doctor`
    can call it to explain a broken sync without triggering a browser."""
    if not token_path.exists():
        return "no token yet — the next sync will open a browser to authorise"

    try:
        info = json.loads(token_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "token file is unreadable"

    if not info.get("refresh_token"):
        return "token has no refresh token"

    granted = _scope_list(info.get("scopes"))
    missing = [s for s in SCOPES if s not in granted]
    if missing:
        short = ", ".join(s.rsplit("/", 1)[-1] for s in missing)
        return f"token was granted before {short} was needed"

    token_client = info.get("client_id")
    current_client = client_id_of(creds_path)
    if current_client and token_client and token_client != current_client:
        return "token belongs to a different OAuth client than credentials.json"

    return None


def client_id_of(creds_path: Path) -> Optional[str]:
    """The client_id inside credentials.json, whichever shape it takes."""
    try:
        data = json.loads(creds_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    block = data.get("installed") or data.get("web") or data
    cid = block.get("client_id")
    return cid if isinstance(cid, str) else None


def _scope_list(value) -> List[str]:
    if isinstance(value, str):
        return value.split()
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


def _consent(creds_path: Path, token_path: Path, InstalledAppFlow):
    if not creds_path.exists():
        raise RuntimeError(
            f"Put your OAuth client secret at {creds_path} "
            "(see docs/google-calendar.md)"
        )
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(creds_path), SCOPES)
        creds = flow.run_local_server(port=0)
    except Exception as exc:
        raise RuntimeError(
            f"Google authorisation failed: {type(exc).__name__}: {exc}\n"
            f"Check that credentials.json at {creds_path} is a current "
            "'Desktop app' OAuth client, and that both the Google Calendar API "
            "and the Google Tasks API are enabled for that project."
        ) from exc

    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json(), encoding="utf-8")
    return creds


def status(cfg: Config) -> dict:
    """What `run.py doctor` reports. Never touches the network."""
    token_path = Path(cfg.system_dir) / cfg.calendar.google_token_file
    creds_path = Path(cfg.system_dir) / cfg.calendar.google_credentials_file
    reason = usable_token(token_path, creds_path)
    return {
        "credentials": creds_path.exists(),
        "token": token_path.exists(),
        "ready": reason is None and creds_path.exists(),
        "reason": reason,
        "scopes": SCOPES,
    }
