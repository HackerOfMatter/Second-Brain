"""HTTP layer — Starlette, bound to localhost.

Every route hands its `Engine` call to `run_in_threadpool`. The engine does
blocking work -- disk, Ollama, the Google API -- and running that directly in
an async handler stalls the event loop. When it unblocks, uvicorn's keep-alive
timer fires late and can close the socket before the response is flushed: the
work succeeds but the browser reports "Failed to fetch".

Thin on purpose: every route is a few lines around an `Engine` call, so the
behaviour lives in testable code rather than in request handlers. Built on
Starlette rather than FastAPI to keep the install to four packages; the route
signatures are shaped so a later swap is mechanical.
"""

from __future__ import annotations

import base64
import contextlib
import json
import threading
import traceback
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import digest as digestmod
from . import freshness
from . import incidents as incidentsmod
from . import intake as intakemod
from .config import Config, load
from .engine import Engine

WEB_DIR = Path(__file__).parent / "web"


def json_default(obj: Any):
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    if isinstance(obj, Path):
        return str(obj)
    return str(obj)


def ok(payload: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(
        json.loads(json.dumps(payload, default=json_default)), status_code=status
    )


def guard(handler: Callable):
    """Turn exceptions into JSON the UI can display, and log the traceback."""

    async def wrapper(request: Request):
        try:
            return await handler(request)
        except ValueError as exc:
            return ok({"error": str(exc)}, status=400)
        except Exception as exc:
            traceback.print_exc()
            return ok({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    wrapper.__name__ = handler.__name__
    return wrapper


async def body_of(request: Request) -> dict:
    """Request JSON, or `{}` when there isn't any.

    A POST with no body at all is normal from the UI — "do this thing" needs
    no arguments — and `request.json()` raises on the empty string, which
    surfaced as a 400 on a button that was working perfectly.
    """
    try:
        data = await request.json()
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _positive_int(value: Any) -> int | None:
    """A count from a JSON body, or None. Never raises on junk — a malformed
    limit should cost the limit, not the request."""
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def build_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or load()
    engine = Engine(cfg)

    # -- routes -------------------------------------------------------------

    @guard
    async def index(request: Request):
        """The front door is the Today screen, not the workbench.

        Story G1. `run.py serve` opens `/` and nothing else, so whatever `/`
        serves is the page lj actually looks at every morning. The dashboard
        is a good workbench — twelve sections, every project's every step,
        every Area's habit form — and a bad answer to "what now?", because
        the answer is somewhere inside it. So Today owns `/`, and the
        dashboard keeps every byte of its behaviour at `/dashboard`.
        """
        return FileResponse(WEB_DIR / "today.html")

    @guard
    async def today_page(request: Request):
        return FileResponse(WEB_DIR / "today.html")

    @guard
    async def dashboard_page(request: Request):
        return FileResponse(WEB_DIR / "index.html")

    @guard
    async def inbox_page(request: Request):
        """Story G2 — keyboard-only triage of 00-Inbox."""
        return FileResponse(WEB_DIR / "inbox.html")

    @guard
    async def health(request: Request):
        """`?progress=1` adds the trigger distances — a full vault walk, so
        it is opt-in and the dashboard footer does not ask for it."""
        want = request.query_params.get("progress") in ("1", "true", "yes")
        return ok(await run_in_threadpool(engine.health, want))

    @guard
    async def llm_check(request: Request):
        """Ollama, step by step. `?deep=1` also makes the model answer once —
        the only proof it loads — so the page-load poll leaves it off."""
        from .llm import ollama_doctor

        deep = request.query_params.get("deep") in ("1", "true", "yes")
        result = await run_in_threadpool(ollama_doctor.check, engine.cfg.llm, deep)
        if result["state"] == "ready":
            await run_in_threadpool(engine.incidents.clear, incidentsmod.OLLAMA)
        return ok(result)

    @guard
    async def llm_fix(request: Request):
        """Start Ollama, and pull missing models when asked.

        This launches a process, so it demands a custom header: a cross-site
        page cannot send one without a CORS preflight, which this server never
        answers. Only the model names in config.yaml are ever pulled.
        """
        from .llm import ollama_doctor

        if request.headers.get("x-sb-action") != "1":
            return ok({"error": "missing X-SB-Action header"}, status=403)
        body = await body_of(request)
        result = await run_in_threadpool(
            ollama_doctor.fix, engine.cfg.llm, bool(body.get("pull"))
        )
        if result["check"]["state"] == "ready":
            await run_in_threadpool(engine.incidents.clear, incidentsmod.OLLAMA)
        return ok(result)

    @guard
    async def dashboard(request: Request):
        return ok(await run_in_threadpool(engine.dashboard))

    @guard
    async def inbox(request: Request):
        """The whole Inbox, uncapped, for the triage screen.

        `?limit=` is honoured for anything that wants the dashboard's cap;
        the default is no cap, because inbox zero is the goal and a list that
        stops short of the end cannot be finished.
        """
        raw = request.query_params.get("limit")
        limit = int(raw) if raw and raw.isdigit() and int(raw) > 0 else None
        return ok(await run_in_threadpool(engine.inbox, limit))

    @guard
    async def capture(request: Request):
        """A capture that names no bucket gets the configured default.

        It used to be `inbox` — which is a queue, and the failure mode of a
        queue is that it is not emptied. See CaptureConfig.
        """
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.capture,
            body.get("text", ""),
            body.get("bucket") or cfg.capture.default_bucket,
            body.get("title", ""),
            body.get("due") or None,
            str(body.get("folder") or ""),
        ))

    @guard
    async def session_status(request: Request):
        return ok(await run_in_threadpool(engine.session_status))

    @guard
    async def session_start(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.session_start,
            str(body.get("course") or ""),
            str(body.get("chapter") or ""),
        ))

    @guard
    async def session_end(request: Request):
        return ok(await run_in_threadpool(engine.session_end))

    @guard
    async def capture_term(request: Request):
        """One highlighted word, one term note, one card. See Engine.capture_term."""
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.capture_term,
            str(body.get("term") or ""),
            str(body.get("definition") or ""),
            source=str(body.get("source") or ""),
            folder=str(body.get("folder") or ""),
        ))

    @guard
    async def attach(request: Request):
        """A screenshot from the clipboard. See Engine.attach_image.

        base64 in a JSON body, same as the drop upload and for the same
        reason: the form parser would be a sixth package for one route.
        """
        body = await body_of(request)
        try:
            data = base64.b64decode(str(body.get("data") or ""), validate=True)
        except Exception:
            raise ValueError("the image was not valid base64")
        if len(data) > intakemod.MAX_UPLOAD_BYTES:
            raise ValueError("that image is past the upload limit")
        return ok(await run_in_threadpool(
            engine.attach_image, data,
            caption=str(body.get("caption") or ""),
            source=str(body.get("source") or ""),
            suffix=str(body.get("suffix") or ".png"),
        ))

    @guard
    async def collected_debt(request: Request):
        """Kept but never used. See sb/collected.py."""
        raw = request.query_params.get("limit")
        limit = int(raw) if raw and raw.isdigit() else 40
        return ok(await run_in_threadpool(engine.collected_debt, limit))

    @guard
    async def folders(request: Request):
        return ok(await run_in_threadpool(engine.folders))

    @guard
    async def move_to_folder(request: Request):
        """File several notes into one folder. See Engine.move_to_folder."""
        body = await body_of(request)
        ids = body.get("note_ids")
        if not isinstance(ids, list):
            raise ValueError("note_ids must be a list")
        return ok(await run_in_threadpool(
            engine.move_to_folder, ids, str(body.get("folder") or ""),
            create=body.get("create", True) is not False,
        ))

    @guard
    async def retire_note(request: Request):
        """The only way the system takes a note out of circulation, and it
        does not delete it. See Engine.retire_note."""
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.retire_note, request.path_params["note_id"],
            str(body.get("reason") or ""),
        ))

    @guard
    async def undefined_terms(request: Request):
        return ok({"terms": await run_in_threadpool(engine.undefined_terms)})

    @guard
    async def capture_plan(request: Request):
        """What a paste would become. Writes nothing — see Engine.capture_plan."""
        body = await body_of(request)
        return ok(
            await run_in_threadpool(
                engine.capture_plan,
                body.get("text", ""),
                body.get("bucket", "project"),
                due=body.get("due") or None,
                mode=str(body.get("mode") or "auto"),
            )
        )

    @guard
    async def capture_commit(request: Request):
        """File a reviewed plan. The items are whatever the preview shows,
        including any edit lj made to a title, a body, a bucket or a date."""
        body = await body_of(request)
        items = body.get("items")
        if not isinstance(items, list):
            raise ValueError("items must be a list")
        return ok(
            await run_in_threadpool(
                engine.capture_commit,
                items,
                bucket=str(body.get("bucket") or ""),
                due=body.get("due") or None,
            )
        )

    @guard
    async def generate_folder(request: Request):
        """Draft cards for every note under a folder.

        Long by nature, so the engine holds a wall-clock budget and returns
        what is left rather than running until the socket gives up.
        """
        body = await body_of(request)
        return ok(
            await run_in_threadpool(
                engine.generate_folder,
                str(body.get("folder") or ""),
                limit=_positive_int(body.get("limit")),
                max_cards=_positive_int(body.get("max_cards")),
                include_existing=bool(body.get("include_existing")),
                dry_run=bool(body.get("dry_run")),
            )
        )

    @guard
    async def list_notes(request: Request):
        bucket = request.query_params.get("bucket")
        notes = await run_in_threadpool(engine.notes, bucket)
        return ok(
            [
                {
                    "id": n.id,
                    "title": n.title,
                    "bucket": n.bucket.value,
                    "updated": n.updated,
                    "tags": n.tags,
                    "deadline": n.project.deadline if n.project else None,
                    "progress": round(n.project.progress, 3) if n.project else None,
                }
                for n in notes
            ]
        )

    @guard
    async def get_note(request: Request):
        note = await run_in_threadpool(engine.note, request.path_params["note_id"])
        return ok(note.model_dump(mode="json"))

    @guard
    async def toggle_step(request: Request):
        """Tick or untick. An optional `minutes` records how long it took."""
        body = await request.json() if await request.body() else {}
        return ok(await run_in_threadpool(
            lambda: engine.toggle_step(
                request.path_params["note_id"],
                request.path_params["step_id"],
                int(body["minutes"]) if body.get("minutes") is not None else None,
            )
        ))

    @guard
    async def start_step(request: Request):
        """Start the clock, so the estimate can be scored against reality."""
        return ok(await run_in_threadpool(
            engine.start_step,
            request.path_params["note_id"],
            request.path_params["step_id"],
        ))

    @guard
    async def estimates(request: Request):
        return ok(await run_in_threadpool(engine.estimates))

    @guard
    async def weekly_review(request: Request):
        days = int(request.query_params.get("days", 7))
        return ok(await run_in_threadpool(engine.weekly_review, days))

    @guard
    async def today_digest(request: Request):
        """F3' spike: what's going on today.

        Default is the full JSON payload — the dashboard panel reads this.
        `?format=text` returns the SMS body (<=320 chars); `?format=long`
        returns the same long form `python run.py today` prints.
        """
        payload = await run_in_threadpool(engine.today_digest)
        fmt = request.query_params.get("format")
        if fmt == "text":
            return PlainTextResponse(digestmod.render_text(payload))
        if fmt == "long":
            return PlainTextResponse(digestmod.render_long(payload))
        return ok(payload)

    @guard
    async def retention_dial(request: Request):
        return ok(await run_in_threadpool(engine.retention_dial))

    @guard
    async def atomicity(request: Request):
        return ok(await run_in_threadpool(engine.atomicity))

    @guard
    async def thresholds(request: Request):
        return ok(await run_in_threadpool(engine.thresholds))

    @guard
    async def fit_weights(request: Request):
        body = await request.json() if await request.body() else {}
        return ok(await run_in_threadpool(engine.fit_weights, bool(body.get("write"))))

    @guard
    async def label_link(request: Request):
        """A suggested link was right, or it was not. One labelled example."""
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.label_link,
            request.path_params["note_id"],
            body.get("target", ""),
            bool(body.get("kept")),
            float(body.get("score") or 0.0),
        ))

    @guard
    async def reparse(request: Request):
        return ok(await run_in_threadpool(engine.reparse, request.path_params["note_id"]))

    @guard
    async def replan(request: Request):
        return ok(await run_in_threadpool(engine.replan, request.path_params["note_id"]))

    @guard
    async def move(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.move, request.path_params["note_id"], body["bucket"]
        ))

    @guard
    async def habit(request: Request):
        """Cadence, target, and the fields that actually move the needle.

        Every field is optional: filling in only the cue must not require
        restating a schedule the caller is not changing.
        """
        body = await request.json()
        return ok(await run_in_threadpool(
            lambda: engine.set_habit(
                request.path_params["note_id"],
                cadence=body.get("cadence"),
                target_count=(
                    int(body["target_count"]) if body.get("target_count") is not None else None
                ),
                **{k: body.get(k) for k in Engine.HABIT_TEXT_FIELDS},
            )
        ))

    @guard
    async def habit_done(request: Request):
        """One occurrence, with the time and place that make it measurable."""
        body = await request.json() if await request.body() else {}
        return ok(await run_in_threadpool(
            engine.log_habit,
            request.path_params["note_id"],
            body.get("on"),
            body.get("at", ""),
            body.get("place", ""),
        ))

    @guard
    async def habit_checkin(request: Request):
        body = await request.json() if await request.body() else {}
        return ok(await run_in_threadpool(
            engine.habit_checkin,
            request.path_params["note_id"],
            body.get("decision", "continue"),
            int(body["target_count"]) if body.get("target_count") is not None else None,
        ))

    @guard
    async def schedule(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.set_schedule,
            request.path_params["note_id"],
            enabled=body.get("enabled"),
            time=body.get("time"),
            duration_minutes=body.get("duration_minutes"),
            days=body.get("days"),
            monthday=body.get("monthday"),
            until=body.get("until"),
        ))

    @guard
    async def deadline(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.set_deadline,
            request.path_params["note_id"],
            body.get("date"),
            bool(body.get("confirm")),
        ))

    @guard
    async def review_answer(request: Request):
        body = await body_of(request)
        action = str(body.get("action") or "keep").lower()
        note_id = request.path_params["note_id"]
        if action == "snooze":
            return ok(await run_in_threadpool(
                engine.snooze_review, note_id, int(body.get("days") or 30)))
        if action == "restore":
            return ok(await run_in_threadpool(engine.restore_resource, note_id))
        if action not in ("keep", "archive"):
            raise ValueError(f"unknown review action {action!r}")
        return ok(await run_in_threadpool(engine.answer_review, note_id, action == "keep"))

    @guard
    async def category(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.set_category, request.path_params["note_id"], body.get("category")
        ))

    @guard
    async def categories(request: Request):
        return ok(await run_in_threadpool(engine.categories))

    # -- tutor --------------------------------------------------------------

    @guard
    async def study_page(request: Request):
        return FileResponse(WEB_DIR / "study.html")

    async def review_page(request: Request):
        """Tier 3: one page for the week, rather than three timers."""
        return FileResponse(WEB_DIR / "review.html")

    @guard
    async def study_overview(request: Request):
        return ok(await run_in_threadpool(engine.study_overview))

    @guard
    async def study_stats(request: Request):
        return ok(await run_in_threadpool(engine.study_stats))

    @guard
    async def study_reminder(request: Request):
        """What study_reminder.pyw asks when the app happens to be running.

        Story H3. The notifier works with the app closed — it reads the deck
        store itself — so this is an optimisation, not a dependency: when the
        server is already up, one localhost call is cheaper than a second
        process re-reading every deck file on the same disk. Read-only; the
        notifier owns the state file, because it is the half that still works
        when this endpoint does not.
        """
        return ok(await run_in_threadpool(engine.study_reminder))

    @guard
    async def study_session(request: Request):
        body = await body_of(request)
        subjects = body.get("subjects") or None
        folders = body.get("folders") or None
        limit = int(body["limit"]) if body.get("limit") else None
        return ok(await run_in_threadpool(engine.study_session, subjects, limit, folders))

    @guard
    async def study_reveal(request: Request):
        return ok(await run_in_threadpool(
            engine.study_reveal,
            request.path_params["note_id"],
            request.path_params["card_id"],
        ))

    @guard
    async def study_answer(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            lambda: engine.study_answer(
                request.path_params["note_id"],
                request.path_params["card_id"],
                grade=int(body["grade"]) if body.get("grade") is not None else None,
                mode=body.get("mode", "self"),
                typed=body.get("typed", ""),
                seconds=float(body.get("seconds") or 0),
                confidence=body.get("confidence"),
            )
        ))

    @guard
    async def study_mark(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.study_mark,
            request.path_params["note_id"],
            request.path_params["card_id"],
            body.get("typed", ""),
        ))

    @guard
    async def study_explain(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.study_explain,
            request.path_params["note_id"],
            request.path_params["card_id"],
            body.get("question", ""),
        ))

    @guard
    async def study_why(request: Request):
        """lj's own explanation, marked. Nothing scheduled — see engine."""
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.study_self_explain,
            request.path_params["note_id"],
            request.path_params["card_id"],
            body.get("said", ""),
        ))

    # -- the info manager ----------------------------------------------------

    @guard
    async def ask(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.ask,
            body.get("question", ""),
            bool(body.get("include_archive")),
            int(body.get("k") or 6),
        ))

    @guard
    async def index_status(request: Request):
        return ok(await run_in_threadpool(engine.index_status))

    @guard
    async def reindex(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(engine.reindex, bool(body.get("force"))))

    @guard
    async def connect_note(request: Request):
        body = await body_of(request)
        return ok(
            await run_in_threadpool(
                lambda: engine.connect(
                    request.path_params["note_id"],
                    include_archive=bool(body.get("include_archive")),
                    write=body.get("write", True) is not False,
                    allow_model=body.get("allow_model", True) is not False,
                )
            )
        )

    @guard
    async def connect_all(request: Request):
        """Deliberately POST-only and never called on a timer: this is the
        'tidy my graph' button, and it costs a model call per note."""
        body = await body_of(request)
        return ok(
            await run_in_threadpool(
                lambda: engine.connect_all(
                    bucket=body.get("bucket") or None,
                    include_archive=bool(body.get("include_archive")),
                    reindex=body.get("reindex", True) is not False,
                    write=body.get("write", True) is not False,
                    changed_only=body.get("changed_only", True) is not False,
                    allow_model=body.get("allow_model", True) is not False,
                )
            )
        )

    @guard
    async def get_deck(request: Request):
        return ok(await run_in_threadpool(engine.deck_dict, request.path_params["note_id"]))

    @guard
    async def generate_cards(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(
            lambda: engine.generate_cards(
                request.path_params["note_id"],
                max_cards=int(body["max_cards"]) if body.get("max_cards") else None,
                source=body.get("source", ""),
            )
        ))

    @guard
    async def add_card(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            lambda: engine.add_card(
                request.path_params["note_id"],
                body.get("front", ""),
                body.get("back", ""),
                hint=body.get("hint", ""),
                source=body.get("source", ""),
            )
        ))

    @guard
    async def update_card(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            lambda: engine.update_card(
                request.path_params["note_id"],
                request.path_params["card_id"],
                front=body.get("front"),
                back=body.get("back"),
                hint=body.get("hint"),
                source=body.get("source"),
                status=body.get("status"),
                delete=bool(body.get("delete")),
            )
        ))

    @guard
    async def approve_cards(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.approve_drafts, request.path_params["note_id"], body.get("cards")
        ))

    @guard
    async def intake_run(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.intake, bool(body.get("dry_run")), body.get("limit")
        ))

    @guard
    async def drop_upload(request: Request):
        """Files dragged onto the dashboard, written into Drop/ and filed.

        base64 in a JSON body rather than multipart: Starlette's form parser
        needs `python-multipart`, and a sixth package for one route is a bad
        trade against a transport the standard library already speaks on both
        ends. Over loopback the ~33% encoding overhead costs nothing
        measurable, and the 25 MB per-file cap in `intake.accept_upload`
        keeps the whole body inside what a JSON parse should ever hold.
        """
        body = await body_of(request)
        raw = body.get("files")
        if not isinstance(raw, list) or not raw:
            raise ValueError("files must be a non-empty list")
        files: list[tuple[str, bytes]] = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each file must be an object with name and data")
            name = str(item.get("name") or "")
            try:
                data = base64.b64decode(str(item.get("data") or ""), validate=True)
            except Exception:
                raise ValueError(f"{name or 'a file'}: the upload was not valid base64")
            files.append((name, data))
        return ok(await run_in_threadpool(engine.accept_uploads, files))

    @guard
    async def classify(request: Request):
        body = await body_of(request)
        return ok(await run_in_threadpool(
            engine.classify_note, request.path_params["note_id"], body.get("bucket", "")
        ))

    @guard
    async def problems(request: Request):
        """What is broken right now, for the banner.

        Its own endpoint as well as a key inside `/api/health` because the two
        have different costs and different callers: the dashboard already
        fetches health and should read the key it is given, while anything
        that only wants the banner — a future status bar, a poll on a page
        that has no other reason to touch health — should not pay for a vault
        walk to get it. See sb/incidents.py.
        """
        items = await run_in_threadpool(engine.incidents.open)
        return ok({"problems": items, "count": len(items)})

    @guard
    async def problems_clear(request: Request):
        """Dismiss one problem by id, or all of them.

        Dismissing is not fixing, and the store knows it: anything still true
        is filed again by the job that hits it, and `health()` re-files a
        standing calendar-auth failure on the very next poll. So this is an
        "I have read that" button, not an override.
        """
        body = await body_of(request)
        ident = str(body.get("id") or "")
        if ident:
            kind, _, key = ident.partition("/")
            cleared = int(await run_in_threadpool(engine.incidents.clear, kind, key))
        else:
            cleared = await run_in_threadpool(engine.incidents.clear_all)
        return ok({"cleared": cleared, "problems": engine.incidents.open()})

    @guard
    async def queue_status(request: Request):
        return ok({
            "cards": await run_in_threadpool(engine.card_queue.pending),
        })

    @guard
    async def queue_drain(request: Request):
        return ok(await run_in_threadpool(engine.drain_card_queue))

    @guard
    async def calendar_sync(request: Request):
        return ok(await run_in_threadpool(engine.sync_calendar))

    @guard
    async def calendar_file(request: Request):
        path = engine.ics_path()
        if not path.exists():
            await run_in_threadpool(engine.sync_calendar)
        if not path.exists():
            return PlainTextResponse("no calendar generated yet", status_code=404)
        return Response(
            path.read_bytes(),
            media_type="text/calendar; charset=utf-8",
            headers={"Content-Disposition": 'inline; filename="secondbrain.ics"'},
        )

    routes = [
        Route("/", index),
        Route("/today", today_page),
        Route("/dashboard", dashboard_page),
        Route("/inbox", inbox_page),
        Route("/api/health", health),
        Route("/api/llm/check", llm_check),
        Route("/api/llm/fix", llm_fix, methods=["POST"]),
        Route("/api/problems", problems),
        Route("/api/problems/clear", problems_clear, methods=["POST"]),
        Route("/api/queue", queue_status),
        Route("/api/queue/drain", queue_drain, methods=["POST"]),
        Route("/api/dashboard", dashboard),
        Route("/api/inbox", inbox),
        Route("/api/capture", capture, methods=["POST"]),
        Route("/api/session", session_status),
        Route("/api/session/start", session_start, methods=["POST"]),
        Route("/api/session/end", session_end, methods=["POST"]),
        Route("/api/capture/term", capture_term, methods=["POST"]),
        Route("/api/terms/undefined", undefined_terms),
        Route("/api/folders", folders),
        Route("/api/collected", collected_debt),
        Route("/api/attach", attach, methods=["POST"]),
        Route("/api/notes/folder", move_to_folder, methods=["POST"]),
        Route("/api/notes/{note_id}/retire", retire_note, methods=["POST"]),
        Route("/api/capture/plan", capture_plan, methods=["POST"]),
        Route("/api/capture/commit", capture_commit, methods=["POST"]),
        Route("/api/notes", list_notes),
        Route("/api/notes/{note_id}", get_note),
        Route("/api/notes/{note_id}/steps/{step_id}/toggle", toggle_step, methods=["POST"]),
        Route("/api/notes/{note_id}/steps/{step_id}/start", start_step, methods=["POST"]),
        Route("/api/estimates", estimates),
        Route("/api/review/weekly", weekly_review),
        Route("/api/today", today_digest),
        Route("/api/study/retention", retention_dial),
        Route("/api/atomicity", atomicity),
        Route("/api/thresholds", thresholds),
        Route("/api/study/fit", fit_weights, methods=["GET", "POST"]),
        Route("/api/notes/{note_id}/label-link", label_link, methods=["POST"]),
        Route("/api/notes/{note_id}/reparse", reparse, methods=["POST"]),
        Route("/api/notes/{note_id}/replan", replan, methods=["POST"]),
        Route("/api/notes/{note_id}/move", move, methods=["POST"]),
        Route("/api/notes/{note_id}/habit", habit, methods=["POST"]),
        Route("/api/notes/{note_id}/habit/done", habit_done, methods=["POST"]),
        Route("/api/notes/{note_id}/habit/checkin", habit_checkin, methods=["POST"]),
        Route("/api/notes/{note_id}/schedule", schedule, methods=["POST"]),
        Route("/api/notes/{note_id}/category", category, methods=["POST"]),
        Route("/api/notes/{note_id}/deadline", deadline, methods=["POST"]),
        Route("/api/notes/{note_id}/review", review_answer, methods=["POST"]),
        Route("/api/notes/{note_id}/classify", classify, methods=["POST"]),
        Route("/api/categories", categories),
        # -- the Drop folder
        Route("/api/intake", intake_run, methods=["POST"]),
        Route("/api/drop/upload", drop_upload, methods=["POST"]),
        # -- tutor
        Route("/study", study_page),
        Route("/review", review_page),
        Route("/api/study/overview", study_overview),
        Route("/api/study/stats", study_stats),
        Route("/api/study/reminder", study_reminder),
        Route("/api/study/session", study_session, methods=["GET", "POST"]),
        Route("/api/study/{note_id}/{card_id}/reveal", study_reveal),
        Route("/api/study/{note_id}/{card_id}/mark", study_mark, methods=["POST"]),
        Route("/api/study/{note_id}/{card_id}/answer", study_answer, methods=["POST"]),
        Route("/api/study/{note_id}/{card_id}/explain", study_explain, methods=["POST"]),
        Route("/api/study/{note_id}/{card_id}/why", study_why, methods=["POST"]),
        Route("/api/ask", ask, methods=["POST"]),
        Route("/api/index", index_status),
        Route("/api/index/rebuild", reindex, methods=["POST"]),
        Route("/api/notes/{note_id}/connect", connect_note, methods=["POST"]),
        Route("/api/connect", connect_all, methods=["POST"]),
        Route("/api/decks/generate-folder", generate_folder, methods=["POST"]),
        Route("/api/decks/{note_id}", get_deck),
        Route("/api/decks/{note_id}/generate", generate_cards, methods=["POST"]),
        Route("/api/decks/{note_id}/approve", approve_cards, methods=["POST"]),
        Route("/api/decks/{note_id}/cards", add_card, methods=["POST"]),
        Route("/api/decks/{note_id}/cards/{card_id}", update_card, methods=["POST"]),
        Route("/api/calendar/sync", calendar_sync, methods=["POST"]),
        Route("/calendar.ics", calendar_file),
        Mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static"),
    ]

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        # `lifespan` rather than the older `on_startup` list: Starlette 1.0
        # dropped that argument, and lifespan works on every version either
        # machine is likely to have.
        start_drop_watcher(engine)
        # A note edited in Obsidian is searchable without anyone pressing
        # anything (sprint 4, K3). Separate thread from the Drop watcher on
        # purpose: they poll at different rates, and a Drop folder that cannot
        # be read must not stop the index following lj's edits.
        freshness.start(engine)
        # A reboot leaves Ollama off unless its tray app is set to start at
        # login. Start it in the background so the first capture of the day
        # gets the model rather than the rule-based fallback.
        if cfg.llm.autostart and (cfg.llm.provider or "ollama").lower() == "ollama":
            threading.Thread(target=_autostart_ollama, args=(engine,), daemon=True).start()
        yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine = engine
    app.state.config = cfg
    return app


