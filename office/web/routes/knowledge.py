"""Knowledge base: wiki (categorized markdown) + the flat rules catalog."""

from __future__ import annotations

import sqlite3
from itertools import groupby

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse
from fastapi.concurrency import run_in_threadpool

from office import core, db
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()


def _wiki_categories(conn: sqlite3.Connection) -> list[tuple[str, list[dict]]]:
    pages = core.list_wiki_pages(conn)
    return [(cat, list(group)) for cat, group in groupby(pages, key=lambda p: p["category"])]


def _rules(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return db.query(conn, "SELECT * FROM rules ORDER BY id DESC")


def _wiki_context(request: Request, path: str | None) -> dict:
    conn = get_db(request)
    categories = _wiki_categories(conn)
    selected_path = path
    selected = core.get_wiki_page(conn, path) if path else None
    if selected is None and categories:
        selected_path = categories[0][1][0]["path"]
        selected = core.get_wiki_page(conn, selected_path)
    return {
        "request": request,
        "active": "knowledge",
        "page_path_pattern": core.PAGE_PATH.pattern,
        "categories": categories,
        "selected": selected,
        "selected_path": selected_path,
        "owner_name": get_owner_name(conn),
    }


def _rules_context(request: Request) -> dict:
    return {"request": request, "rules": _rules(get_db(request))}


@router.get("/knowledge")
def knowledge_page(request: Request, path: str | None = None):
    ctx = _wiki_context(request, path)
    ctx["rules"] = _rules(get_db(request))
    return templates.TemplateResponse(request, "knowledge.html", ctx)


@router.get("/fragments/knowledge-wiki")
def knowledge_wiki_fragment(request: Request, path: str | None = None):
    return templates.TemplateResponse(request, "partials/knowledge_wiki.html", _wiki_context(request, path))


@router.post("/knowledge/wiki")
async def save_wiki_page(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    path = data.get("path", "").strip()
    category = data.get("category", "").strip()
    title = data.get("title", "").strip()
    body = data.get("body", "")
    expected_version = int(data["expected_version"])
    try:
        await run_in_threadpool(
            core.note_wiki,
            conn,
            path=path,
            category=category,
            title=title,
            body=body,
            updated_by=get_owner_name(conn),
            expected_version=expected_version,
        )
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=409)
    return templates.TemplateResponse(request, "partials/knowledge_wiki.html", _wiki_context(request, path))


@router.post("/knowledge/wiki/delete")
async def delete_wiki_page(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    path = data.get("path", "").strip()
    try:
        await run_in_threadpool(
            core.delete_wiki_page, conn, path, int(data["expected_version"])
        )
    except ValueError as exc:
        return PlainTextResponse(str(exc), status_code=409)
    return templates.TemplateResponse(request, "partials/knowledge_wiki.html", _wiki_context(request, None))


@router.post("/knowledge/wiki/comments")
async def comment_wiki_page(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    path = data.get("path", "").strip()
    body = data.get("body", "").strip()
    try:
        await run_in_threadpool(core.comment_wiki_page, conn, path, get_owner_name(conn), body)
    except ValueError:
        pass
    return templates.TemplateResponse(request, "partials/knowledge_wiki.html", _wiki_context(request, path))


@router.get("/fragments/knowledge-rules")
def knowledge_rules_fragment(request: Request):
    return templates.TemplateResponse(request, "partials/knowledge_rules.html", _rules_context(request))


@router.post("/knowledge/rules")
async def create_rule(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    text = data.get("text", "").strip()
    core.create_rule(conn, text, get_owner_name(conn), title=title)
    return templates.TemplateResponse(request, "partials/knowledge_rules.html", _rules_context(request))


@router.post("/knowledge/rules/{rule_id}")
async def update_rule(request: Request, rule_id: int):
    conn = get_db(request)
    data = await read_form(request)
    title = data.get("title", "").strip()
    text = data.get("text", "").strip()
    core.update_rule(conn, rule_id, text, title=title or None)
    return templates.TemplateResponse(request, "partials/knowledge_rules.html", _rules_context(request))


@router.post("/knowledge/rules/{rule_id}/delete")
async def delete_rule(request: Request, rule_id: int):
    conn = get_db(request)
    core.delete_rule(conn, rule_id)
    return templates.TemplateResponse(request, "partials/knowledge_rules.html", _rules_context(request))
