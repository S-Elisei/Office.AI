"""Works monitor."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from fastapi.concurrency import run_in_threadpool

from office import core
from office.web.deps import get_bus, get_db, get_owner_name
from office.web.routes.main import quota_context
from office.web.templating import templates

router = APIRouter()


def _wake_context(request: Request) -> dict:
    """The deferred messages the office is holding."""
    return {
        "request": request,
        "wakes": core.scheduled_messages(get_db(request)),
    }


@router.get("/monitor")
def monitor_page(request: Request):
    return templates.TemplateResponse(
        request, "monitor.html", {"active": "monitor", **_wake_context(request), **quota_context(request)}
    )


@router.get("/fragments/monitor/wakes")
def monitor_wakes_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/monitor_wakes.html", _wake_context(request))


@router.post("/monitor/wakes/{wake_id}/cancel")
def cancel_wake(request: Request, wake_id: int):
    """Drops one pending wake before it fires."""
    conn = get_db(request)
    core.cancel_scheduled_message(conn, wake_id, sender=None, actor=get_owner_name(conn))
    return templates.TemplateResponse(
        request, "partials/monitor_wakes.html", _wake_context(request)
    )


@router.post("/monitor/stop/{agent_name}")
async def stop_agent(request: Request, agent_name: str):
    """Kills one agent's running turn and fails its work as 'killed'."""
    bus = get_bus(request)
    result = await run_in_threadpool(bus.stop_agent, agent_name, get_owner_name(get_db(request)))
    if not result["ok"]:
        raise HTTPException(409, result["reason"])
    return Response(status_code=204)
