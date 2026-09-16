"""Team roster: agents, runtime/model/effort, status, context fill."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request

from office import db, git
from office.web.deps import get_config, get_db
from office.web.templating import templates

router = APIRouter()


def _agents(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.query(
        conn,
        "SELECT * FROM agents ORDER BY (kind = 'director') DESC, name COLLATE NOCASE",
    )


def _context(request: Request) -> dict:
    return {
        "request": request,
        "active": "team",
        "agents": _agents(get_db(request)),
        "project_state": git.project_state(get_config(request)),
    }


@router.get("/team")
def team_page(request: Request):
    return templates.TemplateResponse(request, "team.html", _context(request))


@router.get("/fragments/team")
def team_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/team_table.html", _context(request))
