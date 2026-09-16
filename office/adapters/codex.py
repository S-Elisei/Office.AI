"""Codex adapter."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from office.adapters import shared
from office.adapters.base import (
    TOOL_SUMMARY_CHARS,
    Event,
    ModelInfo,
    QuotaSnapshot,
    clamp_fraction,
    identifying_argument,
    one_line,
    quota_window,
    strip_shell_prefix,
    tool_summary,
    with_outcome,
)

REMOTE_CONTEXT_CAP = 872_000

MCP_TOOL_TIMEOUT_SEC = 24 * 60 * 60
MCP_STARTUP_TIMEOUT_SEC = 300

# How much of a rollout file's tail to read on the first look. Falls back to
# the whole file if the tail holds no token_count yet. Every later look reads
# only the bytes appended since — see _RolloutTail.
_TAIL_BYTES = 512 * 1024


def codex_home() -> Path:
    """Where codex keeps its state. `CODEX_HOME` wins, else ~/.codex."""
    override = os.environ.get("CODEX_HOME")
    return Path(override) if override else Path.home() / ".codex"


def _sessions_dir() -> Path:
    return codex_home() / "sessions"


def _session_file(thread_id: str) -> Path | None:
    """The rollout file for one thread, or None. Resolve once per turn.

    Today's and yesterday's directories are tried first. The full glob covers
    a resumed thread whose file is older.
    """
    if not thread_id:
        return None
    root = _sessions_dir()
    pattern = f"rollout-*-{thread_id}.jsonl"
    today = datetime.now()
    for day in (today, today - timedelta(days=1)):
        matches = sorted((root / day.strftime("%Y") / day.strftime("%m") / day.strftime("%d")).glob(pattern))
        if matches:
            return matches[-1]
    matches = sorted(root.glob(f"*/*/*/{pattern}"))
    return matches[-1] if matches else None


def _newest_office_session_file(thread_ids: Sequence[str]) -> Path | None:
    """The most recently written rollout file among the office's own threads.

    Returns None before the office has run a codex turn.

    Each id is resolved through _session_file().
    """
    newest: tuple[float, Path] | None = None
    for thread_id in thread_ids:
        path = _session_file(thread_id)
        if path is None:
            continue
        mtime = path.stat().st_mtime
        if newest is None or mtime > newest[0]:
            newest = (mtime, path)
    return newest[1] if newest else None


def models_cache_path() -> Path:
    """`$CODEX_HOME/models_cache.json`."""
    return codex_home() / "models_cache.json"


def parse_models_cache(payload: dict) -> list[ModelInfo] | None:
    """The cache file's `models` array -> what may be hired on codex.

    Only `visibility == "list"` is taken.

    Returns None when the array is missing or not a list.

    An entry missing a slug is skipped.
    """
    models = payload.get("models")
    if not isinstance(models, list):
        return None
    out = []
    for entry in models:
        if not isinstance(entry, dict) or entry.get("visibility") != "list":
            continue
        slug = entry.get("slug")
        if not isinstance(slug, str) or not slug:
            continue
        levels = entry.get("supported_reasoning_levels")
        efforts = None
        if isinstance(levels, list):
            efforts = tuple(
                lvl["effort"] for lvl in levels
                if isinstance(lvl, dict) and isinstance(lvl.get("effort"), str)
            ) or None
        display = entry.get("display_name")
        out.append(
            ModelInfo(
                runtime="codex",
                model_id=slug,
                display_name=display if isinstance(display, str) else None,
                efforts=efforts,
            )
        )
    return out


def _newest_token_count(lines: list[str]) -> dict | None:
    """The newest `token_count` payload among these lines, or None.

    A line the writer has not finished simply fails to parse and is skipped.
    """
    for line in reversed(lines):
        line = line.strip()
        if not line.startswith("{") or '"token_count"' not in line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
        if payload.get("type") == "token_count":
            return payload
    return None


class _RolloutTail:
    """The newest `token_count` in a rollout file, read incrementally.

    The first look reads at most `_TAIL_BYTES` (and, only if that tail holds
    no `token_count` yet, the whole file once); every look after that reads
    exactly the bytes appended since the previous one.

    `_offset` is always a line boundary: a partially written last line is
    scanned but not consumed. The complete version is read again next time.

    A file that shrank is a different file: everything remembered about it is
    dropped and the next look starts over.

    One instance per adapter instance, which office/bus.py builds per turn.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._path: Path | None = None
        self._offset = 0
        self._payload: dict | None = None

    def token_count(self, path: Path | None) -> dict | None:
        if path is None:
            return None
        with self._lock:
            try:
                size = path.stat().st_size
            except OSError:
                return self._payload if path == self._path else None
            if path != self._path or size < self._offset:
                self._path, self._offset, self._payload = path, 0, None
            if size == self._offset:
                return self._payload  # nothing appended since the last look
            first_look = self._offset == 0
            start = max(0, size - _TAIL_BYTES) if first_look else self._offset
            payload, consumed = self._scan(path, start, size, drop_first_line=first_look and start > 0)
            if payload is None and first_look and start > 0:
                # The tail held no token_count yet — read the whole file, once.
                payload, consumed = self._scan(path, 0, size, drop_first_line=False)
            if consumed is None:
                return self._payload  # unreadable right now; the last figure stands
            self._offset = consumed
            if payload is not None:
                self._payload = payload
            return self._payload

    @staticmethod
    def _scan(path: Path, start: int, end: int, drop_first_line: bool) -> tuple[dict | None, int | None]:
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                blob = handle.read(end - start)
        except OSError:
            return None, None
        newline = blob.rfind(b"\n")
        consumed = start + newline + 1 if newline >= 0 else start
        lines = blob.decode("utf-8", errors="replace").splitlines()
        if drop_first_line and lines:
            # Only true of the first look: every later one starts exactly
            # where the previous complete line ended.
            lines = lines[1:]
        return _newest_token_count(lines), consumed


