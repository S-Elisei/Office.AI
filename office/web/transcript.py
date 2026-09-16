"""An agent's output stream, parsed back into something a person can read."""

from __future__ import annotations

STDERR_PREFIX = "stderr: "


def _row(event) -> dict | None:
    """One adapter Event -> one display row, or None when it has nothing to show."""
    if event.kind == "error":
        return {"kind": "error", "phase": None, "text": event.error or event.text or "error"}
    if event.kind == "tool":
        return {"kind": "tool", "phase": event.phase, "text": event.text or "tool call"}
    if event.kind == "compact":
        return {
            "kind": "note",
            "phase": None,
            "text": f"context compacted ({event.text or 'trigger unknown'})",
        }
    if event.kind == "turn_end":
        return {
            "kind": "error" if event.error else "note",
            "phase": None,
            "text": event.error or "turn ended",
        }
    if event.text:
        return {"kind": "text", "phase": None, "text": event.text}
    return None


def rows(lines: list[str], adapter) -> list[dict]:
    """A stretch of an agent's output stream, as something a person can read."""
    out: list[dict] = []
    for line in lines:
        if line.startswith(STDERR_PREFIX):
            out.append({"kind": "error", "phase": None, "text": line[len(STDERR_PREFIX):]})
            continue
        try:
            event = adapter.parse_line(line)
        except Exception:  # noqa: BLE001
            continue
        if event is None:
            continue
        parsed = _row(event)
        if parsed is None:
            continue
        if parsed["kind"] == "text" and out and out[-1]["kind"] == "text":
            out[-1]["text"] += parsed["text"]
            continue
        out.append(parsed)
    return out


ACTIVITY_CHARS = 220


def last_activity(lines: list[str], adapter) -> dict | None:
    """The newest row of the transcript, trimmed to one line. None if there is none."""
    parsed = rows(lines, adapter)
    if not parsed:
        return None
    activity = dict(parsed[-1])
    text = " ".join(activity["text"].split())
    if len(text) > ACTIVITY_CHARS:
        if activity["kind"] == "text":
            text = "…" + text[-(ACTIVITY_CHARS - 1):]
        else:
            text = text[: ACTIVITY_CHARS - 1] + "…"
    activity["text"] = text
    return activity
