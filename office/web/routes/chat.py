"""Common chat: everyone posts to the 'all' channel, and everyone gets it."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import Response

from office import core
from office.web import threads
from office.web.deps import get_db, get_owner_name, read_form
from office.web.templating import templates

router = APIRouter()

REGION_ID = "chat-thread"


def _context(request: Request) -> dict:
    conn = get_db(request)
    messages, has_older = core.chat_history(conn, limit=threads.PAGE_SIZE)
    return {
        "request": request,
        "active": "chat",
        "messages": messages,
        "has_older": has_older,
        "conv_channel": "all",
        "conv_peer": "",
        "newest_id": messages[-1]["id"] if messages else None,
        "region_id": REGION_ID,
        "owner_name": get_owner_name(conn),
        "empty_text": "No messages in the common chat yet.",
    }


@router.get("/chat")
def chat_page(request: Request):
    return templates.TemplateResponse(request, "chat.html", _context(request))


@router.post("/chat/messages")
async def send_chat(request: Request):
    conn = get_db(request)
    data = await read_form(request)
    body = data.get("body", "").strip()
    owner_name = get_owner_name(conn)
    core.send_message(conn, sender=owner_name, recipient="all", body=body, actor=owner_name)
    return Response(status_code=204)
