"""Desktop cursors and the Works connection."""

import asyncio
import json
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from office import db, monitor
from office.web import threads
from office.web.templating import templates

router = APIRouter()


@router.get("/live/transcript.js")
def transcript_script():
    return FileResponse(Path(__file__).parents[1] / "mobile" / "assets" / "transcript.js")


@router.get("/live/thread/{kind}/{peer}")
def thread_page(request: Request, kind: Literal["chat", "dm", "ticket", "pr"], peer: str, after: int = 0, before: int | None = None):
    return threads.page(request, kind, peer, after, before)


@router.get("/live/prs")
def prs(request: Request, known: str = ""):
    from office.web.routes.prs import _context
    context = _context(request)
    ids = [int(value) for value in known.split(",") if value]
    return {"prs": [{"id": p["id"], "display": templates.get_template("partials/pr_display.html").render(p=p),
                     **({"shell": templates.get_template("partials/pr_card.html").render(**context, p=p)} if p["id"] not in ids else {})} for p in context["prs"]]}


@router.websocket("/live/works")
async def works(websocket: WebSocket):
    host = websocket.headers["host"]
    if urlsplit("//" + host).hostname not in {"localhost", "127.0.0.1"} or websocket.headers.get("origin") != "http://" + host:
        await websocket.close(code=1008)
        return
    await websocket.accept()
    state = websocket.app.state
    queue = state.broadcaster.subscribe()
    receive = asyncio.create_task(websocket.receive_json())
    update = asyncio.create_task(queue.get())
    previous = {}
    rendered = {}
    outputs = []
    transcripts = {}
    due = None
    refresh_at = 0.0
    project_state = None
    initial = True
    agent_ids = set()
    task_ids = set()
    fields = ("head", "live", "context", "brief", "reason", "calls")

    async def refresh():
        nonlocal previous, refresh_at, due, project_state, agent_ids, task_ids, initial, outputs
        snapshot = await asyncio.to_thread(monitor.snapshot, state.db, state.config, state.bus)
        clocks = monitor.new_clock_activity(previous.values(), snapshot["works"])
        current = {w["id"]: monitor.work_state(w) for w in snapshot["works"]}
        changes = {"removed": [key for key in previous if key not in current], "added": [], "changed": []}
        for key, work in current.items():
            if previous.get(key) == work:
                continue
            html = {field: templates.get_template("partials/monitor_field.html").render(w=work, turn=work["turn"], field=field) for field in fields}
            changed = {field: value for field, value in html.items() if rendered.get(key, {}).get(field) != value}
            if key not in previous or changed:
                changes["added" if key not in previous else "changed"].append({"id": key, "fields": changed})
            rendered[key] = html
        for key in changes["removed"]:
            rendered.pop(key)
        changes = {key: value for key, value in changes.items() if value}
        if list(previous) != list(current):
            changes["order"] = list(current)
        if project_state != snapshot["project_state"]:
            changes["project_state"] = snapshot["project_state"]
            project_state = snapshot["project_state"]
        if initial:
            changes["activity"] = clocks
            initial = False
        else:
            for name, at in clocks.items():
                await websocket.send_json({"event": "activity", "data": {"subject_type": "agent", "name": name, "at": at}})
        if changes:
            await websocket.send_json({"event": "works", "server_time": time.time(), "data": changes})
        previous = current
        targets = db.query(state.db, "SELECT agent_id, task_id FROM works")
        agent_ids = {row["agent_id"] for row in targets}
        task_ids = {row["task_id"] for row in targets if row["task_id"] is not None}
        outputs = [key for key in outputs if key in current]
        for key in list(transcripts):
            if key not in outputs:
                del transcripts[key]
        await send_outputs()
        refresh_at = asyncio.get_running_loop().time() + 10
        due = None

    async def send_outputs():
        for key in outputs:
            output = await asyncio.to_thread(monitor.output, state.db, state.config, state.bus, key)
            change = monitor.output_delta(transcripts.get(key), output)
            if change:
                await websocket.send_json({"event": "output", "id": key, "server_time": time.time(), "data": change})
            transcripts[key] = output

    try:
        await refresh()
        while True:
            deadline = min(refresh_at, due) if due is not None else refresh_at
            done, _ = await asyncio.wait({receive, update}, timeout=max(0, deadline - asyncio.get_running_loop().time()), return_when=asyncio.FIRST_COMPLETED)
            received = receive in done
            if received:
                outputs = receive.result()["outputs"]
                transcripts = {key: value for key, value in transcripts.items() if key in outputs}
                receive = asyncio.create_task(websocket.receive_json())
            if update in done:
                kind, encoded = update.result().split("\ndata: ", 1)
                update = asyncio.create_task(queue.get())
                payload = json.loads(encoded)
                concerns = kind == "event: works" or (
                    kind == "event: agents" and payload["subject_type"] == "agent" and payload["subject_id"] in agent_ids
                ) or (kind == "event: tasks" and payload["subject_id"] in task_ids)
                if concerns and due is None:
                    due = asyncio.get_running_loop().time() + 0.1
            if asyncio.get_running_loop().time() >= (min(refresh_at, due) if due is not None else refresh_at):
                await refresh()
            elif received:
                await send_outputs()
    except WebSocketDisconnect:
        pass
    finally:
        state.broadcaster.unsubscribe(queue)
        for task in (receive, update):
            task.cancel()
            with suppress(asyncio.CancelledError, WebSocketDisconnect):
                await task
