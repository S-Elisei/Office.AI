"""Rules: the flat catalog of standing statements, each for an audience."""

from __future__ import annotations

from fastapi import APIRouter, Request

from office import core, db
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()


def _rules_context(request: Request) -> dict:
    conn = get_db(request)
    return {
        "request": request,
        "active": "rules",
        "rules": db.query(conn, "SELECT * FROM rules ORDER BY id DESC"),
        "audiences": core.rule_audiences(conn),
    }


@router.get("/rules")
def rules_page(request: Request):
    return templates.TemplateResponse(request, "rules.html", _rules_context(request))


@router.get("/fragments/rules")
def rules_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/rules_list.html", _rules_context(request))


@router.post("/rules")
async def create_rule(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    text = data.get("text", "").strip()
    core.create_rule(conn, text, get_owner_name(conn), title=title, audience=data["audience"])
    return templates.TemplateResponse(request, "partials/rules_list.html", _rules_context(request))


@router.post("/rules/{rule_id}")
async def update_rule(request: Request, rule_id: int):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    text = data.get("text", "").strip()
    core.update_rule(conn, rule_id, text, title=title or None, audience=data["audience"])
    return templates.TemplateResponse(request, "partials/rules_list.html", _rules_context(request))


@router.post("/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int):
    conn = get_db(request)
    core.delete_rule(conn, rule_id)
    return templates.TemplateResponse(request, "partials/rules_list.html", _rules_context(request))
