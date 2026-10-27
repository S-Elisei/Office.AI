"""Pull requests: open ones only."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import Response

from office import core, db
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()


def _prs(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.query(
        conn,
        """
        SELECT prs.*, agents.name AS author_name,
               (SELECT COUNT(*) FROM pr_comments WHERE pr_comments.pr_id = prs.id) AS comment_count
        FROM prs
        JOIN agents ON agents.id = prs.author_agent_id
        WHERE prs.status = 'open'
        ORDER BY prs.id DESC
        """,
    )


def _context(request: Request) -> dict:
    conn = get_db(request)
    return {
        "request": request,
        "active": "prs",
        "prs": _prs(conn),
    }


@router.get("/prs")
def prs_page(request: Request):
    return templates.TemplateResponse(request, "prs.html", _context(request))


@router.post("/prs/{pr_id}/comments")
async def comment_pr(request: Request, pr_id: int):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    try:
        core.comment_pr(conn, pr_id, get_owner_name(conn), body)
    except ValueError:
        pass
    return Response(status_code=204)
