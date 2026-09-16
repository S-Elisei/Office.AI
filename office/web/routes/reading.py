"""What the owner has read: the one route that moves his reading marks."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from office import core
from office.web.deps import get_db, read_form

router = APIRouter()


@router.post("/messages/seen")
async def mark_seen(request: Request):
    """Record that the owner has seen one conversation as far as one message."""
    data = await read_form(request)
    channel = data.get("channel", "")
    peer = data.get("peer", "").strip() if channel == "dm" else ""
    core.see_conversation(
        get_db(request), channel=channel, peer=peer, up_to_message_id=int(data["message_id"])
    )
    return Response(status_code=204)
