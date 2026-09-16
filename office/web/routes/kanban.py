from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from office import core, db
from office.web.deps import get_db, read_form
from office.web.templating import templates

router = APIRouter()

COLUMNS = [
    ("idea", "Idea"),
    ("planned", "Planned"),
    ("needs_clarification", "Needs clarification"),
    ("in_progress", "In progress"),
    ("paused", "Paused"),
    ("done", "Done"),
]
NEW_TASK_STATUS = "idea"


def _by_status(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    rows = db.query(conn, "SELECT * FROM tasks ORDER BY status, position, created_at")
    by_status: dict[str, list[sqlite3.Row]] = {}
    for r in rows:
        by_status.setdefault(r["status"], []).append(r)
    return by_status


def _signals(conn: sqlite3.Connection) -> dict[int, dict]:
    """Per task: the works on it and the tickets pointing at it."""
    works: dict[int, list[dict]] = {}
    for r in db.query(
        conn,
        "SELECT w.task_id, a.name AS agent, w.status FROM works w "
        "JOIN agents a ON a.id = w.agent_id WHERE w.task_id IS NOT NULL ORDER BY w.id",
    ):
        works.setdefault(r["task_id"], []).append({"agent": r["agent"], "status": r["status"]})
    tickets: dict[int, list[dict]] = {}
    for r in db.query(
        conn,
        "SELECT task_id, id, status, addressee FROM tickets WHERE task_id IS NOT NULL ORDER BY id",
    ):
        tickets.setdefault(r["task_id"], []).append(
            {"id": r["id"], "status": r["status"], "addressee": r["addressee"]}
        )
    return {"works": works, "tickets": tickets}


def _blocker_ids(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """Task id -> ids of its not-yet-done blockers."""
    out: dict[int, list[int]] = {}
    for r in db.query(
        conn,
        "SELECT DISTINCT td.blocked_task_id AS task_id, blocker.id AS blocker_id "
        "FROM task_dependencies td "
        "JOIN tasks t ON t.id = td.blocked_task_id "
        "JOIN tasks blocker ON blocker.id = td.blocking_task_id "
        "WHERE t.status != 'done' AND blocker.status != 'done' ORDER BY blocker.id",
    ):
        out.setdefault(r["task_id"], []).append(r["blocker_id"])
    return out


def _context(request: Request) -> dict:
    conn = get_db(request)
    return {
        "request": request,
        "active": "kanban",
        "columns": COLUMNS,
        "by_status": _by_status(conn),
        "blocker_ids": _blocker_ids(conn),
        "signals": _signals(conn),
    }


@router.get("/kanban")
def kanban_page(request: Request):
    return templates.TemplateResponse(request, "kanban.html", _context(request))


@router.get("/fragments/kanban")
def kanban_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/kanban_board.html", _context(request))


def _task_detail(conn: sqlite3.Connection, task_id: int) -> dict:
    task = db.query_one(conn, "SELECT * FROM tasks WHERE id = ?", (task_id,))
    works = db.query(
        conn,
        "SELECT w.id, a.name AS agent, w.status, w.branch, w.brief, w.fail_reason, w.pause_reason "
        "FROM works w JOIN agents a ON a.id = w.agent_id WHERE w.task_id = ? ORDER BY w.id",
        (task_id,),
    )
    tickets = db.query(
        conn,
        "SELECT id, title, kind, status, addressee, author FROM tickets WHERE task_id = ? ORDER BY id",
        (task_id,),
    )
    blocked_by = db.query(
        conn,
        "SELECT t.id, t.title, t.status FROM task_dependencies td "
        "JOIN tasks t ON t.id = td.blocking_task_id WHERE td.blocked_task_id = ? ORDER BY t.id",
        (task_id,),
    )
    blocks = db.query(
        conn,
        "SELECT t.id, t.title, t.status FROM task_dependencies td "
        "JOIN tasks t ON t.id = td.blocked_task_id WHERE td.blocking_task_id = ? ORDER BY t.id",
        (task_id,),
    )
    return {
        "task": task, "works": works, "tickets": tickets,
        "blocked_by": blocked_by, "blocks": blocks,
    }


@router.get("/fragments/kanban/task/{task_id}")
def task_detail_fragment(request: Request, task_id: int):
    ctx = _task_detail(get_db(request), task_id)
    return templates.TemplateResponse(
        request, "partials/kanban_task.html", {"request": request, **ctx}
    )


@router.post("/kanban/tasks")
async def create_task(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    body = data.get("body", "").strip() or None
    core.create_task(conn, title, body, status=NEW_TASK_STATUS)
    return templates.TemplateResponse(request, "partials/kanban_board.html", _context(request))


@router.post("/kanban/tasks/{task_id}")
async def edit_task(request: Request, task_id: int):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip() or None
    body = data["body"].strip() or None
    core.update_task(conn, task_id, title=title, body=body)
    return templates.TemplateResponse(request, "partials/kanban_board.html", _context(request))
