
from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from office import core, db
from office.web.deps import get_db

router = APIRouter()

#: name -> (SQL for the count, the SSE kinds that can change it).
#:
#: A statement here may bind the owner's own name and nothing else, once for
#: every `?` it writes -- see _count().
COUNTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "tickets": ("SELECT COUNT(*) AS n FROM tickets WHERE status = 'open'", ("tickets",)),
    "prs": ("SELECT COUNT(*) AS n FROM prs WHERE status = 'open'", ("prs",)),
    "works": ("SELECT COUNT(*) AS n FROM works", ("works",)),
    "dm": (
        """
        SELECT COUNT(*) AS n
          FROM messages m
          LEFT JOIN owner_reading r ON r.channel = 'dm' AND r.peer = m.sender
         WHERE m.channel = 'dm' AND m.recipient = ? AND m.sender <> ?
           AND m.id > COALESCE(r.seen_message_id, 0)
        """,
        ("messages",),
    ),
    "chat": (
        """
        SELECT COUNT(*) AS n
          FROM messages
         WHERE channel = 'all' AND recipient IS NULL AND sender <> ?
           AND id > COALESCE(
               (SELECT seen_message_id FROM owner_reading WHERE channel = 'all' AND peer = ''), 0)
        """,
        ("messages",),
    ),
}


def nav_counts(request: Request) -> dict[str, dict]:
    """Every counter for a full page render: its number and the trigger that
    refreshes it."""
    conn = get_db(request)
    owner = core.get_owner_name(conn)
    return {
        name: {"value": _count(conn, name, owner), "trigger": nav_trigger(name)}
        for name in COUNTS
    }


def nav_trigger(name: str) -> str:
    """One region's hx-trigger, built from the kinds COUNTS declares for it."""
    _sql, kinds = COUNTS[name]
    return ", ".join(f"office:{kind} from:body" for kind in kinds)


def _count(conn: sqlite3.Connection, name: str, owner: str) -> int:
    """One counter's number."""
    sql, _kinds = COUNTS[name]
    return int(db.query_one(conn, sql, (owner,) * sql.count("?"))["n"])


@router.get("/fragments/nav-count/{name}", response_class=HTMLResponse)
def nav_count_fragment(request: Request, name: str):
    """One number, as bare text."""
    conn = get_db(request)
    value = _count(conn, name, core.get_owner_name(conn))
    return HTMLResponse(str(value) if value else "")
