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


def build_app(cfg: Config | None = None) -> Starlette:
    cfg = cfg or load()
    engine = Engine(cfg)

    # -- routes -------------------------------------------------------------

    @guard
    async def index(request: Request):
        return FileResponse(WEB_DIR / "index.html")

    @guard
    async def health(request: Request):
        return ok(await run_in_threadpool(engine.health))

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
        return ok(await run_in_threadpool(
            engine.toggle_step,
            request.path_params["note_id"],
            request.path_params["step_id"],
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
        body = await request.json()
        return ok(await run_in_threadpool(
            engine.set_habit,
            request.path_params["note_id"],
            body.get("cadence", "weekly"),
            int(body.get("target_count", 3)),
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
        body = await request.json() if request.method == "POST" else {}
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

    @guard
    async def study_overview(request: Request):
        return ok(await run_in_threadpool(engine.study_overview))

    @guard
    async def study_stats(request: Request):
        return ok(await run_in_threadpool(engine.study_stats))

    @guard
    async def study_session(request: Request):
        body = await request.json() if request.method == "POST" else {}
        subjects = body.get("subjects") or None
        limit = int(body["limit"]) if body.get("limit") else None
        return ok(await run_in_threadpool(engine.study_session, subjects, limit))

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
        body = await request.json() if request.method == "POST" else {}
        return ok(await run_in_threadpool(engine.reindex, bool(body.get("force"))))

    @guard
    async def connect_note(request: Request):
        body = await request.json() if request.method == "POST" else {}
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
        body = await request.json() if request.method == "POST" else {}
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
        body = await request.json() if request.method == "POST" else {}
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
        body = await request.json() if request.method == "POST" else {}
        return ok(await run_in_threadpool(
            engine.approve_drafts, request.path_params["note_id"], body.get("cards")
        ))

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
        Route("/api/dashboard", dashboard),
        Route("/api/capture", capture, methods=["POST"]),
        Route("/api/notes", list_notes),
        Route("/api/notes/{note_id}", get_note),
        Route("/api/notes/{note_id}/steps/{step_id}/toggle", toggle_step, methods=["POST"]),
        Route("/api/notes/{note_id}/reparse", reparse, methods=["POST"]),
        Route("/api/notes/{note_id}/replan", replan, methods=["POST"]),
        Route("/api/notes/{note_id}/move", move, methods=["POST"]),
        Route("/api/notes/{note_id}/habit", habit, methods=["POST"]),
        Route("/api/notes/{note_id}/schedule", schedule, methods=["POST"]),
        Route("/api/notes/{note_id}/category", category, methods=["POST"]),
        Route("/api/notes/{note_id}/deadline", deadline, methods=["POST"]),
        Route("/api/notes/{note_id}/review", review_answer, methods=["POST"]),
        Route("/api/categories", categories),
        # -- tutor
        Route("/study", study_page),
        Route("/api/study/overview", study_overview),
        Route("/api/study/stats", study_stats),
        Route("/api/study/session", study_session, methods=["GET", "POST"]),
        Route("/api/study/{note_id}/{card_id}/reveal", study_reveal),
        Route("/api/study/{note_id}/{card_id}/mark", study_mark, methods=["POST"]),
        Route("/api/study/{note_id}/{card_id}/answer", study_answer, methods=["POST"]),
        Route("/api/study/{note_id}/{card_id}/explain", study_explain, methods=["POST"]),
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

    app = Starlette(routes=routes)
    app.state.engine = engine
    app.state.config = cfg
    return app
