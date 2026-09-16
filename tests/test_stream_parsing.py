from __future__ import annotations

from pathlib import Path

from office.adapters.codex import CodexAdapter

STREAMS = Path(__file__).parent / "fixtures" / "streams"


def test_codex_capture_reports_no_context_occupancy_at_all():
    adapter = CodexAdapter(bin_path="codex")
    text = (STREAMS / "codex_capture.jsonl").read_text(encoding="utf-8")
    events = [
        event
        for event in (adapter.parse_line(line) for line in text.splitlines() if line.strip())
        if event
    ]

    assert "turn_end" in [event.kind for event in events]
    assert all(event.context_used is None for event in events)
    assert all(event.context_limit is None for event in events)
    assert next(event for event in events if event.kind == "usage").session_id
