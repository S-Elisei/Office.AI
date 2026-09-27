"""Works monitor."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool

from office import core, db, git
from office.adapters import adapter_for
from office.web import transcript
from office.web.deps import get_bus, get_config, get_db, get_owner_name
from office.web.templating import templates

router = APIRouter()


def _works(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every work still on the books."""
    return db.query(
        conn,
        """
        SELECT works.*, agents.name AS agent_name, agents.status AS agent_status,
               agents.runtime AS agent_runtime, agents.model AS agent_model,
               agents.context_used AS context_used, agents.context_limit AS context_limit,
               assigner.name AS assigner_name, tasks.title AS task_title
        FROM works
        JOIN agents ON agents.id = works.agent_id
        JOIN agents assigner ON assigner.id = works.assigned_by_agent_id
        LEFT JOIN tasks ON tasks.id = works.task_id
        ORDER BY works.id DESC
        """,
    )


def _derived_status(work: sqlite3.Row, live: dict) -> str:
    """What this work is, as opposed to what its row says."""
    if work["status"] in ("running", "done") and work["agent_name"] in live:
        return "running"
    if work["status"] == "done":
        return "reported"
    if work["status"] == "running":
        return "waiting"
    return work["status"]


def _context(request: Request, note: str | None = None) -> dict:
    conn = get_db(request)
    live = get_bus(request).live_turns()
    return {
        "request": request,
        "active": "monitor",
        "works": [
            dict(row) | {
                "derived_status": _derived_status(row, live),
                "told": core.work_notice_recipient(conn, row["id"]) or get_owner_name(conn),
            }
            for row in _works(conn)
        ],
        "live": live,
        "project_state": git.project_state(get_config(request)),
        "notice_ok": note,
    }


def _wake_context(request: Request) -> dict:
    """The deferred messages the office is holding."""
    return {
        "request": request,
        "wakes": core.scheduled_messages(get_db(request)),
    }


@router.get("/monitor")
def monitor_page(request: Request):
    return templates.TemplateResponse(request, "monitor.html", _context(request) | _wake_context(request))


@router.get("/fragments/monitor")
def monitor_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/monitor_list.html", _context(request))


@router.get("/fragments/monitor/wakes")
def monitor_wakes_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/monitor_wakes.html", _wake_context(request))


@router.post("/monitor/wakes/{wake_id}/cancel")
def cancel_wake(request: Request, wake_id: int):
    """Drops one pending wake before it fires."""
    conn = get_db(request)
    core.drop_scheduled_message(conn, wake_id, actor=get_owner_name(conn))
    return templates.TemplateResponse(
        request, "partials/monitor_wakes.html", _wake_context(request)
    )


def _tail(request: Request, rows: list[dict] | None = None, note: str | None = None):
    return templates.TemplateResponse(
        request, "partials/monitor_tail.html", {"request": request, "rows": rows or [], "note": note}
    )


@router.get("/fragments/monitor/tail/{work_id}")
def monitor_tail_fragment(request: Request, work_id: int):
    """What this work's assignee last wrote, parsed into a transcript."""
    conn = get_db(request)
    config = get_config(request)
    work = db.query_one(
        conn,
        """
        SELECT works.output_tail AS output_tail, agents.name AS agent_name,
               agents.runtime AS agent_runtime
        FROM works JOIN agents ON agents.id = works.agent_id
        WHERE works.id = ?
        """,
        (work_id,),
    )
    if work is None:
        return _tail(request, note="This work is gone — it was closed or written off.")

    live = False
    if work["output_tail"]:
        lines = work["output_tail"].splitlines()
    else:
        lines = get_bus(request).turn_tail(work["agent_name"])
        live = lines is not None
        if not live:
            try:
                lines = (config.tails_dir / f"{work['agent_name']}.log").read_text(
                    encoding="utf-8", errors="replace"
                ).splitlines()
            except OSError:
                lines = []
    if not lines:
        return _tail(
            request,
            note=(
                "This turn has not printed anything yet."
                if live
                else "Nothing captured — no turn has left output behind."
            ),
        )

    try:
        adapter = adapter_for(work["agent_runtime"], config)
    except RuntimeError as exc:
        return _tail(request, note=f"Cannot parse this stream: {exc}")
    return _tail(request, rows=transcript.rows(lines, adapter))


@router.post("/monitor/stop/{agent_name}")
async def stop_agent(request: Request, agent_name: str):
    """Kills one agent's running turn and fails its work as 'killed'."""
    bus = get_bus(request)
    result = await run_in_threadpool(bus.stop_agent, agent_name, get_owner_name(get_db(request)))
    note = f"Stopped {agent_name}." if result["ok"] else None
    return templates.TemplateResponse(
        request, "partials/monitor_list.html", _context(request, note=note)
    )
