"""Mid-turn delivery plumbing shared by the adapters.

Imports the standard library only: inbox_hook.py loads this file from its own directory.
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

_HOOK_SCRIPT = Path(__file__).with_name("inbox_hook.py")
_IS_WINDOWS = os.name == "nt"

# Names the agent a process is running as.
AGENT_ENV = "OFFICE_AGENT"


def office_dir(ws: str | Path) -> Path:
    return Path(ws) / ".office"


def inbox_path(ws: str | Path) -> Path:
    return office_dir(ws) / "inbox"


def system_prompt_path(ws: str | Path) -> Path:
    """Composed system prompt for this agent, written before each turn."""
    return office_dir(ws) / "system.md"


def owner_path(ws: str | Path) -> Path:
    return office_dir(ws) / "owner"


def claim_owner(ws: str | Path, agent: str) -> None:
    """Record which agent this workspace's inbox belongs to.

    Rewritten on every session start. Does NOT clear the inbox.
    """
    office_dir(ws).mkdir(parents=True, exist_ok=True)
    owner_path(ws).write_text(agent, encoding="utf-8")


def deliver(ws: str | Path, text: str, agent: str, after: int) -> None:
    """Queue a message for `agent`'s next tool call.

    `after` is `agent`'s watermark as it stood before this line was written.
    """
    path = inbox_path(ws)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps({"to": agent, "text": text, "after": after}, ensure_ascii=False) + "\n"
        )


def _claim_raw(inbox: Path) -> str | None:
    """Take the whole queue file, atomically: its text, or None when there is
    none or someone beat us."""
    claim = inbox.with_name(f"inbox.claim.{uuid.uuid4().hex}")
    try:
        os.replace(inbox, claim)
    except OSError:
        return None
    raw = claim.read_text(encoding="utf-8", errors="replace")
    claim.unlink()
    return raw


def claim_inbox(ws: str | Path, agent: str) -> str | None:
    """Take everything queued for `agent`, atomically, or return None if someone
    beat us."""
    inbox = inbox_path(ws)
    raw = _claim_raw(inbox)
    if raw is None:
        return None
    mine, others = split_for(raw, agent)
    if others:
        _requeue(inbox, others)
    return mine


def take_back(ws: str | Path, agent: str) -> int | None:
    """Take `agent`'s lines off the queue, atomically, and return the lowest
    watermark they were written after, or None when there were none."""
    inbox = inbox_path(ws)
    raw = _claim_raw(inbox)
    if raw is None:
        return None
    floor: int | None = None
    others: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if item["to"] == agent:
            floor = item["after"] if floor is None else min(floor, item["after"])
        else:
            others.append(line)
    if others:
        _requeue(inbox, others)
    return floor


def _requeue(inbox: Path, lines: list[str]) -> None:
    """Put lines addressed to somebody else back on the queue."""
    inbox.parent.mkdir(parents=True, exist_ok=True)
    with inbox.open("a", encoding="utf-8") as handle:
        handle.write("".join(line + "\n" for line in lines))


def split_for(raw: str, agent: str) -> tuple[str | None, list[str]]:
    """What `agent` may take, and the raw lines that are not theirs."""
    out: list[str] = []
    others: list[str] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        item = json.loads(line)
        if item["to"] == agent:
            out.append(item["text"])
        else:
            others.append(line)
    joined = "\n".join(t for t in out if t).strip()
    return (joined or None), others


def _posix(path: Path | str) -> str:
    return str(path).replace("\\", "/")


def claude_hook_command(ws: str | Path) -> str:
    """Per-workspace hook command for claude."""
    return (
        f'[ -s "{_posix(inbox_path(ws))}" ] || exit 0; '
        f'"{_posix(sys.executable)}" "{_posix(_HOOK_SCRIPT)}" claude "{_posix(Path(ws))}"'
    )


def _unquotable(path: Path | str) -> str:
    """A Windows path with no spaces."""
    text = str(path)
    if " " not in text:
        return text
    import ctypes

    buffer = ctypes.create_unicode_buffer(4096)
    length = ctypes.windll.kernel32.GetShortPathNameW(text, buffer, 4096)
    return buffer.value if length else text


def agy_hook_spaced_paths(ws: str | Path) -> list[str]:
    """The paths in agy's hook command that still contain a space, if any.

    Empty on POSIX by construction.
    """
    if not _IS_WINDOWS:
        return []
    candidates = (_unquotable(sys.executable), _unquotable(_HOOK_SCRIPT), _unquotable(Path(ws)))
    return [path for path in candidates if " " in path]


def agy_hook_command(ws: str | Path, phase: str) -> str:
    """Per-workspace hook command for agy."""
    if _IS_WINDOWS:
        return " ".join(
            [_unquotable(sys.executable), _unquotable(_HOOK_SCRIPT), "agy",
             _unquotable(Path(ws)), phase]
        )
    return (
        f'"{_posix(sys.executable)}" "{_posix(_HOOK_SCRIPT)}" agy '
        f'"{_posix(Path(ws))}" {phase}'
    )


def _ps_single_quoted(value: str | Path) -> str:
    """A PowerShell single-quoted literal."""
    return "'" + str(value).replace("'", "''") + "'"


def codex_hook_command(ws: str | Path) -> str:
    """Per-workspace hook command for codex."""
    if _IS_WINDOWS:
        return (
            "if (!(Test-Path -LiteralPath " + _ps_single_quoted(inbox_path(ws)) + ")) "
            "{ Write-Output '{}'; exit 0 }; "
            "& " + _ps_single_quoted(sys.executable)
            + " " + _ps_single_quoted(_HOOK_SCRIPT)
            + " 'codex' " + _ps_single_quoted(Path(ws))
        )
    return (
        f'[ -s "{_posix(inbox_path(ws))}" ] || {{ echo "{{}}"; exit 0; }}; '
        f'"{_posix(sys.executable)}" "{_posix(_HOOK_SCRIPT)}" codex "{_posix(Path(ws))}"'
    )


def codex_hook_config(ws: str | Path) -> str:
    """The inline TOML that installs that hook for ONE invocation."""
    handler = '{type="command", command=' + json.dumps(codex_hook_command(ws)) + ", timeout=15}"
    return "hooks.PostToolUse=[{hooks=[" + handler + "]}]"