def _autostart_ollama(engine: Engine) -> None:
    from .llm import ollama_doctor

    try:
        result = ollama_doctor.start_server(engine.cfg.llm)
        if result.get("started"):
            engine.vault.log_line("llm", f"started Ollama via {result.get('via')}")
        elif result.get("reason") != "already running":
            engine.vault.log_line("llm", f"could not start Ollama: {result.get('reason')}")
    except Exception as exc:  # never take the app down over this
        engine.vault.log_line("llm", f"autostart failed: {exc!r}")


def start_drop_watcher(engine: Engine) -> None:
    """Poll the Drop folder while the app is running.

    Dropping a file should be the whole interaction — a folder you have to
    remember to press a button about is a folder that fills up. The button
    stays for "do it now", and this is the same call on a timer.

    Polling rather than filesystem events on purpose: the vault sits in
    OneDrive, where a synced file arrives as a rename of a temp file and fires
    events that do not correspond to a finished file. `intake.candidates`
    already waits for a file to hold still, which makes a 20-second poll both
    simpler and more correct than reacting to every event.

    Daemon thread, so closing the app closes it; and it never raises into the
    server — a folder that cannot be read is a log line, not a dead process.
    """
    cfg = engine.cfg
    if not cfg.intake.watch or getattr(engine, "_watching", False):
        return
    engine._watching = True

    import threading
    import time

    def loop() -> None:
        while True:
            time.sleep(max(5.0, float(cfg.intake.poll_seconds)))
            try:
                result = engine.intake()
                if result.get("filed") or result.get("asking"):
                    engine.vault.log_line(
                        "intake",
                        f"watch: {result['filed']} filed, {result['asking']} asked",
                    )
            except Exception as exc:  # never take the server down with it
                # ...but never swallow it either. A watcher that has been
                # failing every twenty seconds since 2am looks exactly like a
                # watcher with nothing to do, and looked like one for a whole
                # sprint. See sb/incidents.py.
                try:
                    engine.vault.log_line("intake", f"watch failed: {exc!r}")
                except Exception:
                    pass
                try:
                    engine.incidents.record(
                        "drop",
                        f"The Drop folder watcher is failing ({type(exc).__name__}).",
                        hint=f"Check {cfg.drop_dir} is reachable, then press "
                             "“File dropped notes”.",
                        detail=str(exc)[:400],
                    )
                except Exception:
                    pass
            else:
                try:
                    engine.incidents.clear("drop")
                except Exception:
                    pass
            # The same tick is the natural place to notice the model came back:
            # card generation queued during an outage drains itself rather than
            # waiting for lj to remember which notes were waiting.
            try:
                if engine.card_queue.count():
                    engine.drain_card_queue()
            except Exception as exc:
                try:
                    engine.vault.log_line("study", f"queue drain failed: {exc!r}")
                except Exception:
                    pass

    threading.Thread(target=loop, name="drop-watcher", daemon=True).start()
