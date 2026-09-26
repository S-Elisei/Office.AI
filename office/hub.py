"""FastAPI skeleton: lifespan wiring, health check, and SSE broadcast infrastructure."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from office import core, db, git, marks, mcp, stages
from office.bus import Bus
from office.config import load_config
from office.web.routes import router as web_router

_OFFICE_LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
_office_log = logging.getLogger("office")
if not _office_log.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(_OFFICE_LOG_FORMAT))
    _office_log.addHandler(_handler)
    _office_log.setLevel(logging.INFO)
    _office_log.propagate = False


def _log_to_file(config) -> None:
    """Append the office's log to <data root>/office.log for the rest of this run.

    Called once the hub holds a config and has made the layout.
    """
    if any(isinstance(h, logging.FileHandler) for h in _office_log.handlers):
        return
    try:
        config.root.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(config.root / "office.log", mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(_OFFICE_LOG_FORMAT))
        _office_log.addHandler(handler)
    except Exception:  # noqa: BLE001
        _office_log.exception("could not open the office log file; logging to stderr only")


class Broadcaster:
    """Fan-out of server-sent events to connected clients.

    One asyncio.Queue per connection.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[str]] = set()

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._subscribers.discard(queue)

    async def publish(self, kind: str, payload: dict[str, Any]) -> None:
        message = f"event: {kind}\ndata: {json.dumps(payload)}\n\n"
        for queue in list(self._subscribers):
            await queue.put(message)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything the hub spawns. The hub must not carry the mark itself.
    os.environ.pop(marks.MARK_ENV, None)

    config = load_config()
    config.ensure_layout()
    _log_to_file(config)
    conn = db.connect(config.db_path)
    db.init_schema(conn)

    await asyncio.to_thread(marks.sweep, marks.office_scope(), "a previous life of this office")
    await asyncio.to_thread(stages.recover, conn, config)

    app.state.config = config
    app.state.db = conn
    app.state.broadcaster = Broadcaster()

    bus = Bus(conn, config)
    app.state.bus = bus

    loop = asyncio.get_running_loop()
    broadcaster = app.state.broadcaster

    def _notify(kind: str, payload: dict[str, Any]) -> None:
        asyncio.run_coroutine_threadsafe(broadcaster.publish(kind, payload), loop)
        bus.notify(kind)

    core.set_notifier(_notify)
    marks.set_survivor_hook(lambda text: core.record_notice(conn, "info", text))
    mcp.set_drain_hook(bus.piggyback)
    mcp.set_stop_hook(bus.stop_agent)
    mcp.set_compact_hook(bus.compact)
    mcp.set_fire_hook(bus.fire_agent)

    async with mcp.session_manager.run():
        bus.recover_after_restart()
        bus.start()

        yield

        bus.stop()
        await asyncio.to_thread(stages.shutdown)

    core.set_notifier(None)
    marks.set_survivor_hook(None)
    mcp.set_drain_hook(None)
    mcp.set_stop_hook(None)
    mcp.set_compact_hook(None)
    mcp.set_fire_hook(None)
    conn.close()


#: The names the office answers to. It listens on 127.0.0.1 only.
_LOOPBACK = frozenset({"127.0.0.1", "localhost"})

#: Methods that change nothing.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _refusal(scope) -> str | None:
    """Why a request is refused, or None: it is addressed to another name, or it
    changes something and a page of another origin sent it."""
    headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
    host = headers.get("host", "")
    if urlsplit(f"//{host}").hostname not in _LOOPBACK:
        return "The office answers only at 127.0.0.1 or localhost. Open it at one of those."
    origin = headers.get("origin")
    if scope["method"] not in _SAFE_METHODS and origin is not None and origin != f"http://{host}":
        return "Refused: a page of another site sent this request."
    return None


class _OnlyThisMachine:
    """Refuses what _refusal refuses, before any route, mount or stream sees it."""

    def __init__(self, inner) -> None:
        self.inner = inner

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            refusal = _refusal(scope)
            if refusal is not None:
                await PlainTextResponse(refusal, status_code=403)(scope, receive, send)
                return
        await self.inner(scope, receive, send)


app = FastAPI(title="Office.AI", lifespan=lifespan)

app.add_middleware(_OnlyThisMachine)
app.include_router(web_router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "web" / "static")), name="static")

app.mount("/mcp/{agent}", mcp.session_manager.handle_request)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Whether the office can work, and if not, what is missing."""
    conn: sqlite3.Connection = request.app.state.db
    config = request.app.state.config
    bus: Bus = request.app.state.bus

    def _collect() -> dict[str, Any]:
        try:
            db.query_one(conn, "SELECT 1")
            db_ok = True
        except sqlite3.Error:
            db_ok = False
        state = git.project_state(config)
        return {
            "status": "ok" if db_ok and state == "ready" else "error",
            "db": db_ok,
            "project_state": state,
            "warnings": bus.health(),
        }

    return await asyncio.to_thread(_collect)


@app.get("/events/stream")
async def events_stream(request: Request) -> StreamingResponse:
    broadcaster: Broadcaster = request.app.state.broadcaster
    queue = broadcaster.subscribe()

    async def event_source():
        try:
            yield ": connected\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
                    continue
                yield message
        finally:
            broadcaster.unsubscribe(queue)

    return StreamingResponse(event_source(), media_type="text/event-stream")


if __name__ == "__main__":
    import uvicorn

    _config = load_config()
    uvicorn.run(
        "office.hub:app", host="127.0.0.1", port=_config.port, timeout_graceful_shutdown=5
    )
