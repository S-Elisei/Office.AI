from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from office import core, wallets
from office.adapters import shared
from office.adapters.agy import AgyAdapter, parse_models_output, reset_seconds
from office.adapters.agy import parse_usage_payload as parse_agy_usage
from office.adapters.base import name_quota_windows
from office.adapters.claude import AUTOCOMPACT, ClaudeAdapter, parse_usage_payload, session_requests
from office.adapters.codex import CodexAdapter, rollout_requests
from office.process import _QUOTA_SIGNS, AgentTurn

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
            for r in requests] == [(10, 23733, 0, 0, None), (8, 23733, 478, 478, None)]
    assert all(r["at"] and r["model"] for r in requests)

    totals = next(event.totals for event in events if event.kind == "turn_end")
    assert totals == {"input": 18, "cache_read": 47466, "cache_write": 478,
                      "cache_write_1h": 478, "output": 361, "thinking": 201}


def test_a_claude_turn_keeps_the_quota_its_stream_reports():
    adapter = ClaudeAdapter(bin_path="claude")
    turn = AgentTurn(adapter, [], ".", "", "exec1")
    for line in (STREAMS / "claude_capture.jsonl").read_text(encoding="utf-8").splitlines():
        event = adapter.parse_line(line)
        if event is not None:
            turn._absorb(event)

    assert [(q.label, round(q.remaining_fraction, 2), q.reset_time) for q in turn.quota] == [
        ("5h", 0.69, 1789144800), ("week (all models)", 0.32, 1789293600)
    ]


def test_agy_turn_totals_are_the_sums_of_its_requests_and_not_the_results_usage():
    adapter = AgyAdapter(bin_path="agy")
    text = (STREAMS / "agy_tool_error_capture.jsonl").read_text(encoding="utf-8")
    events = [
        event
        for event in (adapter.parse_line(line) for line in text.splitlines() if line.strip())
        if event
    ]

    requests = [event.request for event in events if event.request]
    assert len(requests) == 3
    assert adapter.turn_totals(requests) == {
        "input": 14544 + 2525 + 2695, "cache_read": 12193 + 12186, "output": 40 + 67 + 1,
        "thinking": 0,
    }
    assert next(event for event in events if event.kind == "turn_end").totals is None


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


def test_codex_poll_reports_the_reasoning_items_appended_since_the_last_look(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    thread = "00000000-0000-4000-8000-000000000000"
    day = tmp_path / "sessions" / datetime.now().strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    rollout = day / f"rollout-2026-10-04T13-45-08-{thread}.jsonl"
    reasoning = (STREAMS / "codex_rollout_reasoning_capture.jsonl").read_text(encoding="utf-8")
    history = (STREAMS / "codex_rollout_capture.jsonl").read_text(encoding="utf-8")
    rollout.write_text(reasoning + history, encoding="utf-8")
    adapter = CodexAdapter(bin_path="codex")

    # What the file held before the first look belongs to earlier turns.
    assert adapter.poll_context(thread).reasoning == 0

    with rollout.open("a", encoding="utf-8") as handle:
        handle.write(reasoning)
    assert adapter.poll_context(thread).reasoning == 2
    assert adapter.poll_context(thread).reasoning == 0


def _token_count_line(stamp: str, week_used_percent: float) -> str:
    # Constructed from the field names on record: the top-level `timestamp`,
    # `payload.type`, and `payload.rate_limits.primary` / `.secondary` with
    # `used_percent`, `window_minutes` and `resets_at`.
    return json.dumps({
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": {"used_percent": 5.0, "window_minutes": 300, "resets_at": 1790543283},
                "secondary": {
                    "used_percent": week_used_percent, "window_minutes": 10080, "resets_at": 1791079503
                },
            },
        },
    }) + "\n"


