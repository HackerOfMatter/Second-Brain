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

import contextlib
import json
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


def build_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or load()
    engine = Engine(cfg)

    # -- routes -------------------------------------------------------------

    @guard
    async def index(request: Request):
        return FileResponse(WEB_DIR / "index.html")

    @guard
    async def health(request: Request):
        """`?progress=1` adds the trigger distances — a full vault walk, so
        it is opt-in and the dashboard footer does not ask for it."""
        want = request.query_params.get("progress") in ("1", "true", "yes")
        return ok(await run_in_threadpool(engine.health, want))

    @guard
    async def dashboard(request: Request):
        return ok(await run_in_threadpool(engine.dashboard))

    @guard
    async def capture(request: Request):
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.capture,
            body.get("text", ""),
            body.get("bucket", "inbox"),
            body.get("title", ""),
            body.get("due") or None,
        ))

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
        Route("/api/health", health),
        Route("/api/problems", problems),
        Route("/api/problems/clear", problems_clear, methods=["POST"]),
        Route("/api/queue", queue_status),
        Route("/api/queue/drain", queue_drain, methods=["POST"]),
        Route("/api/dashboard", dashboard),
        Route("/api/capture", capture, methods=["POST"]),
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
        # -- tutor
        Route("/study", study_page),
        Route("/review", review_page),
        Route("/api/study/overview", study_overview),
        Route("/api/study/stats", study_stats),
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
        yield

    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.engine = engine
    app.state.config = cfg
    return app


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
