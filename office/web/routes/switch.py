"""The office's run/pause switch and its one-shot timer in the left menu."""

from __future__ import annotations

from fastapi import APIRouter, Request

from office import office_switch
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
    office_switch.set_timer(get_bus(request), data["to"], data["at"], get_owner_name(get_db(request)))
    return templates.TemplateResponse(request, PARTIAL, {})


@router.post("/office/timer/cancel")
def cancel_timer(request: Request):
    get_bus(request).cancel_switch_timer(get_owner_name(get_db(request)))
    return templates.TemplateResponse(request, PARTIAL, {})
