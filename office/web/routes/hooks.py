"""The hook a local service posts to: the answer to one agent's expectation."""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import PlainTextResponse

from office import core
from office.web.deps import get_db

router = APIRouter()

_SERVICE_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")

_SHAPE = 'send {"from": "<service>", "text": "<message>"} as JSON'


def _take(raw: bytes) -> tuple[str, str]:
    """The service's name and its text, out of the request.

    Raises ValueError with the sentence that says what is wrong with it.
    """
    try:
        body = json.loads(raw)
    except ValueError:
        raise ValueError(f"the body is not JSON; {_SHAPE}") from None
    if not isinstance(body, dict):
        raise ValueError(f"the body is not a JSON object; {_SHAPE}")
    service, text = body.get("from"), body.get("text")
    if not isinstance(service, str) or not _SERVICE_NAME.fullmatch(service):
        raise ValueError(
            "'from' is missing or is not a service name: lowercase letters, digits and "
            "hyphens, starting with a letter or a digit, at most 40 characters"
        )
    if not isinstance(text, str) or not text.strip():
        raise ValueError("'text' is missing, empty or not a string")
    return service, text


@router.post("/hooks/{token}")
async def hook_answer(request: Request, token: str):
    """Take a service's answer to the expectation open at `token`."""
    try:
        service, text = _take(await request.body())
    except ValueError as exc:
        return PlainTextResponse(
            f"The message was not taken: {exc}.", status_code=400
        )
    sent = await run_in_threadpool(core.answer_expectation, get_db(request), token, service, text)
    if sent is None:
        return PlainTextResponse(
            "Nothing is waiting at this address: it has been answered, its time has run out, "
            "its agent is gone, or it never existed. Stop sending to it.",
            status_code=404,
        )
    return PlainTextResponse(f"Queued for {sent['recipient']}.", status_code=202)
