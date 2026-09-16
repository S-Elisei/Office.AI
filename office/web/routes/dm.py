"""Direct messages: thread list + thread view."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from office import core, db
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

PAGE_SIZE = 200


def _agent_names(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in db.query(conn, "SELECT name FROM agents ORDER BY name COLLATE NOCASE")]


def _threads(conn: sqlite3.Connection, owner_name: str) -> list[dict]:
    """Every peer the owner has ever exchanged a DM with, newest thread first."""
    rows = db.query(
        conn,
        """
        SELECT peer, MAX(id) AS id, body, created_at FROM (
            SELECT id, body, created_at,
                   CASE WHEN sender = ? THEN recipient ELSE sender END AS peer
            FROM messages
            WHERE channel = 'dm' AND (sender = ? OR recipient = ?)
        )
        WHERE peer IS NOT NULL AND peer <> ?
        GROUP BY peer
        ORDER BY id DESC
        """,
        (owner_name, owner_name, owner_name, owner_name),
    )
    return [{"peer": r["peer"], "preview": r["body"], "created_at": r["created_at"]} for r in rows]


def _thread_messages(
    conn: sqlite3.Connection,
    peer: str,
    owner_name: str,
    before_id: int | None = None,
    limit: int = PAGE_SIZE,
) -> tuple[list[sqlite3.Row], bool]:
    """One page of one thread, oldest-first for display.

    Returns the rows and whether anything older exists.

    The inner query takes the page by id and the outer one puts it back in reading order.
    """
    where = (
        "channel = 'dm' AND ((sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?))"
    )
    params: list = [peer, owner_name, owner_name, peer]
    if before_id is not None:
        where += " AND id < ?"
        params.append(before_id)
    params.append(limit + 1)
    rows = db.query(
        conn,
        f"SELECT * FROM (SELECT * FROM messages WHERE {where} ORDER BY id DESC LIMIT ?) ORDER BY id ASC",
        tuple(params),
    )
    has_older = len(rows) > limit
    return (rows[1:] if has_older else rows), has_older


def _context(request: Request, peer: str | None, before_id: int | None = None) -> dict:
    conn = get_db(request)
    owner_name = get_owner_name(conn)
    messages, has_older = (
        _thread_messages(conn, peer, owner_name, before_id) if peer else ([], False)
    )
    return {
        "request": request,
        "active": "dm",
        "threads": _threads(conn, owner_name),
        "peer": peer,
        "owner_name": owner_name,
        "agent_names": _agent_names(conn),
        "messages": messages,
        "before_id": before_id,
        "has_older": has_older,
        "oldest_id": messages[0]["id"] if messages else None,
        "conv_channel": "dm",
        "conv_peer": peer or "",
        "newest_id": messages[-1]["id"] if messages else None,
        "page_url": f"/fragments/dm-thread/{peer}" if peer else "",
        "region_id": "dm-thread",
    }


@router.get("/dm")
def dm_page(request: Request):
    return templates.TemplateResponse(request, "dm.html", _context(request, None))


@router.get("/dm/open")
def dm_open(peer: str):
    """Plain GET-form target for "start a new conversation"."""
    return RedirectResponse(f"/dm/{peer}", status_code=303)


@router.get("/dm/{peer}")
def dm_thread_page(request: Request, peer: str):
    return templates.TemplateResponse(request, "dm.html", _context(request, peer))


@router.get("/fragments/dm-threads")
def dm_threads_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/dm_thread_list.html", _context(request, None))


@router.get("/fragments/dm-thread/{peer}")
def dm_thread_fragment(request: Request, peer: str, before_id: int | None = None):
    """The thread panel, wrapper included."""
    return templates.TemplateResponse(
        request, "partials/dm_thread_panel.html", _context(request, peer, before_id)
    )


@router.post("/dm/{peer}/messages")
async def send_dm(request: Request, peer: str):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    owner_name = get_owner_name(conn)
    try:
        core.send_message(conn, sender=owner_name, recipient=peer, body=body, actor=owner_name)
    except ValueError:
        pass
    return templates.TemplateResponse(request, "partials/dm_thread_panel.html", _context(request, peer))
