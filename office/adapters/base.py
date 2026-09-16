"""Shared vendor-adapter vocabulary: the normalized event and quota records."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class QuotaSnapshot:
    """One quota bucket, under the office's own name for its window.

    `remaining_fraction` is what is left.
    `reset_time` is epoch seconds, UTC.
    """

    runtime: str  # "claude" | "codex" | "agy"
    label: str
    remaining_fraction: float
    reset_time: int | None = None


QUOTA_5H = "5h"
QUOTA_WEEK = "week"

# A window outside both ranges is not named at all.
_WINDOWS = ((240.0, 480.0, QUOTA_5H), (5.0 * 1440, 9.0 * 1440, QUOTA_WEEK))

# Words that name a window unambiguously in a label.
_LABEL_WORDS = (
    (QUOTA_5H, ("five hour", "five-hour", "5 hour", "5-hour", "session")),
    (QUOTA_WEEK, ("weekly", "week", "7 day", "7-day", "seven day")),
)


def quota_window(minutes: float | int | None) -> str | None:
    """The office's name for a window given in minutes, or None for neither."""
    if minutes is None:
        return None
    try:
        value = float(minutes)
    except (TypeError, ValueError):
        return None
    for low, high, name in _WINDOWS:
        if low <= value <= high:
            return name
    return None


def _read_label(label: str) -> tuple[str | None, str]:
    """Split a vendor label into (window name or None, qualifier)."""
    text = " ".join(label.split())
    qualifier = ""
    if "/" in text:
        head, _, tail = text.partition("/")
        qualifier, text = head.strip(), tail.strip()
    elif "(" in text and text.rstrip().endswith(")"):
        head, _, tail = text.partition("(")
        qualifier, text = tail.rstrip()[:-1].strip(), head.strip()
    lowered = text.lower()
    for name, words in _LABEL_WORDS:
        if any(word in lowered for word in words):
            return name, qualifier
    return None, ""


def name_quota_windows(snapshots: list[QuotaSnapshot]) -> list[QuotaSnapshot]:
    """Rename one poll's buckets to the office's window names. Anything unrecognised is returned exactly as it came.

    Takes the whole poll rather than one bucket. Idempotent.
    """
    named: list[tuple[QuotaSnapshot, str | None, str]] = []
    for snapshot in snapshots:
        window, qualifier = _read_label(snapshot.label)
        named.append((snapshot, window, qualifier))

    counts: dict[str, int] = {}
    for _, window, _q in named:
        if window:
            counts[window] = counts.get(window, 0) + 1

    out: list[QuotaSnapshot] = []
    for snapshot, window, qualifier in named:
        if window is None:
            out.append(snapshot)
            continue
        label = f"{window} ({qualifier})" if qualifier and counts[window] > 1 else window
        out.append(
            QuotaSnapshot(
                runtime=snapshot.runtime,
                label=label,
                remaining_fraction=snapshot.remaining_fraction,
                reset_time=snapshot.reset_time,
            )
        )
    return out


@dataclass
class ModelInfo:
    """One model a runtime will accept.

    `model_id` is the string that goes on the command line verbatim.
    Every adapter's list_models() returns None on any failure, and never a hardcoded list.
    None and an empty list mean different things: None is "we could not ask", [] is "we asked and the answer was nothing".
    `efforts` is None when the vendor does not say. Nothing downstream may turn None into an assumption.
    """

    runtime: str  # "claude" | "codex" | "agy"
    model_id: str
    display_name: str | None = None
    efforts: tuple[str, ...] | None = None


@dataclass
class Event:
    kind: str  # "text" | "tool" | "usage" | "turn_end" | "error" | "compact"
    text: str | None = None
    session_id: str | None = None
    context_used: int | None = None
    context_limit: int | None = None
    # Set on a "compact" event: what the window held before the summary.
    context_before: int | None = None
    error: str | None = None
    # "started" | "finished" on a `tool` event, None everywhere else.
    # Every way a call can end is "finished", a failure included.
    phase: str | None = None


