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


def _context(request: Request, peer: str | None, before_id: int | None = None) -> dict:
    conn = get_db(request)
    owner_name = get_owner_name(conn)
    messages, has_older = (
        core.dm_history(conn, peer, owner_name, before_id, limit=PAGE_SIZE) if peer else ([], False)
    )
    return {
        "request": request,
        "active": "dm",
        "threads": core.dm_threads(conn, owner_name),
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