_SHIM_SUFFIXES = (".cmd", ".bat", ".ps1")

_HOOK_TRUST_NOTICE = "`--dangerously-bypass-hook-trust` is enabled"


def _vendored_executables(shim: Path) -> list[Path]:
    """The native codex binaries inside the npm package a shim belongs to."""
    package = shim.parent / "node_modules" / "@openai" / "codex"
    return sorted(package.glob("**/vendor/**/codex.exe"))


def native_binary(bin_path: str) -> str:
    """The real executable behind an npm shim, or `bin_path` untouched.

    Everything else — a native codex on PATH, a POSIX install, an owner's
    explicit OFFICE_CODEX_BIN pointing at a real binary — is returned unchanged.
    """
    path = Path(bin_path)
    if os.name != "nt" or path.suffix.lower() not in _SHIM_SUFFIXES:
        return bin_path
    found = _vendored_executables(path)
    # Last by path order is arbitrary but stable.
    return str(found[-1]) if found else bin_path


def shim_warning(bin_path: str | None) -> str | None:
    """Say so if codex can only be reached through a shim that breaks hooks.

    Point OFFICE_CODEX_BIN at the native executable to fix it.
    """
    if not bin_path or os.name != "nt":
        return None
    path = Path(bin_path)
    if path.suffix.lower() not in _SHIM_SUFFIXES or _vendored_executables(path):
        return None
    return (
        f"codex is only reachable as {path.name}, a batch shim, and no native codex.exe was "
        "found beside it. cmd.exe truncates the inline hook configuration, so codex agents "
        "will run without mid-turn delivery. Set OFFICE_CODEX_BIN to the real executable."
    )