def clamp_fraction(value: float) -> float:
    return min(1.0, max(0.0, value))


# How much of a tool's subject reaches `Event.text`.
# Truncation happens here, in the adapters.
TOOL_SUMMARY_CHARS = 200

# Argument names that identify WHICH invocation this is, in preference order.
_IDENTIFYING_KEYS = ("command", "CommandLine", "file_path", "path", "url", "pattern", "query")


def one_line(text, limit: int = TOOL_SUMMARY_CHARS) -> str | None:
    """Collapse to a single line and cap the length. None stays None."""
    if text is None:
        return None
    collapsed = " ".join(str(text).split())
    if not collapsed:
        return None
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "…"


def identifying_argument(args) -> str | None:
    """The one argument worth showing beside a tool's name, or None.

    Tries the names in _IDENTIFYING_KEYS in order, then falls back to the first
    non-empty string argument. Values that are not strings yield None.
    """
    if not isinstance(args, dict):
        return None
    for key in _IDENTIFYING_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for value in args.values():
        if isinstance(value, str) and value.strip():
            return value
    return None


# Matched on the basename of the launcher, .exe removed.
_SHELL_NAMES = frozenset(("pwsh", "powershell", "cmd", "bash", "sh", "zsh"))

# The flags that mean "the rest of this is the command".
_SHELL_COMMAND_FLAGS = frozenset(("-command", "-c", "-lc", "-ic", "/c"))


def strip_shell_prefix(command):
    """`"…\\pwsh.exe" -Command git status` -> `git status`. Anything else, untouched."""
    if not isinstance(command, str):
        return command
    text = command.strip()
    if not text:
        return command
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        if end < 0:
            return command
        exe, rest = text[1:end], text[end + 1:]
    else:
        parts = text.split(None, 1)
        exe, rest = parts[0], parts[1] if len(parts) > 1 else ""
    name = exe.replace("\\", "/").rpartition("/")[2].lower()
    if name.removesuffix(".exe") not in _SHELL_NAMES:
        return command
    while rest:
        parts = rest.split(None, 1)
        word, rest = parts[0], parts[1] if len(parts) > 1 else ""
        if word.lower() in _SHELL_COMMAND_FLAGS:
            return rest or command
        if not word.startswith("-"):
            break
    return command


def with_outcome(summary, outcome) -> str | None:
    """Append how a tool ended to its summary.

    Nothing parses it back out, and nothing may.
    """
    if not summary:
        return one_line(outcome)
    return f"{summary} — {outcome}" if outcome else summary


def tool_summary(name, subject=None) -> str | None:
    """"<name> <subject>", one line, capped. The text of every tool event."""
    name_part = one_line(name, 80)
    subject_part = one_line(subject)
    if name_part and subject_part:
        return one_line(f"{name_part} {subject_part}", TOOL_SUMMARY_CHARS + 80)
    return name_part or subject_part


def window_occupancy(prompt_tokens, output_tokens) -> int | None:
    """How full the window is after a round: what the NEXT request will carry.

    `prompt_tokens` is everything that round's request carried, cache fields
    included. `output_tokens` is what the model produced in that round.
    Reasoning and thinking tokens are NOT added.
    """
    total = int(prompt_tokens or 0) + int(output_tokens or 0)
    return total or None


# Denominators of last resort, only for when nothing better is available yet.
# Keyed by MODEL, not by runtime.
# Matched as substrings.
_CONTEXT_LIMITS = {
    "claude": {"haiku": 200_000, "sonnet": 1_000_000, "opus": 1_000_000},
    "agy": {"gemini": 1_048_576},
}


def context_limit_for(runtime: str, model: str | None = None) -> int | None:
    """Denominator of last resort, until the CLI reports the real one.

    An unknown model gets the smallest window its runtime offers.
    """
    table = _CONTEXT_LIMITS.get(runtime)
    if not table:
        return None
    if model:
        lowered = model.lower()
        for key, limit in table.items():
            if key in lowered:
                return limit
    return min(table.values())
