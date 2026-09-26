"""Claude Code adapter."""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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

_DAY_MS = str(24 * 60 * 60 * 1000)

USAGE_TIMEOUT = 60.0

AUTOCOMPACT = "auto"

# Matches a line shaped "<label>: N% used".
_USAGE_LINE = re.compile(r"^([^:\n]{1,80}?):\s*(\d{1,3})%\s+used\b(.*)$", re.MULTILINE)

# Minutes are optional; the matched text carries no year.
_RESET = re.compile(
    r"resets\s+([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?\s*([ap]m)\s*\(([^)]+)\)",
    re.IGNORECASE,
)

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

# Captures everything after "Available:" to the end of the line.
_AVAILABLE = re.compile(r"Available:\s*(.+)")

# Matches the first parenthesised group within 200 characters after `--effort`.
_EFFORT_HELP = re.compile(r"--effort\b[^()]{0,200}\(([^)]*)\)")

HELP_TIMEOUT = 30.0

_CHILD_ENV = {
    "BASH_DEFAULT_TIMEOUT_MS": _DAY_MS,
    "BASH_MAX_TIMEOUT_MS": _DAY_MS,
    "MCP_TOOL_TIMEOUT": _DAY_MS,
    "CLAUDE_CODE_MCP_TOOL_IDLE_TIMEOUT": _DAY_MS,
    "MCP_TIMEOUT": str(5 * 60 * 1000),
    "MCP_CONNECT_TIMEOUT_MS": str(5 * 60 * 1000),
}