class CodexAdapter:
    runtime = "codex"
    takes_system_prompt = False

    def __init__(self, bin_path: str) -> None:
        self.bin_path = native_binary(bin_path)
        # Per-instance; office/bus.py builds an instance per turn. The rollout
        # file of the thread this turn is running is resolved once and read
        # forward from there. See _session_file() and _RolloutTail.
        self._session_path: Path | None = None
        self._tail = _RolloutTail()

    def _rollout_file(self, thread_id: str) -> Path | None:
        """This turn's rollout file, remembered. Looked up again only when the
        remembered path is gone, or the thread id is not the one already
        resolved."""
        if not thread_id:
            return None
        path = self._session_path
        if path is not None and path.name.endswith(f"-{thread_id}.jsonl") and path.exists():
            return path
        self._session_path = _session_file(thread_id)
        return self._session_path

    def prepare_session(self, ws: str, agent: str) -> None:
        """Claim the workspace. The hook itself is installed by build_command.

        What remains here is the owner record the hook checks before it
        delivers.

        See build_command for the shape that works.
        """
        shared.claim_owner(ws, agent)

    def build_command(
        self,
        ws: str,
        model: str,
        effort: str | None,
        office_url: str | None,
        session_id: str | None,
        resume: bool = False,
    ) -> list[str]:
        cmd = [self.bin_path, "exec"]
        resuming = bool(resume and session_id)
        if resuming:
            cmd += ["resume", session_id]
        cmd += [
            "--json",
            "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            # Mid-turn delivery. All three parts are load-bearing.
            "--dangerously-bypass-hook-trust",
            "-c",
            "features.hooks=true",
            "-c",
            shared.codex_hook_config(ws),
            "-m",
            model,
        ]
        if not resuming:
            cmd += ["-C", ws]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        # Ours per invocation, never written into the owner's config.toml.
        cmd += ["-c", f"model_context_window={REMOTE_CONTEXT_CAP}"]
        # One server, written out directly. There is no loop and no
        # url-versus-command branching.
        if office_url:
            cmd += [
                "-c", f'mcp_servers.office.url="{office_url}"',
                "-c", f"mcp_servers.office.tool_timeout_sec={MCP_TOOL_TIMEOUT_SEC}",
                "-c", f"mcp_servers.office.startup_timeout_sec={MCP_STARTUP_TIMEOUT_SEC}",
            ]
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def encode_message(self, text: str) -> str:
        # Not NDJSON.
        return text + "\n"

    def parse_line(self, line: str) -> Event | None:
        line = line.strip()
        if not line or not line.startswith("{"):
            return None
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            return None

        kind = msg.get("type")

        if kind == "thread.started":
            # The thread id is how poll_context() finds the session file.
            return Event(kind="usage", session_id=msg.get("thread_id"))

        # The envelope gives the phase and the item's own `status` overrules it
        # where that names an end. An unrecognized status leaves the phase as
        # given.
        if kind in ("item.started", "item.updated"):
            status = (msg.get("item") or {}).get("status")
            over = status in ("completed", "failed")
            return _item_event(msg, phase="finished" if over else "started")

        if kind == "item.completed":
            return _item_event(msg, phase="finished")

        if kind == "turn.completed":
            # The real figure comes from poll_context() reading the session
            # file. This branch stays empty.
            return Event(kind="turn_end")

        if kind == "turn.failed":
            return Event(
                kind="turn_end",
                error=str((msg.get("error") or {}).get("message") or msg.get("error")),
            )

        if kind == "error":
            return Event(kind="error", error=str(msg.get("message")))

        return None

    def poll_context(self, session_id: str | None) -> Event | None:
        """Window occupancy and size for a live thread, from its session file.

        `last_token_usage` is the occupancy — NOT `total_token_usage`, which is
        the same cumulative figure `turn.completed` carries.

        Returns None on any reading or parsing failure, which leaves the
        previous figure standing and reports nothing new; it never estimates.

        The file is located once per turn (_rollout_file) and read forward
        from where the last poll stopped (_RolloutTail).
        """
        payload = self._tail.token_count(self._rollout_file(session_id or ""))
        if payload is None:
            return None
        info = payload.get("info") or {}
        used = (info.get("last_token_usage") or {}).get("total_tokens")
        limit = info.get("model_context_window")
        if used is None and limit is None:
            return None
        return Event(
            kind="usage",
            context_used=int(used) if used is not None else None,
            context_limit=int(limit) if limit else None,
        )

    def poll_quota(self, session_ids: Sequence[str] = ()) -> list[QuotaSnapshot] | None:
        """Quota from the office's newest session file. Free: no turn, no tokens.

        Account-wide: any of the office's own threads carries the same figure,
        including the file of a turn running right now.

        **`session_ids` must be the office's own threads and nothing else.**
        The ids come from `agents.session_id` via office/bus.py.

        Returns None when the office has run no codex turn yet: no thread, no
        file, no number.

        Not per-thread state: this does not go through _rollout_file. The
        newest office file is looked up on each round.
        """
        path = _newest_office_session_file(session_ids)
        return _quota((self._tail.token_count(path) or {}).get("rate_limits"))

    def list_models(self) -> list[ModelInfo] | None:
        """`$CODEX_HOME/models_cache.json`. Free, local, and NO process at all.

        Returns None if the file is missing or unreadable — codex not installed,
        or never run. Never a hardcoded list (see ModelInfo).
        """
        try:
            payload = json.loads(models_cache_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict):
            return None
        return parse_models_cache(payload)


def _quota(rate_limits: dict | None) -> list[QuotaSnapshot] | None:
    """`rate_limits` -> buckets, labelled by the window they cover.

    A window codex does not report, or one that is neither, keeps codex's own
    key.
    """
    if not rate_limits:
        return None
    snapshots = []
    for key in ("primary", "secondary"):
        bucket = rate_limits.get(key)
        if not isinstance(bucket, dict):
            continue
        used = bucket.get("used_percent")
        if used is None:
            continue
        reset = bucket.get("resets_at")
        snapshots.append(
            QuotaSnapshot(
                runtime="codex",
                label=quota_window(bucket.get("window_minutes")) or key,
                remaining_fraction=clamp_fraction(1.0 - float(used) / 100.0),
                reset_time=int(reset) if reset else None,
            )
        )
    return snapshots or None