def test_codex_quota_is_the_reading_stamped_latest_not_the_one_in_the_newest_file(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    day = tmp_path / "sessions" / datetime.now().strftime("%Y/%m/%d")
    day.mkdir(parents=True)
    latest, earlier, unstamped = (f"00000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
    (day / f"rollout-2026-10-04T13-45-08-{latest}.jsonl").write_text(
        _token_count_line("2026-10-04T14:00:00.000Z", 40.0), encoding="utf-8"
    )
    newest_file = day / f"rollout-2026-10-04T13-45-09-{earlier}.jsonl"
    newest_file.write_text(_token_count_line("2026-10-04T13:50:00.000Z", 30.0), encoding="utf-8")
    os.utime(newest_file, (time.time() + 60, time.time() + 60))
    bare = json.loads(_token_count_line("2026-10-04T15:00:00.000Z", 10.0))
    del bare["timestamp"]
    (day / f"rollout-2026-10-04T13-45-10-{unstamped}.jsonl").write_text(json.dumps(bare) + "\n", encoding="utf-8")

    polled = CodexAdapter(bin_path="codex").poll_quota([earlier, latest, unstamped])

    assert [(q.label, round(q.remaining_fraction, 2), q.read_at) for q in polled] == [
        ("5h", 0.95, "2026-10-04T14:00:00.000Z"),
        ("week", 0.6, "2026-10-04T14:00:00.000Z"),
    ]
    assert CodexAdapter(bin_path="codex").poll_quota([unstamped]) is None


def test_claude_session_requests_carry_the_output_the_stream_does_not():
    lines = (STREAMS / "claude_session_capture.jsonl").read_text(encoding="utf-8").splitlines()

    requests = session_requests(lines, "2026-10-03T19:59:08.000Z")
    assert [(r["key"], r["tools"], r["output"], r["thinking"], r["cache_read"]) for r in requests] == [
        ("REDACTED-1", ["Bash"], 144, 64, 0),
        ("REDACTED-2", [], 80, 31, 33697),
    ]
    assert requests[0]["at"] == "2026-10-03T19:59:08.362Z"

    assert [r["key"] for r in session_requests(lines, "2026-10-03T19:59:12.000Z")] == ["REDACTED-2"]


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
    turn.remove("--disable-slash-commands")
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


def test_a_turn_keeps_the_tool_calls_the_vendor_started_and_has_not_ended():
    # claude: constructed from field names on record. Two calls in one message, their
    # results in two.
    claude = [
        '{"type":"assistant","message":{"content":['
        '{"type":"tool_use","id":"toolu_a","name":"Bash","input":{"command":"ls"}},'
        '{"type":"tool_use","id":"toolu_b","name":"Read","input":{"file_path":"x"}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_a"}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":"toolu_b"}]}}',
    ]
    captures = [
        (ClaudeAdapter(bin_path="claude"), claude, 2),
        (
            CodexAdapter(bin_path="codex"),
            (STREAMS / "codex_failed_mcp_tool_capture.jsonl").read_text(encoding="utf-8").splitlines(),
            1,
        ),
        (
            AgyAdapter(bin_path="agy"),
            (STREAMS / "agy_failing_command_capture.jsonl").read_text(encoding="utf-8").splitlines(),
            1,
        ),
    ]
    for adapter, lines, peak in captures:
        turn = AgentTurn(adapter, [], ".", "", "exec1")
        counts = []
        for line in lines:
            event = adapter.parse_line(line)
            if event is not None:
                turn._absorb(event)
                counts.append(len(turn.open_calls()))
        assert max(counts) == peak
        assert counts[-1] == 0


def test_the_agy_usage_capture_yields_the_buckets_the_agy_wallets_read():
    payload = json.loads((STREAMS / "agy_usage_capture.json").read_text(encoding="utf-8"))

    named = {s.label: s for s in name_quota_windows(parse_agy_usage(payload))}

    gemini, argon = wallets.BY_ID["agy"], wallets.BY_ID["argon"]
    assert set(named) == {gemini.week_bucket, gemini.window_bucket, argon.week_bucket}
    assert named[gemini.week_bucket].remaining_fraction == pytest.approx(0.3254393935203552)
    assert named[gemini.window_bucket].remaining_fraction == pytest.approx(0.9863394498825073)
    assert named[argon.week_bucket].remaining_fraction == pytest.approx(0.9898666739463806)


def test_the_agy_model_catalogue_puts_the_argon_models_on_the_argon_wallet(conn):
    models = parse_models_output((STREAMS / "agy_models_capture.txt").read_text(encoding="utf-8"))
    core.record_models(conn, "agy", models)

    drawn = {m["model_id"]: wallets.of_model(conn, "agy", m["model_id"]).id for m in core.list_models(conn, "agy")}

    assert {model for model, wallet in drawn.items() if wallet == "argon"} == {
        "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high",
    }
    assert {model for model, wallet in drawn.items() if wallet == "agy"} == {
        "gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
    }
