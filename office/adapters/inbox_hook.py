"""Runtime hook: hands the agent whatever the office has queued for its workspace."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.parse import quote, urlsplit

import shared

PREAMBLE = "Office messages that arrived during this turn:"

STALE_PROMPT_REASON = "The agent's system prompt changed during this turn."

# Seconds the hub has to answer.
HUB_TIMEOUT = 10


def payload(runtime: str, text: str, phase: str = "") -> dict:
    body = PREAMBLE + "\n" + text
    if runtime == "agy":
        reply = {"injectSteps": [{"systemMessage": {"systemMessage": body}}]}
        if phase == "PostInvocation":
            reply["terminationBehavior"] = "force_continue"
        return reply
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": body}}


def read_stdin_json() -> dict:
    """The hook event, or {} if there is nothing readable there."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def stale_prompt_stop() -> dict | None:
    """The reply that ends a codex turn about to compact, or None when the hub
    says the system prompt the turn was launched with is still the agent's current one.

    The hub is the one OFFICE_MCP_URL names; the agent is OFFICE_AGENT.
    """
    hub = urlsplit(os.environ["OFFICE_MCP_URL"])
    agent = quote(os.environ[shared.AGENT_ENV], safe="")
    request = urllib.request.Request(
        f"{hub.scheme}://{hub.netloc}/turns/{agent}/prompt-current", data=b"", method="POST"
    )
    with urllib.request.urlopen(request, timeout=HUB_TIMEOUT) as response:
        current = json.loads(response.read().decode("utf-8"))["current"]
    return None if current else {"continue": False, "stopReason": STALE_PROMPT_REASON}


def deliverable(runtime: str, argv: list[str]) -> dict | None:
    """The reply object, or None when this hook has nothing to say."""
    if runtime == "agy":
        read_stdin_json()
    elif runtime == "codex":
        event = read_stdin_json().get("hook_event_name")
        if event == "PreCompact":
            return stale_prompt_stop()
        if event != "PostToolUse":
            return None
    ws = Path(argv[2]) if len(argv) > 2 else None
    if ws is None:
        return None

    agent = os.environ.get(shared.AGENT_ENV)
    try:
        owner = shared.owner_path(ws).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not agent or agent != owner:
        return None

    text = shared.claim_inbox(ws, agent) or ""
    phase = argv[3] if runtime == "agy" and len(argv) > 3 else ""
    return payload(runtime, text, phase) if text else None


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    runtime = sys.argv[1]
    reply = deliverable(runtime, sys.argv)
    if reply is None:
        if runtime in ("codex", "agy"):
            sys.stdout.write("{}\n")
        return
    json.dump(reply, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