def _item_event(msg: dict, phase: str) -> Event | None:
    item = msg.get("item") or {}
    item_type = item.get("type")
    if item_type == "agent_message":
        # Only once, when it is complete.
        text = item.get("text")
        return Event(kind="text", text=text) if text and phase == "finished" else None
    if item_type in ("reasoning", "todo_list"):
        return None
    if item_type == "error":
        if str(item.get("message") or "").startswith(_HOOK_TRUST_NOTICE):
            # Not a failure; suppressed.
            return None
        # Everything else here renders `message`, in the shape parse_line gives
        # a top-level `error` line: `kind="error"` and NO phase.
        # Every envelope carrying one renders it, rather than the finished one
        # alone.
        return Event(kind="error", error=one_line(item.get("message")))
    summary = _item_summary(item)
    if phase == "finished":
        # Where it says nothing, _failure_reason is asked instead, and the
        # colon is written only once one of the two answers.
        exit_code = item.get("exit_code")
        outcome = f"exit {exit_code}" if exit_code is not None else "done"
        if item.get("status") == "failed":
            outcome = "failed" if exit_code is None else f"failed, exit {exit_code}"
            detail = one_line(item.get("aggregated_output")) or _failure_reason(item)
            if detail:
                outcome = f"{outcome}: {detail}"
        summary = with_outcome(summary, outcome)
    return Event(kind="tool", phase=phase, text=summary)


def _failure_reason(item: dict) -> str | None:
    """Why a failed item failed, in its own words. None when it does not say.

    Two fields are read and either one answers: `error.message`, and the text
    of the blocks under `result.content`.

    `error` wins where both arrive.

    A bare string there is read as the message.

    `content` is read as a LIST, in order, into one subject under the one cap
    every other subject gets: the blocks that fit arrive whole, the block that
    straddles the cap is marked, and what follows it is never read. A block
    with no string `text` contributes nothing, and an item that yields no text
    at all yields None.

    An item carrying neither field lands here and leaves the same way.
    """
    error = item.get("error")
    message = error.get("message") if isinstance(error, dict) else error
    if isinstance(message, str):
        detail = one_line(message)
        if detail:
            return detail
    result = item.get("result")
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None
    pieces: list[str] = []
    budget = TOOL_SUMMARY_CHARS
    for block in content:
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str):
            continue
        # Cut each block to what is still unspent, and stop once nothing is.
        # One character past the budget is all one_line needs to see that
        # there is more and mark it. A block longer than its slice is marked
        # here as well.
        piece = one_line(text[: budget + 1], budget)
        if not piece:
            continue
        if len(text) > budget + 1 and not piece.endswith("…"):
            piece += "…"
        pieces.append(piece)
        budget -= len(piece) + 1
        if budget <= 0:
            break
    return one_line(" ".join(pieces))


def _item_summary(item: dict) -> str | None:
    """What this item is DOING, short enough for one transcript row.

    An MCP call is named `server/tool`.

    A command goes through strip_shell_prefix before tool_summary.
    """
    item_type = item.get("type")
    if item_type == "command_execution":
        return tool_summary(None, strip_shell_prefix(item.get("command"))) or item_type
    if item_type == "mcp_tool_call":
        server, tool = item.get("server"), item.get("tool")
        name = f"{server}/{tool}" if server and tool else (tool or server or item_type)
        return tool_summary(name, identifying_argument(item.get("arguments")))
    if item_type == "file_change":
        # `changes` is the item's own field, not an `arguments` dict.
        return tool_summary(item_type, _changed_files(item.get("changes")))
    return tool_summary(item_type, identifying_argument(item.get("arguments")))


def _changed_files(changes) -> str | None:
    """Which files a `file_change` touched, short enough to sit in its row.

    Basenames.

    Several changed files are one item; the first is named and the rest
    counted.

    None when nothing in there names a path.
    """
    if not isinstance(changes, list):
        return None
    paths = [c.get("path") for c in changes if isinstance(c, dict)]
    names = [
        path.replace("\\", "/").rstrip("/").rpartition("/")[2] or path
        for path in dict.fromkeys(p for p in paths if isinstance(p, str) and p.strip())
    ]
    if not names:
        return None
    return names[0] if len(names) == 1 else f"{names[0]} and {len(names) - 1} more"
