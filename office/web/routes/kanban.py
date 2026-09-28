from __future__ import annotations

import sqlite3
from urllib.parse import urlencode

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
    ("cancelled", "Cancelled"),
]
NEW_TASK_STATUS = "idea"
DONE_SHOWN = 20


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


def _view_query(parent: int | None, flat: bool, all_done: bool) -> str:
    """The query string of one view of the board."""
    pairs = {"parent": parent, "flat": 1 if flat else None, "all_done": 1 if all_done else None}
    return urlencode({k: v for k, v in pairs.items() if v})


def _context(
    request: Request, parent: int | None = None, flat: bool = False, all_done: bool = False, **extra
) -> dict:
    """The board of `parent`'s children, of the top-level tasks when `parent` is
    None, or of every task when `flat`. The done and cancelled columns hold
    the DONE_SHOWN most recently changed ones each unless `all_done`."""
    conn = get_db(request)
    tasks = core.board(conn)
    if flat:
        shown = list(tasks.values())
    elif parent is None:
        shown = [t for t in tasks.values() if t["parent_task_id"] is None]
    else:
        shown = [tasks[c] for c in tasks[parent]["children"]]
    by_status: dict[str, list[dict]] = {}
    for t in shown:
        by_status.setdefault(t["status"], []).append(t)
    closed_totals = {}
    for status in core.CLOSED_TASK_STATUSES:
        closed = sorted(by_status.pop(status, []), key=lambda t: t["updated_at"], reverse=True)
        by_status[status] = closed if all_done else closed[:DONE_SHOWN]
        closed_totals[status] = len(closed)
    return {
        "request": request,
        "active": "kanban",
        "columns": COLUMNS,
        "by_status": by_status,
        "closed_totals": closed_totals,
        "done_shown": DONE_SHOWN,
        "parent": parent,
        "flat": flat,
        "all_done": all_done,
        "chain": [] if parent is None else core.task_chain(tasks, parent)[::-1],
        "view_query": _view_query(parent, flat, all_done),
        "all_done_query": _view_query(parent, flat, not all_done),
        "signals": _signals(conn),
        **extra,
    }


@router.get("/kanban")
def kanban_page(request: Request, parent: int | None = None, flat: bool = False, all_done: bool = False):
    return templates.TemplateResponse(request, "kanban.html", _context(request, parent, flat, all_done))


@router.get("/fragments/kanban")
def kanban_fragment(request: Request, parent: int | None = None, flat: bool = False, all_done: bool = False):
    return templates.TemplateResponse(
        request, "partials/kanban_board.html", _context(request, parent, flat, all_done)
    )


@router.get("/fragments/kanban/task/{task_id}")
def task_detail_fragment(
    request: Request, task_id: int, parent: int | None = None, flat: bool = False, all_done: bool = False
):
    return templates.TemplateResponse(
        request, "partials/kanban_task.html",
        {"request": request, **core.get_task(get_db(request), task_id),
         "view_query": _view_query(parent, flat, all_done)},
    )


@router.post("/kanban/tasks")
async def create_task(request: Request, parent: int | None = None, flat: bool = False, all_done: bool = False):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    body = data.get("body", "").strip() or None
    under = int(data["parent"]) if data.get("parent", "").strip() else None
    try:
        core.create_task(conn, title, body, status=NEW_TASK_STATUS, parent=under)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "partials/kanban_board.html",
            _context(request, parent, flat, all_done, notice_error=str(exc), form=data), status_code=400,
        )
    return templates.TemplateResponse(
        request, "partials/kanban_board.html", _context(request, parent, flat, all_done)
    )


@router.post("/kanban/tasks/{task_id}")
async def edit_task(request: Request, task_id: int, parent: int | None = None, flat: bool = False, all_done: bool = False):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip() or None
    body = data["body"].strip() or None
    core.update_task(conn, task_id, title=title, body=body)
    return templates.TemplateResponse(
        request, "partials/kanban_board.html", _context(request, parent, flat, all_done)
    )
