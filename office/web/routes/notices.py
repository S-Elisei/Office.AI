"""The owner's journal of system notices."""

from __future__ import annotations

from fastapi import APIRouter, Request

from office import core
from office.web.deps import get_db
from office.web.templating import templates

router = APIRouter()


def _context(request: Request) -> dict:
    return {
        "request": request,
        "active": "notices",
        "notices": core.list_notices(get_db(request)),
    }


@router.get("/notices")
def notices_page(request: Request):
    return templates.TemplateResponse(request, "notices.html", _context(request))


@router.get("/fragments/notices")
def notices_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/notices_list.html", _context(request))
