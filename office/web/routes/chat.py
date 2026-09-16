"""Common chat: everyone posts to the 'all' channel, and everyone gets it."""

from __future__ import annotations

from fastapi import APIRouter, Request

from office import core
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

PAGE_SIZE = 200

PAGE_URL = "/fragments/chat"
REGION_ID = "chat-thread"


def _context(request: Request, before_id: int | None = None) -> dict:
    conn = get_db(request)
    messages, has_older = core.chat_history(conn, before_id=before_id, limit=PAGE_SIZE)
    return {
        "request": request,
        "active": "chat",
        "messages": messages,
        "before_id": before_id,
        "has_older": has_older,
        "oldest_id": messages[0]["id"] if messages else None,
        "conv_channel": "all",
        "conv_peer": "",
        "newest_id": messages[-1]["id"] if messages else None,
        "page_url": PAGE_URL,
        "region_id": REGION_ID,
        "owner_name": get_owner_name(conn),
        "empty_text": "No messages in the common chat yet.",
    }


@router.get("/chat")
def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html", _context(request))


@router.get("/fragments/chat")
def chat_fragment(request: Request, before_id: int | None = None):
    """The panel itself, wrapper included."""
    return templates.TemplateResponse(request, "partials/chat_panel.html", _context(request, before_id))


@router.post("/chat/messages")
async def send_chat(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    owner_name = get_owner_name(conn)
    core.send_message(conn, sender=owner_name, recipient="all", body=body, actor=owner_name)
    return templates.TemplateResponse(request, "partials/chat_panel.html", _context(request))
