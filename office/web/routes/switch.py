"""The office's run/pause switch and its one-shot timer, in the header of every page."""

from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter, Request

from office.web.deps import get_bus, get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

PARTIAL = "partials/office_switch.html"


@router.get("/fragments/office-switch")
def office_switch_fragment(request: Request):
    return templates.TemplateResponse(request, PARTIAL, {})


@router.post("/office/switch")
async def set_switch(request: Request):
    """Set the office running or paused. `to` is "run" or "pause"."""
    data = await read_form(request)
    get_bus(request).set_running(
        data["to"] == "run", "by hand", actor=get_owner_name(get_db(request))
    )
    return templates.TemplateResponse(request, PARTIAL, {})


@router.post("/office/timer")
async def set_timer(request: Request):
    """Set the one-shot timer, replacing a pending one: `at` is a local time of day,
    HH:MM, and the timer fires at its next occurrence; `to` is "run" or "pause"."""
    data = await read_form(request)
    hour, minute = map(int, data["at"].split(":"))
    now = datetime.now().astimezone()
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    get_bus(request).set_switch_timer(data["to"], int(at.timestamp()), get_owner_name(get_db(request)))
    return templates.TemplateResponse(request, PARTIAL, {})


@router.post("/office/timer/cancel")
def cancel_timer(request: Request):
    get_bus(request).cancel_switch_timer(get_owner_name(get_db(request)))
    return templates.TemplateResponse(request, PARTIAL, {})
