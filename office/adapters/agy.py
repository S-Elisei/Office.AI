"""Antigravity (agy) adapter."""

from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from office.adapters import shared
from office.adapters.base import (
    Event,
    ModelInfo,
    QuotaSnapshot,
    clamp_fraction,
    identifying_argument,
    one_line,
    tool_summary,
    window_occupancy,
    with_outcome,
)

_log = logging.getLogger(__name__)

_GEMINI_GROUP = "gemini"

HOOKS_DIR = ".agents"
HOOKS_FILE = "hooks.json"
HOOK_NAME = "office-inbox"

HOOK_EVENTS = ("PreInvocation", "PostInvocation")

MODELS_TIMEOUT = 60.0

PRINT_TIMEOUT = "720h"

# Hours, minutes and seconds, each optional, at least one present.
_RESETS_IN = re.compile(r"Resets in (?=\d)(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")


MCP_SERVER_NAME = "office"
_SHIM = Path(__file__).with_name("mcp_shim.py")


def install_mcp_server(bin_path: str) -> bool:
    """Register the office stdio MCP server, machine-wide, once."""
    result = subprocess.run(
        [bin_path, "mcp", "add", "--type", "stdio", MCP_SERVER_NAME, sys.executable, str(_SHIM)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return result.returncode == 0


def hook_warning(agent: str, ws: str | Path) -> str | None:
    """Say so when this workspace's hook command still contains a space."""
    spaced = shared.agy_hook_spaced_paths(ws)
    if not spaced:
        return None
    return (
        f"agy agent '{agent}' has a hook command containing a space, in {spaced}. agy cannot be "
        "given a quoted hook command and 8.3 short names are unavailable for that path, so the "
        "hook will not run: this agent gets no mid-turn delivery, and messages reach it at the "
        "start of its next turn instead. The only other symptom is a line in agy.log. Moving the "
        "office checkout, the interpreter or the workspace root to a path without spaces is the "
        "one fix."
    )


class AgyAdapter:
    runtime = "agy"
    takes_system_prompt = False

    def __init__(self, bin_path: str) -> None:
        self.bin_path = bin_path

    def prepare_session(self, ws: str, agent: str) -> None:
        """Claim the workspace and install its inbox hook."""
        shared.claim_owner(ws, agent)
        path = Path(ws) / HOOKS_DIR / HOOKS_FILE
        config: dict = {}
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8") or "{}")
            except json.JSONDecodeError:
                existing = None
            if isinstance(existing, dict):
                config = existing
        config[HOOK_NAME] = {
            event: [{"type": "command", "timeout": 15,
                     "command": shared.agy_hook_command(ws, event)}]
            for event in HOOK_EVENTS
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        spaced = shared.agy_hook_spaced_paths(ws)
        if spaced:
            _log.warning(
                "agy hook command for %s still contains a space, in %s: no 8.3 short name was "
                "available, so the hook will not run and delivery falls back to the next turn",
                agent, spaced,
            )

    def build_command(
        self,
        ws: str,
        model: str,
        effort: str | None,
        office_url: str,
        session_id: str | None,
        resume: bool = False,
    ) -> list[str]:
        cmd = [
            self.bin_path,
            "--output-format",
            "stream-json",
            "--input-format",
            "stream-json",
            "--dangerously-skip-permissions",
            "--model",
            model,
            "--add-dir",
            ws,
            "--print-timeout",
            PRINT_TIMEOUT,
        ]
        if effort:
            cmd += ["--effort", effort]
        if resume and session_id:
            cmd.append(f"--conversation={session_id}")
        cmd.append("-p=")
        return cmd

    def encode_message(self, text: str) -> str:
        envelope = {
            "event": "user",
            "message": {"role": "user", "content": [{"type": "text", "text": text}]},
        }
        return json.dumps(envelope, ensure_ascii=False) + "\n"

    def parse_line(self, line: str) -> Event | None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None

        event = msg.get("event")
        body = msg.get(event) if isinstance(msg.get(event), dict) else {}
        session_id = msg.get("conversation_id") or body.get("conversation_id")

        if event == "init":
            return Event(kind="usage", session_id=session_id)

        if event == "result":
            status = str(body.get("status", "")).upper()
            return Event(
                kind="turn_end",
                text=body.get("response"),
                session_id=session_id,
                error=None if status in ("", "SUCCESS") else (body.get("error") or status),
            )

        if event == "step_update":
            return _step_event(body, session_id)

        if event == "error":
            return Event(kind="error", error=json.dumps(body or msg), session_id=session_id)

        return None

    def turn_totals(self, requests: list[dict]) -> dict | None:
        """The turn's totals, in the keys of Event.totals: the sums of its requests,
        or None when it has none."""
        if not requests:
            return None
        return {
            key: sum(request[key] or 0 for request in requests)
            for key in ("input", "cache_read", "output", "thinking")
        }

    def quota_reset(self, error: str | None) -> int | None:
        """When a quota refusal says the quota comes back, as epoch seconds, or None."""
        seconds = reset_seconds(error)
        return int(time.time()) + seconds if seconds is not None else None

    def poll_quota(self, session_ids: Sequence[str] = ()) -> list[QuotaSnapshot] | None:
        try:
            proc = subprocess.run(
                [self.bin_path, "--output-format", "json", "--print", "/usage"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return parse_usage_payload(payload)

    def list_models(self) -> list[ModelInfo] | None:
        try:
            proc = subprocess.run(
                [self.bin_path, "models"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=MODELS_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return parse_models_output(proc.stdout)


def reset_seconds(text: str | None) -> int | None:
    """`Resets in 3h58m49s` in a refusal -> the seconds it names, or None."""
    match = _RESETS_IN.search(text or "")
    if match is None:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def parse_models_output(text: str) -> list[ModelInfo] | None:
    models = []
    for line in (text or "").splitlines():
        if "\t" not in line:
            continue
        model_id, _, display = line.partition("\t")
        model_id = model_id.strip()
        if not model_id:
            continue
        models.append(
            ModelInfo(runtime="agy", model_id=model_id, display_name=display.strip() or None)
        )
    return models or None


def parse_usage_payload(payload: dict) -> list[QuotaSnapshot] | None:
    groups = (((payload.get("command") or {}).get("data")) or {}).get("groups") or []
    snapshots = []
    for group in groups:
        name = str(group.get("name") or group.get("id") or "")
        if _GEMINI_GROUP not in str(group.get("id", "") or name).lower():
            continue
        for bucket in group.get("buckets") or []:
            bucket_name = str(bucket.get("name") or bucket.get("id") or "").strip()
            if not bucket_name:
                continue
            snapshots.append(
                QuotaSnapshot(
                    runtime="agy",
                    label=f"{name} / {bucket_name}" if name else bucket_name,
                    remaining_fraction=clamp_fraction(float(bucket.get("remaining_fraction", 0.0))),
                    reset_time=_epoch(bucket.get("reset_time")),
                )
            )
    return snapshots or None


def _epoch(value) -> int | None:
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
        except ValueError:
            return None
    return None


def _step_event(body: dict, session_id: str | None) -> Event | None:
    step_type = body.get("step_type")
    if step_type == "user_input":
        return None
    usage = body.get("usage")
    used = _used(usage)
    delta = body.get("text_delta")
    if step_type == "agent_response":
        if used is not None:
            request = None
            if str(body.get("state") or "").upper() == "DONE":
                request = {"key": body.get("step_index"), "at": None, "model": None,
                           "cache_write_1h": None, "tools": None, **_counts(usage)}
            return Event(kind="usage", text=delta, session_id=session_id, context_used=used,
                         request=request)
        return Event(kind="text", text=delta, session_id=session_id) if delta else None
    # Everything else is a tool step; agy names the kind in step_type.
    phase = _phase(body.get("state"))
    text = _tool_text(body) or step_type
    if phase == "finished":
        text = with_outcome(text, _outcome(body))
    return Event(
        kind="tool",
        phase=phase,
        text=text,
        session_id=session_id,
        context_used=used,
    )


def _counts(usage: dict) -> dict:
    """agy's `usage` under the number keys of Event.request; `cache_write` is
    None."""
    return {
        "input": usage.get("input_tokens"),
        "cache_read": usage.get("cache_read_tokens"),
        "cache_write": None,
        "output": usage.get("output_tokens"),
        "thinking": usage.get("thinking_tokens"),
    }


def _phase(state) -> str | None:
    """`step_update.state` -> started / finished.

    ACTIVE, DONE and ERROR are agy's three words for a tool step, all carried
    under one `step_index`: ACTIVE while the step runs, DONE or ERROR when it
    ends. ERROR is a step that could not run at all, and it is as terminal as
    DONE — it carries the `duration_seconds` an ending step has and an ACTIVE
    update does not, and the index is not seen again. Both therefore map to
    `finished`, so the started row gets its twin either way.

    A state not named above yields None, and a reader adding a fourth must hold
    to the rule that makes that safe: map a state to `finished` only once it is
    known to end a step, never from the sound of its name. A row whose phase is
    unknown is still a row, while a running tool called finished reads as "this
    command came back" and nothing downstream can correct it. That is the
    cheaper of the two errors even so — the started row keeps no twin and reads
    as a step the agent is still sitting inside, and an agent reported as busy
    gets looked at where an agent reported as done does not.
    """
    text = str(state or "").upper()
    if text == "ACTIVE":
        return "started"
    if text in ("DONE", "ERROR"):
        return "finished"
    return None


def _outcome(body: dict) -> str:
    """How a finished agy tool step ENDED, for the suffix on its row.

    The suffix earns its place twice over: it tells a reader of the transcript
    that this step came back, and its absence is what marks the step an agy
    agent is still sitting inside — _step_event appends it on `finished` only.

    **"done" is said only when nothing in the step contradicts it.** The test is
    the UNION of the two things this function reads: the state naming ERROR, or
    a reason sitting under `tool_info.error`. Either one alone means `failed`,
    and a step with neither is the only kind that says `done`.

    The union rather than the state alone, because the two possible mistakes do
    not cost the same. A row that says `failed` for a step that ran is visible,
    cheap, and corrected by the next thing its reader opens; a row that says
    `done` for a step that failed asserts a success, and it is the only signal
    that reader gets — nothing downstream can take it back. There is no third
    field here to catch that, so narrowing the test would hand the assertion to
    every shape that carries a reason and no verdict.

    The reason is taken from `error` as `{"message": …}`, or from `error` itself
    where that is a string, and is rendered only if what comes out is a string:
    a stringified structure in a row says nothing, where `failed` on its own is
    true. A reason that yields no string still fails the step — it adds nothing
    to the suffix, which is a different thing. Capped like every other subject
    (one_line), because what this returns lands in a transcript row.

    DONE and ERROR are the only states that reach here, because they are the two
    _phase calls finished. A reader teaching _phase a third has to say here what
    that state's row reads as, or it will quietly read as success.

    This is the human-readable half only. `phase` is the machine-readable
    answer, and nothing may parse a verdict back out of this text
    (office/adapters/base.py, with_outcome).
    """
    info = body.get("tool_info")
    error = info.get("error") if isinstance(info, dict) else None
    if str(body.get("state") or "").upper() != "ERROR" and not error:
        return "done"
    message = error.get("message") if isinstance(error, dict) else error
    detail = one_line(message) if isinstance(message, str) else None
    return f"failed: {detail}" if detail else "failed"


def _tool_text(body: dict) -> str | None:
    """What an agy tool step is doing, or None to fall back to the step type.

    **The subject of an agy tool step lives at `tool_info.parameters`**, beside
    a `tool_name` the step repeats:

        "tool_name": "run_command",
        "tool_info": {"name": "run_command",
                      "parameters": {"CommandLine": "git diff", "Cwd": …}}

    and the office's own tools arrive wrapped in agy's MCP caller:

        "tool_name": "call_mcp_tool",
        "tool_info": {"parameters": {"ServerName": "office", "ToolName": "pr",
                                     "Arguments": {"op": "read", "pr_id": 3}}}

    **`tool_calls: [{name, args}]` is the shape of agy's own internal transcript
    files and it is not on this stream** — a reader who reaches for it, because
    that is where agy's on-disk history keeps its calls, gets a key that never
    exists here, and every row silently falls back to the step type: a whole
    transcript reading "TOOL tool", which says a tool ran and cannot say which.

    `call_mcp_tool` is unwrapped rather than shown as itself: a row saying
    `call_mcp_tool office` for every single office call would push the one thing
    worth reading — which tool, on what — off the end of the line.
    """
    info = body.get("tool_info")
    params = info.get("parameters") if isinstance(info, dict) else None
    if not isinstance(params, dict):
        params = None
    name = body.get("tool_name") or (info.get("name") if isinstance(info, dict) else None)

    if name == "call_mcp_tool" and params:
        server = params.get("ServerName")
        tool = params.get("ToolName")
        if isinstance(server, str) and isinstance(tool, str):
            return tool_summary(f"{server}/{tool}", identifying_argument(params.get("Arguments")))

    return tool_summary(name, identifying_argument(params))


def _used(usage: dict | None) -> int | None:
    """How full the window is, from a `step_update`.

    **`total_tokens` is NOT the answer and must not be used here.** On this
    stream it is `input_tokens + output_tokens` only, and it leaves out
    `cache_read_tokens` — which is the part of the prompt served from cache and
    therefore very much still occupying the window. The occupancy is
    `input_tokens + cache_read_tokens`, the same disjoint-fields rule claude
    needs (see claude._context_used).

    The two fields move against each other across a turn, which is what makes
    the wrong one so convincing: on a cold cache the whole context arrives as
    `input_tokens`, and as the cache warms that same context moves into
    `cache_read_tokens` while `input_tokens` drops. So `input + cache_read`
    climbs with the conversation while `total_tokens` stays flat near its
    starting value — a window filling towards its ceiling reads as one barely
    touched, which is precisely the number the director uses to decide who can
    take the next long job. The two figures diverge by more than an order of
    magnitude by the end of a long turn.

    ⚠ **`output_tokens` IS included**, and the argument against it is worth
    answering here because it is the natural one: output is what the model
    produced this round rather than what the window holds, and it joins the
    context only as the next round's input. The fact is right; the conclusion
    is not. Being counted in the next round's input is exactly what makes it
    occupy the window already — the question this number answers is whether the
    NEXT round fits. The other two runtimes agree: claude decides its own
    auto-compaction on `input + cache_creation + cache_read + output`, and
    codex's own `total_tokens` is `input + output`.

    `thinking_tokens` is NOT added. Thinking is not carried into the next
    round's prompt, so adding it puts tokens in the window that the next request
    will not send, and the sum overshoots what the window actually holds.

    base.window_occupancy owns the rule; this function owns the field names.
    """
    if not usage:
        return None
    prompt = sum(
        int(usage.get(key, 0) or 0) for key in ("input_tokens", "cache_read_tokens")
    )
    return window_occupancy(prompt, usage.get("output_tokens"))
