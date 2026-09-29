from __future__ import annotations

import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from office.adapters import shared
from office.adapters.agy import AgyAdapter, reset_seconds
from office.adapters.claude import AUTOCOMPACT, ClaudeAdapter, parse_usage_payload
from office.adapters.codex import CodexAdapter, rollout_requests
from office.process import _QUOTA_SIGNS

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


def test_claude_capture_yields_its_requests_and_its_totals():
    adapter = ClaudeAdapter(bin_path="claude")
    text = (STREAMS / "claude_capture.jsonl").read_text(encoding="utf-8")
    events = [
        event
        for event in (adapter.parse_line(line) for line in text.splitlines() if line.strip())
        if event
    ]

    requests = [event.request for event in events if event.request]
    assert [r["tools"] for r in requests] == [["Write"], []]
    assert [(r["input"], r["cache_read"], r["cache_write"], r["cache_write_1h"], r["output"])
            for r in requests] == [(10, 23733, 0, 0, 3), (8, 23733, 478, 478, 2)]
    assert all(r["at"] and r["model"] for r in requests)

    totals = next(event.totals for event in events if event.kind == "turn_end")
    assert totals == {"input": 18, "cache_read": 47466, "cache_write": 478,
                      "cache_write_1h": 478, "output": 361, "thinking": 201}


def test_codex_rollout_requests_are_the_token_counts_since_the_turn_began():
    lines = (STREAMS / "codex_rollout_capture.jsonl").read_text(encoding="utf-8").splitlines()

    this_turn = rollout_requests(lines, "2026-09-27T21:00:00.000Z")
    assert [(r["input"], r["cache_read"], r["output"], r["thinking"]) for r in this_turn] == [
        (151798 - 8192, 8192, 2628, 2254),
        (154450 - 151680, 151680, 831, 21),
    ]
    assert this_turn[0]["at"] == "2026-09-27T21:53:58.190Z"

    # The count repeated at the end of the earlier turn is one request, not two.
    assert len(rollout_requests(lines, "2026-09-27T17:00:00.000Z")) == 4


def test_a_usage_reset_two_hours_ago_is_read_as_this_years():
    # Constructed from the line shape /usage prints.
    zone = "Europe/Istanbul"
    when = datetime.now(ZoneInfo(zone)) - timedelta(hours=2)
    stamp = (f"{when:%b} {when.day}, {when.hour % 12 or 12}:{when:%M}"
             f"{'am' if when.hour < 12 else 'pm'}")
    text = f"Current session: 16% used · resets {stamp} ({zone})"

    (bucket,) = parse_usage_payload({"result": text})

    assert abs(bucket.reset_time - when.timestamp()) < 60


def test_a_compaction_carries_every_flag_of_a_resumed_turn_but_its_input(tmp_path):
    adapter = ClaudeAdapter(bin_path="claude")
    ws = str(tmp_path)
    adapter.prepare_session(ws, "agent")
    shared.system_prompt_path(ws).write_text("the role", encoding="utf-8")
    session = ("opus", "high", "http://127.0.0.1:7777/mcp/agent/")

    turn = adapter.build_command(ws, *session, "sid", resume=True)
    compaction = adapter.compact_command(ws, *session, "sid")

    for flag, value in (("--input-format", "stream-json"), ("--autocompact", AUTOCOMPACT)):
        at = turn.index(flag)
        assert turn[at + 1] == value
        del turn[at:at + 2]
    compaction.remove("/compact")
    assert sorted(turn) == sorted(compaction)


def test_each_vendors_quota_refusal_names_its_time_of_return():
    now = time.time()

    # agy: the fixture's result line.
    (line,) = (STREAMS / "agy_quota_capture.jsonl").read_text(encoding="utf-8").splitlines()
    refusal = AgyAdapter(bin_path="agy").parse_line(line).error
    assert _QUOTA_SIGNS.search(refusal)
    assert reset_seconds(refusal) == 3 * 3600 + 58 * 60 + 49

    # claude: copied verbatim from a claude session transcript.
    refusal = "You've hit your session limit · resets 12:50am (Europe/Istanbul)"
    assert _QUOTA_SIGNS.search(refusal)
    back = ClaudeAdapter(bin_path="claude").quota_reset(refusal)
    assert now < back <= now + 86400 + 60
    local = datetime.fromtimestamp(back - 60, ZoneInfo("Europe/Istanbul"))
    assert (local.hour, local.minute) == (0, 50)

    # codex: both forms copied verbatim from codex rollout files.
    codex = CodexAdapter(bin_path="codex")
    refusal = (
        "You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), visit "
        "https://chatgpt.com/codex/settings/usage to purchase more credits or try again at "
        "Sep 10th, 2026 1:43 AM."
    )
    assert _QUOTA_SIGNS.search(refusal)
    assert codex.quota_reset(refusal) == int(datetime(2026, 9, 10, 1, 43).timestamp()) + 60
    back = codex.quota_reset(refusal.replace("Sep 10th, 2026 1:43 AM", "3:20 PM"))
    assert now < back <= now + 86400 + 60
    assert (datetime.fromtimestamp(back - 60).hour, datetime.fromtimestamp(back - 60).minute) == (15, 20)