class ClaudeAdapter:
    runtime = "claude"
    # --append-system-prompt-file, appended to claude's own
    takes_system_prompt = True

    def __init__(self, bin_path: str) -> None:
        self.bin_path = bin_path
        # tool_use id -> the summary already shown when that tool STARTED.
        self._tool_names: dict[str, str] = {}

    def child_env(self) -> dict[str, str]:
        return dict(_CHILD_ENV)

    def prepare_session(self, ws: str, agent: str) -> None:
        """Install the PostToolUse inbox hook and claim the workspace for `agent`.

        The settings file goes under `.office/` and is passed with `--settings`
        rather than written to `<ws>/.claude/settings.json`.

        The matcher must be a real regex — `"*"` alone does not compile.
        """
        shared.claim_owner(ws, agent)
        settings = shared.office_dir(ws) / "claude-settings.json"
        settings.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PostToolUse": [
                            {
                                "matcher": ".*",
                                "hooks": [
                                    {"type": "command", "command": shared.claude_hook_command(ws)}
                                ],
                            }
                        ]
                    }
                },
                indent=2,
            ),
            encoding="utf-8",
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
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--input-format",
            "stream-json",
            "--permission-mode",
            "bypassPermissions",
            "--model",
            model,
            # The whole of the office's context management.
            "--autocompact",
            AUTOCOMPACT,
        ]
        if session_id:
            # --session-id assigns an id to a session being created; --resume
            # continues one that exists.
            cmd += ["--resume" if resume else "--session-id", session_id]
        if effort:
            cmd += ["--effort", effort]
        cmd += [
            "--disallowedTools",
            "SendMessage", "ListAgents", "Task",
            "CronCreate", "CronDelete", "CronList", "Monitor",
            "Artifact", "ArtifactComments", "ArtifactData",
            "RemoteTrigger", "PushNotification", "ScheduleWakeup", "DesignSync",
            "EnterWorktree", "ExitWorktree",
        ]
        # Exactly one server, "office". --strict-mcp-config keeps any other
        # server out of the context.
        cmd += [
            "--mcp-config",
            json.dumps({"mcpServers": {"office": {"type": "http", "url": office_url}}}),
            "--strict-mcp-config",
        ]
        settings = shared.office_dir(ws) / "claude-settings.json"
        if Path(settings).exists():
            cmd += ["--settings", str(settings)]
        system = shared.system_prompt_path(ws)
        if system.exists():
            # Appends to claude's own system prompt rather than replacing it.
            cmd += ["--append-system-prompt-file", str(system)]
        cmd += ["--add-dir", ws]
        return cmd

    def compact_command(self, ws: str, session_id: str) -> list[str]:
        """Ask claude to summarize its own session in place.

        A separate invocation from a normal turn: `/compact` is passed as the
        argv prompt, not as a stream-json message on stdin.
        """
        return [
            self.bin_path,
            "-p",
            "/compact",
            "--resume",
            session_id,
            "--output-format",
            "stream-json",
            "--verbose",
            "--add-dir",
            ws,
        ]

    def encode_message(self, text: str) -> str:
        envelope = {
            "type": "user",
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

        kind = msg.get("type")

        if kind == "system" and msg.get("subtype") == "init":
            return Event(kind="usage", session_id=msg.get("session_id"))

        if kind == "system" and msg.get("subtype") == "compact_boundary":
            meta = msg.get("compact_metadata") or {}
            return Event(
                kind="compact",
                session_id=msg.get("session_id"),
                context_used=meta.get("post_tokens"),
                context_before=meta.get("pre_tokens"),
                text=str(meta.get("trigger") or "unknown"),
            )

        if kind == "assistant":
            return _assistant_event(msg, self._tool_names)

        # The other half of a claude tool call: the result comes back as a
        # `user` message carrying `tool_result` blocks, the same envelope our
        # own injected messages arrive in. This branch answers only when a
        # tool_result is present.
        if kind == "user":
            return _tool_result_event(msg, self._tool_names)

        if kind == "result":
            return _result_event(msg)

        return None

    def poll_quota(self, session_ids: Sequence[str] = ()) -> list[QuotaSnapshot] | None:
        """`claude -p /usage --output-format json`. Reaches the CLI only as the
        argv prompt.

        `session_ids` is unused: `/usage` asks the account, not a session.
        """
        payload = self._json_slash_command("/usage")
        if payload is None:
            return None
        return parse_usage_payload(payload)

    def list_models(self) -> list[ModelInfo] | None:
        """`claude -p /model --output-format json`.

        `/model` gives the aliases and `--help` gives the `--effort` enum.
        Every ModelInfo from this runtime carries the same effort tuple. A
        help parse that fails leaves `efforts` None.

        Returns None on any failure.
        """
        payload = self._json_slash_command("/model")
        if payload is None:
            return None
        return parse_model_payload(payload, efforts=self._effort_levels())

    def _json_slash_command(self, command: str) -> dict | None:
        """One free `-p <slash command> --output-format json` call, parsed.

        Refuses to interpret anything but a clean exit and valid JSON.
        """
        try:
            proc = subprocess.run(
                [self.bin_path, "-p", command, "--output-format", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=USAGE_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def _effort_levels(self) -> tuple[str, ...] | None:
        """Valid `--effort` values, read from `claude --help`. None if unreadable."""
        try:
            proc = subprocess.run(
                [self.bin_path, "--help"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=HELP_TIMEOUT,
                shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if proc.returncode != 0:
            return None
        return parse_effort_help(proc.stdout)


def parse_effort_help(text: str) -> tuple[str, ...] | None:
    """The parenthesised group after `--effort` -> its comma-separated names.

    Only bare lowercase words survive.
    """
    match = _EFFORT_HELP.search(text or "")
    if not match:
        return None
    levels = tuple(
        part.strip() for part in match.group(1).split(",")
        if part.strip().isalpha() and part.strip().islower()
    )
    return levels or None


def parse_model_payload(payload: dict, efforts: tuple[str, ...] | None = None) -> list[ModelInfo] | None:
    """`/model`'s prose -> the aliases it lists.

    An alias is one token; multi-word entries are dropped by that shape. This
    function only refuses to call such an entry a model.

    Returns None rather than [] when the sentence is missing or unrecognisable.
    """
    text = payload.get("result")
    if not isinstance(text, str):
        return None
    match = _AVAILABLE.search(text)
    if not match:
        return None
    models = []
    for part in match.group(1).rstrip().rstrip(".").split(","):
        alias = part.strip()
        # One token or it is not an alias.
        if not alias or " " in alias:
            continue
        models.append(ModelInfo(runtime="claude", model_id=alias, efforts=efforts))
    return models or None


def parse_usage_payload(payload: dict) -> list[QuotaSnapshot] | None:
    """Buckets out of `/usage`'s answer, each labelled in claude's own words.

    Only lines shaped "<label>: N% used" become buckets; other lines are
    excluded by shape. Labels are taken verbatim.
    """
    text = payload.get("result")
    if not isinstance(text, str):
        return None
    snapshots = []
    for label, used, rest in _USAGE_LINE.findall(text):
        label = label.strip()
        if not label:
            continue
        snapshots.append(
            QuotaSnapshot(
                runtime="claude",
                label=label,
                remaining_fraction=clamp_fraction(1.0 - float(used) / 100.0),
                reset_time=_reset_epoch(rest),
            )
        )
    return snapshots or None


def _reset_epoch(text: str) -> int | None:
    """The matched reset text -> epoch seconds UTC, or None.

    The year is inferred: try this year, then the next, keeping whichever is
    not already in the past.

    Returns None if the zone name does not resolve.

    Does not fall back to the machine's local timezone.
    """
    match = _RESET.search(text or "")
    if match is None:
        return None
    month_name, day, hour, minute, meridiem, zone_name = match.groups()
    month = _MONTHS.get(month_name.lower())
    if month is None:
        return None
    hour = int(hour) % 12  # 12am is hour 0, 12pm is hour 12 once pm is added
    if meridiem.lower() == "pm":
        hour += 12
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, KeyError, OSError):
        return None  # unresolvable zone: no guess

    now = datetime.now(zone)
    for year in (now.year, now.year + 1):
        try:
            when = datetime(year, month, int(day), hour, int(minute or 0), tzinfo=zone)
        except ValueError:
            continue  # Feb 29 of a non-leap year, say
        # A little slack, rather than comparing directly to now.
        if when > now - timedelta(hours=1):
            return int(when.astimezone(timezone.utc).timestamp())
    return None


def _assistant_event(msg: dict, tool_names: dict[str, str]) -> Event:
    """One `assistant` message -> a tool event if it calls tools, else text.

    The result comes back later in a `user` message (_tool_result_event).

    Prose and tool calls are both kept and joined; the prose is capped like
    everything else on a tool row.
    """
    message = msg.get("message") or {}
    blocks = message.get("content") or []
    texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
    calls = [b for b in blocks if b.get("type") == "tool_use"]
    prose = "".join(texts)
    summaries = []
    for block in calls:
        summary = tool_summary(block.get("name") or "?", identifying_argument(block.get("input")))
        summaries.append(summary or "?")
        block_id = block.get("id")
        if isinstance(block_id, str) and summary:
            tool_names[block_id] = summary
    if calls:
        parts = ([one_line(prose)] if prose.strip() else []) + summaries
        text = " · ".join(p for p in parts if p)
    else:
        # Not truncated.
        text = prose or None
    return Event(
        kind="tool" if calls else "text",
        phase="started" if calls else None,
        text=text,
        session_id=msg.get("session_id"),
        # Mid-turn context reading.
        context_used=_context_used(message.get("usage")),
    )


def _tool_result_event(msg: dict, tool_names: dict[str, str]) -> Event | None:
    """A `user` message carrying tool results -> the finished half, or None.

    Returns None for every other `user` message.

    The result content is deliberately not shown.

    A result whose id was never seen renders by id rather than being dropped.
    """
    message = msg.get("message") or {}
    blocks = message.get("content") or []
    if not isinstance(blocks, list):
        return None
    results = [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
    if not results:
        return None
    summaries = []
    for block in results:
        block_id = block.get("tool_use_id")
        known = tool_names.pop(block_id, None) if isinstance(block_id, str) else None
        # A missing `is_error` reads as "done" rather than being asserted either way.
        outcome = "error" if block.get("is_error") else "done"
        summaries.append(with_outcome(known or one_line(block_id, 40) or "tool", outcome))
    return Event(
        kind="tool",
        phase="finished",
        text=" · ".join(summaries),
        session_id=msg.get("session_id"),
    )


def _context_used(usage: dict | None) -> int | None:
    """Window occupancy from one `assistant` message's usage.

    The three prompt fields are summed rather than treated as a breakdown.
    base.window_occupancy owns what happens to `output_tokens`.
    """
    if not usage:
        return None
    prompt = sum(
        int(usage.get(key, 0) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )
    return window_occupancy(prompt, usage.get("output_tokens"))


def _result_event(msg: dict) -> Event:
    limits = [
        int(entry["contextWindow"])
        for entry in (msg.get("modelUsage") or {}).values()
        if isinstance(entry, dict) and entry.get("contextWindow")
    ]
    error = None
    if msg.get("is_error") or msg.get("subtype") not in (None, "success"):
        error = " ".join(
            str(part)
            for part in (
                msg.get("subtype"),
                msg.get("api_error_status"),
                msg.get("terminal_reason"),
                msg.get("result"),
            )
            if part
        )
    return Event(
        kind="turn_end",
        session_id=msg.get("session_id"),
        context_limit=max(limits) if limits else None,
        error=error,
    )
