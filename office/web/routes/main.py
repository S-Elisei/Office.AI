"""Main page: director chat and stop control, critical notices, quota, context and owner tickets."""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool

from office import core, db, git, monitor
from office.adapters import RUNTIMES, adapter_for
from office.web import threads, transcript
from office.web.deps import get_bus, get_config, get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

REGION_ID = "main-chat"


def _director(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return db.query_one(conn, "SELECT * FROM agents WHERE kind = 'director' LIMIT 1")


def _messages(
    conn: sqlite3.Connection,
    director: sqlite3.Row | None,
    owner_name: str,
    limit: int = threads.PAGE_SIZE,
) -> tuple[list[sqlite3.Row], bool]:
    """The newest page of the owner-director thread and its older-page flag."""
    if director is None:
        return [], False
    return core.dm_history(conn, director["name"], owner_name, limit=limit)


def _compact_blocker(request: Request, director: sqlite3.Row | None) -> str | None:
    """Why the Compact button is disabled right now, or None."""
    if director is None:
        return None
    return get_bus(request).compaction_blocker(director["name"])


# -- one collector per region -------------------------------------------------


def _chat_context(
    request: Request,
    *,
    error: str | None = None,
    form: dict | None = None,
) -> dict:
    """The director-chat panel: a page of the thread, or the form that has to
    come first.
    """
    conn = get_db(request)
    director = _director(conn)
    owner_name = get_owner_name(conn)
    messages, has_older = _messages(conn, director, owner_name)
    return {
        "request": request,
        "director": director,
        "owner_name": owner_name,
        "project_state": git.project_state(get_config(request)),
        "office_repo": str(git.project_git(get_config(request))),
        "owner_repo": str(git.owner_repo(get_config(request))),
        "owner_state": git.owner_state(get_config(request)),
        "messages": messages,
        "has_older": has_older,
        "conv_channel": "dm",
        "conv_peer": director["name"] if director is not None else "",
        "newest_id": messages[-1]["id"] if messages else None,
        "region_id": REGION_ID,
        "runtimes": RUNTIMES,
        "notice_error": error,
        "form": form or {},
    }


def _stop_context(request: Request, *, note: str | None = None) -> dict:
    """The stop control under the chat: the live turn, or nothing at all."""
    conn = get_db(request)
    director = _director(conn)
    turn = get_bus(request).turn_status(director["name"]) if director is not None else None
    return {
        "request": request,
        "director_turn": turn,
        "director_activity": _activity(request, director, turn),
        "notice_ok": note,
    }


def _activity(request: Request, director: sqlite3.Row | None, turn: dict | None) -> dict | None:
    """The last line of the director's own output stream, or None."""
    if director is None or turn is None:
        return None
    lines = get_bus(request).turn_tail(director["name"])
    if not lines:
        return None
    adapter = adapter_for(director["runtime"], get_config(request))
    return transcript.last_activity(lines, adapter)


def _plate_context(request: Request) -> dict:
    """The plate: two queries against a table with one row per incident."""
    notices, more = core.plate_notices(get_db(request))
    return {"request": request, "plate_notices": notices, "plate_more": more}


def quota_context(request: Request) -> dict:
    return {"request": request, "quota": monitor.quota(get_db(request))}


def _context_panel_context(request: Request, *, applied_note: str | None = None) -> dict:
    """Director context fill and the Compact button beside it."""
    director = _director(get_db(request))
    return {
        "request": request,
        "director": director,
        "compact_blocker": _compact_blocker(request, director),
        "applied_note": applied_note,
    }


def _intake_context(
    request: Request, *, note: str | None = None, error: str | None = None
) -> dict:
    """The Take-my-changes control and whatever the last press of it said."""
    config = get_config(request)
    ready = git.project_state(config) == "ready"
    return {
        "intake": {
            "repo": str(git.owner_repo(config)),
            "branch": git.default_branch(config),
        }
        if ready
        else None,
        "notice_error": error,
        "notice_ok": note,
    }


def _tickets_context(request: Request) -> dict:
    conn = get_db(request)
    return {
        "request": request,
        "tickets": db.query(
            conn,
            "SELECT * FROM tickets WHERE status = 'open' AND addressee = ? ORDER BY id ASC",
            (get_owner_name(conn),),
        ),
    }


def _page_context(request: Request) -> dict:
    """The whole page: every region's collector, merged."""
    return {
        **_tickets_context(request),
        **_intake_context(request),
        **_chat_context(request),
        **_stop_context(request),
        **_plate_context(request),
        **quota_context(request),
        **_context_panel_context(request),
        "active": "main",
    }


@router.get("/")
def main_page(request: Request):
    return templates.TemplateResponse(request, "main.html", _page_context(request))


@router.get("/fragments/main-chat")
def main_chat_fragment(request: Request):
    """The setup or director panel, wrapper included."""
    return templates.TemplateResponse(
        request, "partials/main_chat_panel.html", _chat_context(request)
    )


@router.post("/messages/main-chat")
async def send_to_director(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    director = _director(conn)
    owner_name = get_owner_name(conn)
    core.send_message(conn, sender=owner_name, recipient=director["name"], body=body, actor=owner_name)
    return Response(status_code=204)

@router.post("/project/init")
async def init_project(request: Request):
    """Build the hub's repository layout."""
    config = get_config(request)
    data = await read_form(request)
    branch = data.get("branch", "").strip()
    readme = data.get("readme", "")
    try:
        await run_in_threadpool(git.init_project, config, branch=branch, readme=readme)
    except Exception as exc:  # noqa: BLE001 - git.py raises several types; show what came back
        message = exc.output.strip() if isinstance(exc, git.GitError) else str(exc)
        ctx = _chat_context(request, error=message, form=data)
        return templates.TemplateResponse(request, "partials/main_chat_panel.html", ctx, status_code=400)
    response = templates.TemplateResponse(
        request, "partials/main_chat_panel.html", _chat_context(request)
    )
    response.headers["HX-Trigger"] = "office:project"
    return response


@router.post("/project/take")
def take_owner_changes(request: Request):
    """Take the owner's own commits into the office."""
    intake = core.take_owner_changes(get_db(request), get_config(request))
    if intake.status in ("diverged", "failed"):
        return templates.TemplateResponse(
            request, "partials/main_intake.html",
            _intake_context(request, error=intake.detail), status_code=400,
        )
    return templates.TemplateResponse(
        request, "partials/main_intake.html", _intake_context(request, note=intake.detail)
    )


@router.post("/agents/director")
async def hire_director(request: Request):
    """First-run bootstrap: the human hires the director."""
    conn = get_db(request)
    config = get_config(request)
    data = await read_form(request)

    name = data.get("name", "").strip()
    runtime = data.get("runtime", "").strip()
    model = data.get("model", "").strip()
    effort = data.get("effort", "").strip() or None
    instructions = data.get("instructions", "")
    owner_name = data.get("owner_name", "").strip()
    if owner_name != get_owner_name(conn):
        core.set_setting(conn, "owner_name", owner_name, actor=owner_name)
    try:
        await run_in_threadpool(
            core.hire,
            conn,
            config,
            name=name,
            runtime=runtime,
            model=model,
            effort=effort,
            kind="director",
            instructions=instructions,
            actor=owner_name,
        )
    except Exception as exc:  # noqa: BLE001 - git.py can raise several types; show whatever comes back
        ctx = _chat_context(request, error=str(exc), form=data)
        return templates.TemplateResponse(request, "partials/main_chat_panel.html", ctx, status_code=400)

    return templates.TemplateResponse(request, "partials/main_chat_panel.html", _chat_context(request))


@router.get("/fragments/main-stop")
def main_stop_fragment(request: Request):
    """The stop control. Renders to nothing whenever no turn is running."""
    return templates.TemplateResponse(request, "partials/main_stop.html", _stop_context(request))


@router.get("/fragments/main-activity")
def main_activity_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/main_activity.html", _stop_context(request))


@router.post("/agents/director/stop")
async def stop_director(request: Request):
    """Kill the director's running turn, on purpose."""
    conn = get_db(request)
    director = _director(conn)
    result = await run_in_threadpool(
        get_bus(request).stop_agent, director["name"], get_owner_name(conn)
    )
    note = f"Stopped {director['name']}'s turn." if result["ok"] else None
    return templates.TemplateResponse(
        request, "partials/main_stop.html", _stop_context(request, note=note)
    )


@router.get("/fragments/main-quota")
def main_quota_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/quota.html", quota_context(request))


@router.get("/fragments/main-context")
def main_context_fragment(request: Request):
    return templates.TemplateResponse(
        request, "partials/main_context.html", _context_panel_context(request)
    )


@router.get("/fragments/main-plate")
def main_plate_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/main_plate.html", _plate_context(request))


@router.post("/notices/dismiss")
def dismiss_notices(request: Request):
    """The × on the plate: the owner says he has read it."""
    conn = get_db(request)
    core.dismiss_notices(conn, actor=get_owner_name(conn))
    return templates.TemplateResponse(request, "partials/main_plate.html", _plate_context(request))


@router.post("/agents/director/compact")
async def compact_director(request: Request):
    """Manual compaction: summarize the session in place, keeping it and its id."""
    conn = get_db(request)
    result = await run_in_threadpool(
        get_bus(request).compact, _director(conn)["name"], get_owner_name(conn)
    )
    note = (
        f"Compacted: {result['before']} -> {result['after']} tokens ({result['freed']} freed)."
        if result["ok"]
        else None
    )
    return templates.TemplateResponse(
        request, "partials/main_context.html", _context_panel_context(request, applied_note=note)
    )


@router.get("/fragments/main-tickets")
def main_tickets_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/main_tickets.html", _tickets_context(request))
