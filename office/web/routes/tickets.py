"""Tickets: questions/bugs with an addressee and a closing authority."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from office import core, db
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()


def _open_tickets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.query(conn, "SELECT * FROM tickets WHERE status = 'open' ORDER BY id ASC")


def _resolved_tickets(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.query(
        conn,
        "SELECT * FROM tickets WHERE status = 'resolved' ORDER BY resolved_at DESC, id DESC",
    )


def _agent_names(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in db.query(conn, "SELECT name FROM agents ORDER BY name COLLATE NOCASE")]


def _list_context(request: Request) -> dict:
    conn = get_db(request)
    return {
        "request": request,
        "active": "tickets",
        "open_tickets": _open_tickets(conn),
        "resolved_tickets": _resolved_tickets(conn),
        "owner_name": get_owner_name(conn),
        "agent_names": _agent_names(conn),
    }


def _ticket(conn: sqlite3.Connection, ticket_id: int) -> sqlite3.Row:
    return db.query_one(conn, "SELECT * FROM tickets WHERE id = ?", (ticket_id,))


def _comments(conn: sqlite3.Connection, ticket_id: int) -> list[sqlite3.Row]:
    return db.query(
        conn, "SELECT * FROM ticket_comments WHERE ticket_id = ? ORDER BY id ASC", (ticket_id,)
    )


def _detail_context(request: Request, ticket_id: int) -> dict:
    conn = get_db(request)
    ticket = _ticket(conn, ticket_id)
    owner_name = get_owner_name(conn)
    is_open = ticket["status"] == "open"
    addressed_to_owner = ticket["addressee"] == owner_name
    return {
        "request": request,
        "active": "tickets",
        "ticket_id": ticket_id,
        "ticket": ticket,
        "comments": _comments(conn, ticket_id),
        "owner_name": owner_name,
        "can_resolve": is_open and addressed_to_owner,
        "can_override_resolve": is_open and not addressed_to_owner,
    }


@router.get("/tickets")
def tickets_page(request: Request):
    return templates.TemplateResponse(request, "tickets.html", _list_context(request))


@router.get("/fragments/tickets")
def tickets_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/tickets_list.html", _list_context(request))


@router.post("/tickets")
async def create_ticket(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    body = data.get("body", "").strip() or None
    kind = data.get("kind", "question")
    addressee = data.get("addressee", "").strip()
    core.create_ticket(
        conn, title=title, body=body, author=get_owner_name(conn), addressee=addressee, kind=kind
    )
    return templates.TemplateResponse(request, "partials/tickets_list.html", _list_context(request))


@router.get("/tickets/{ticket_id}")
def ticket_detail_page(request: Request, ticket_id: int):
    return templates.TemplateResponse(request, "ticket_detail.html", _detail_context(request, ticket_id))


@router.get("/fragments/tickets/{ticket_id}")
def ticket_detail_fragment(request: Request, ticket_id: int):
    return templates.TemplateResponse(
        request, "partials/ticket_detail.html", _detail_context(request, ticket_id)
    )


@router.post("/tickets/{ticket_id}/comments")
async def comment_ticket(request: Request, ticket_id: int):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    core.comment_ticket(conn, ticket_id, get_owner_name(conn), body)
    return templates.TemplateResponse(request, "partials/ticket_detail.html", _detail_context(request, ticket_id))


@router.post("/tickets/{ticket_id}/resolve")
async def resolve_ticket(request: Request, ticket_id: int):
    conn = get_db(request)
    data = await read_form(request)
    resolution = data.get("resolution", "").strip()
    try:
        core.resolve_ticket(conn, ticket_id, resolved_by=get_owner_name(conn), resolution=resolution)
    except ValueError:
        pass
    return templates.TemplateResponse(request, "partials/ticket_detail.html", _detail_context(request, ticket_id))
