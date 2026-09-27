"""Bus state-machine tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from office import core, db
from office import mcp as office_mcp
from office.adapters import shared
from office.adapters.claude import ClaudeAdapter
import office.bus as busmod
from office.bus import Bus

STUB_SCRIPT = Path(__file__).parent / "fixtures" / "stub_agent.py"
HOOK_SCRIPT = Path(shared.__file__).with_name("inbox_hook.py")

#: How long the stand-in binary stays alive where a turn has to be running
#: while the test does something else.
TURN_SECONDS = 6.0


# --------------------------------------------------------------------------- helpers


def make_agent(
    conn, name, *, kind="executor", manager=None, runtime="claude", model="stub-model", status="idle"
):
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO agents (name, kind, manager_agent_id, runtime, model, status) "
            "VALUES (?, ?, (SELECT id FROM agents WHERE name = ?), ?, ?, ?)",
            (name, kind, manager, runtime, model, status),
        )
    return db.query_one(conn, "SELECT * FROM agents WHERE name = ?", (name,))["id"]


def mark_mid_turn(conn, agent_id):
    """The spawn-time watermark a turn in flight carries."""
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET turn_start_message_id = 0 WHERE id = ?", (agent_id,))


def make_workspace(config, conn, agent_id, ws_id):
    path = config.ws_dir / ws_id
    path.mkdir(parents=True, exist_ok=True)
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO workspaces (id, path, owner_agent_id) VALUES (?, ?, ?)",
            (ws_id, str(path), agent_id),
        )
    return path


def get_agent(conn, name):
    return db.query_one(conn, "SELECT * FROM agents WHERE name = ?", (name,))


def wait_until(predicate, timeout=10.0, interval=0.02) -> bool:
    """Poll for a background thread's effect."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class StubClaudeAdapter(ClaudeAdapter):
    """The real ClaudeAdapter with only the spawned binary swapped out."""

    def __init__(self, dump_path=None, context_pct=5.0, sleep=0.0):
        super().__init__(sys.executable)
        self.dump_path = dump_path
        self.context_pct = context_pct
        self.sleep = sleep

    def build_command(self, ws, model, effort, office_url, session_id, resume=False):
        cmd = [self.bin_path, str(STUB_SCRIPT), "--context-pct", str(self.context_pct)]
        if self.dump_path:
            cmd += ["--dump", str(self.dump_path)]
        if self.sleep:
            cmd += ["--sleep", str(self.sleep)]
        return cmd


def finish_turn(bus: Bus, name: str) -> None:
    """Block until a turn started by _start_turn (and its _watch thread) is done."""
    state = bus._state(name)
    turn = state.turn
    assert turn is not None, "no turn was started"
    assert turn.wait(timeout=30), "stub process did not finish in time"
    assert wait_until(lambda: state.turn is None), "_watch thread never cleared state.turn"


def start_turn(bus: Bus, conn, name: str):
    """Raise a turn on the agent row as it stands, and hand back the process."""
    agent = dict(get_agent(conn, name))
    assert bus._start_turn(agent) is True
    turn = bus._state(name).turn
    assert turn is not None and turn.alive, "no process is running for this turn"
    return turn


def run_inbox_hook(ws_path, identity: str):
    """The hook script, run the way the runtime's hook command runs it."""
    proc = subprocess.run(
        [sys.executable, str(HOOK_SCRIPT), "claude", str(ws_path)],
        env={**os.environ, "OFFICE_AGENT": identity},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return proc


def tick_until(bus: Bus, ready, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        bus._tick()
        if ready():
            return True
        time.sleep(0.05)
    return ready()


# --------------------------------------------------------------------------- delivery: piggyback


def test_the_common_chat_reaches_a_running_turn_through_the_tool_drain(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-1")
    bus = Bus(conn, config)
    monkeypatch.setattr(
        busmod,
        "adapter_for",
        lambda runtime, cfg: StubClaudeAdapter(
            dump_path=tmp_path / "dump.txt", sleep=TURN_SECONDS
        ),
    )

    core.send_message(conn, "director", "exec1", "the first errand")
    start_turn(bus, conn, "exec1")

    core.send_message(conn, "director", "all", "a word to the room")
    bus._tick()

    first = bus.piggyback("exec1")
    assert first is not None
    assert "a word to the room" in first
    assert bus.piggyback("exec1") is None

    finish_turn(bus, "exec1")


# --------------------------------------------------------------------------- delivery: hook


def test_a_message_the_hook_took_mid_turn_is_not_handed_over_again(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    ws_path = make_workspace(config, conn, agent_id, "ws-2")
    bus = Bus(conn, config)

    first_dump = tmp_path / "first.txt"
    monkeypatch.setattr(
        busmod,
        "adapter_for",
        lambda runtime, cfg: StubClaudeAdapter(dump_path=first_dump, sleep=TURN_SECONDS),
    )
    core.send_message(conn, "director", "exec1", "the first errand")
    start_turn(bus, conn, "exec1")

    core.send_message(conn, "director", "exec1", "the errand that arrived mid-turn")
    bus._tick()

    stranger = run_inbox_hook(ws_path, "someone-else")
    assert stranger.stdout.strip() == ""

    taken = run_inbox_hook(ws_path, "exec1")
    handed_over = json.loads(taken.stdout)["hookSpecificOutput"]["additionalContext"]
    assert "the errand that arrived mid-turn" in handed_over

    finish_turn(bus, "exec1")

    second_dump = tmp_path / "second.txt"
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=second_dump)
    )
    core.send_message(conn, "director", "exec1", "the errand for the next turn")
    start_turn(bus, conn, "exec1")
    finish_turn(bus, "exec1")

    prompt = second_dump.read_text(encoding="utf-8")
    assert "the errand for the next turn" in prompt
    assert "the errand that arrived mid-turn" not in prompt


# --------------------------------------------------------------------------- delivery: waiting


def test_a_message_for_an_agent_with_no_workspace_is_kept_for_its_first_turn(
    conn, config, monkeypatch, tmp_path
):
    make_agent(conn, "exec1")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump)
    )

    core.send_message(conn, "director", "exec1", "the standing order")

    assert not tick_until(bus, lambda: dump.exists(), busmod.COALESCE_SECONDS * 2)

    make_workspace(config, conn, get_agent(conn, "exec1")["id"], "ws-late")

    assert tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 4
    )
    finish_turn(bus, "exec1")

    assert "the standing order" in dump.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- delivery: turn


def test_turn_drain_spawns_a_real_process_and_delivers_the_message(conn, config, monkeypatch, tmp_path):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-3")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump))

    message_id = core.send_message(conn, "director", "exec1", "turn message")["id"]

    agent = dict(get_agent(conn, "exec1"))
    assert bus._start_turn(agent) is True
    finish_turn(bus, "exec1")

    assert "turn message" in dump.read_text(encoding="utf-8")

    updated = get_agent(conn, "exec1")
    assert updated["session_id"]
    assert (updated["last_seen_message_id"] or 0) >= message_id


# --------------------------------------------------------------------------- a service's message lost with a turn


def test_a_service_message_lost_with_a_stopped_turn_rides_the_next_turn_and_buys_none(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-lost")
    make_agent(conn, "director1", kind="director")
    bus = Bus(conn, config)
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(sleep=TURN_SECONDS)
    )

    expectation = core.open_expectation(conn, "exec1", "the first job", 3600)
    core.answer_expectation(conn, expectation["token"], "assetfactory", "job j-1 succeeded")
    start_turn(bus, conn, "exec1")
    assert bus.stop_agent("exec1", "director1")["ok"]
    finish_turn(bus, "exec1")

    assert not tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 2
    )

    # Control: a direct message does buy a turn from the same ticks.
    dump = tmp_path / "next.txt"
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump)
    )
    core.send_message(conn, "director1", "exec1", "carry on")
    assert tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 4
    )
    finish_turn(bus, "exec1")

    prompt = dump.read_text(encoding="utf-8")
    assert "job j-1 succeeded" in prompt
    assert "assetfactory" in prompt


# --------------------------------------------------------------------------- expectations


def test_an_expectation_nobody_answers_wakes_its_agent(conn, config, monkeypatch, tmp_path):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-expect")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump)
    )

    # Control: an expectation still inside its time wakes nobody.
    core.open_expectation(conn, "exec1", "the long render", 3600)
    assert not tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 2
    )

    core.open_expectation(conn, "exec1", "the portrait batch", 0)
    assert tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 4
    )
    finish_turn(bus, "exec1")

    assert "the portrait batch" in dump.read_text(encoding="utf-8")


def test_a_remind_or_an_expectation_set_in_a_turn_is_speech_for_that_turn_only(
    conn, config, monkeypatch
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-speech")
    make_agent(conn, "director1", kind="director")
    bus = Bus(conn, config)
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(sleep=TURN_SECONDS)
    )

    def office_messages_to(name):
        return db.query_one(
            conn,
            "SELECT COUNT(*) AS n FROM messages WHERE channel = 'dm' AND sender = ? "
            "AND recipient = ?",
            (core.OFFICE_SENDER, name),
        )["n"]

    def a_turn(during):
        core.send_message(conn, "director1", "exec1", "the next step")
        start_turn(bus, conn, "exec1")
        during()
        finish_turn(bus, "exec1")

    a_turn(lambda: office_mcp._h_expect(
        conn, config, "exec1", "executor", {"about": "the batch", "within_seconds": 3600}
    ))
    assert office_messages_to("exec1") == 0

    # Control: a turn that does neither is nudged, the expectation still open from
    # the turn before notwithstanding.
    a_turn(lambda: None)
    assert office_messages_to("exec1") == 1

    a_turn(lambda: office_mcp._h_remind(
        conn, config, "exec1", "executor", {"to": "exec1", "text": "look again", "in_seconds": 3600}
    ))
    assert office_messages_to("exec1") == 1
    assert office_messages_to("director1") == 0


# --------------------------------------------------------------------------- no double delivery


def test_no_double_delivery_when_hook_and_turn_claims_race(config):
    ws = config.ws_dir / "ws-race"
    ws.mkdir(parents=True)
    shared.claim_owner(ws, "agent1")

    for i in range(20):
        shared.deliver(ws, f"msg-{i}", "agent1")
        proc_holder: list[subprocess.Popen | None] = [None]

        def start_hook() -> None:
            proc_holder[0] = subprocess.Popen(
                [sys.executable, str(HOOK_SCRIPT), "claude", str(ws)],
                env={**os.environ, "OFFICE_AGENT": "agent1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )

        t = threading.Thread(target=start_hook)
        t.start()
        bus_side = shared.claim_inbox(ws, "agent1")
        t.join()
        out, _err = proc_holder[0].communicate(timeout=10)

        hook_side = None
        if out.strip():
            hook_side = json.loads(out)["hookSpecificOutput"]["additionalContext"]

        winners = [w for w in (bus_side, hook_side) if w]
        assert len(winners) == 1, f"trial {i}: expected exactly one winner, got bus={bus_side!r} hook={hook_side!r}"
        assert f"msg-{i}" in winners[0]


# --------------------------------------------------------------------------- coalescing


def test_coalescing_batches_a_burst_into_one_turn(conn, config, monkeypatch, tmp_path):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-4")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump))

    core.send_message(conn, "director", "exec1", "first")
    core.send_message(conn, "director", "exec1", "second")
    core.send_message(conn, "director", "exec1", "third")

    bus._tick()
    state = bus._state("exec1")
    assert state.owes_turn is True
    assert state.turn is None

    state.drain_at = time.monotonic() - 1
    bus._tick()
    assert state.turn is not None
    finish_turn(bus, "exec1")

    prompt = dump.read_text(encoding="utf-8")
    assert "first" in prompt and "second" in prompt and "third" in prompt


# --------------------------------------------------------------------------- circuit breaker


def test_fuse_collapses_wakeups_over_the_cap(conn, config):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-5")
    bus = Bus(conn, config)
    message_id = core.send_message(conn, "director", "exec1", "one more")["id"]

    state = bus._state("exec1")
    state.wake_times = [time.monotonic()] * busmod.FUSE_TURNS

    agent = dict(get_agent(conn, "exec1"))
    assert bus._start_turn(agent) is False
    assert state.turn is None

    updated = get_agent(conn, "exec1")
    assert (updated["last_seen_message_id"] or 0) < message_id


# --------------------------------------------------------------------------- fresh vs continuing


def test_fresh_session_gets_snapshot_continuing_session_gets_delta_only(conn, config, monkeypatch, tmp_path):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-6")
    bus = Bus(conn, config)

    dump1 = tmp_path / "dump1.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump1))
    core.send_message(conn, "director", "exec1", "turn one")

    agent = dict(get_agent(conn, "exec1"))
    assert agent["session_id"] is None
    assert bus._start_turn(agent) is True
    finish_turn(bus, "exec1")

    prompt1 = dump1.read_text(encoding="utf-8")
    assert "New session" in prompt1
    assert "turn one" in prompt1

    agent2 = dict(get_agent(conn, "exec1"))
    assert agent2["session_id"]

    dump2 = tmp_path / "dump2.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump2))
    core.send_message(conn, "director", "exec1", "turn two")

    assert bus._start_turn(agent2) is True
    finish_turn(bus, "exec1")

    prompt2 = dump2.read_text(encoding="utf-8")
    assert "New session" not in prompt2
    assert "turn two" in prompt2


# --------------------------------------------------------------------------- restart path


def test_restart_recovery_fails_only_the_work_that_had_a_turn_on_it(conn, config):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1", status="running")
    waiting_id = make_agent(conn, "exec2", manager="director1", status="idle")
    core.assign_work(
        conn, agent_id=exec_id, brief="refactor the widget", task_id=None, branch="feature-x",
        actor="director1",
    )
    core.assign_work(
        conn, agent_id=waiting_id, brief="waiting on an answer", task_id=None, branch="feature-y",
        actor="director1",
    )
    mark_mid_turn(conn, exec_id)

    bus = Bus(conn, config)
    bus._wakeup.clear()
    bus.recover_after_restart()

    work = db.query_one(conn, "SELECT * FROM works WHERE agent_id = ?", (exec_id,))
    assert work["status"] == "failed"
    assert work["fail_reason"] == "hub_restart"

    untouched = db.query_one(conn, "SELECT * FROM works WHERE agent_id = ?", (waiting_id,))
    assert untouched["status"] == "running"
    assert untouched["fail_reason"] is None

    recovered_exec = db.query_one(conn, "SELECT * FROM agents WHERE id = ?", (exec_id,))
    assert recovered_exec["status"] == "idle"

    delta = db.query_one(
        conn, "SELECT * FROM messages WHERE recipient = 'director1' ORDER BY id DESC"
    )
    assert delta is not None
    assert "hub_restart" in delta["body"]
    assert "refactor the widget" in delta["body"]
    assert "waiting on an answer" not in delta["body"]

    assert bus._wakeup.is_set()


def test_restart_recovery_with_nothing_interrupted_still_wakes_the_director(conn, config):
    make_agent(conn, "director1", kind="director")

    bus = Bus(conn, config)
    bus._wakeup.clear()
    bus.recover_after_restart()

    delta = db.query_one(
        conn, "SELECT * FROM messages WHERE recipient = 'director1' ORDER BY id DESC"
    )
    assert delta is not None
    assert "Nothing was in progress" in delta["body"]
    assert bus._wakeup.is_set()




# --------------------------------------------------------------------------- who hears of a work


def test_a_report_reaches_its_assigner_and_a_restart_tells_the_assigner_or_the_self_assigners_manager(
    conn, config
):
    make_agent(conn, "director1", kind="director")
    make_agent(conn, "upper", kind="lead", manager="director1")
    lower = make_agent(conn, "lower", kind="lead", manager="upper")
    hand = make_agent(conn, "hand", manager="lower")
    reported = core.assign_work(
        conn, agent_id=hand, brief="paint the fence", task_id=None, branch="fence",
        actor="lower",
    )

    def last_from(sender, recipient):
        row = db.query_one(
            conn,
            "SELECT body FROM messages WHERE channel = 'dm' AND sender = ? AND recipient = ? "
            "ORDER BY id DESC",
            (sender, recipient),
        )
        return row["body"] if row is not None else None

    core.finish_work(conn, reported["id"], summary="fence painted", actor="hand")
    assert last_from("hand", "lower") == "fence painted"
    assert last_from("hand", "director1") is None
    core.close_work(conn, reported["id"], actor="lower")

    core.assign_work(
        conn, agent_id=hand, brief="mend the gate", task_id=None, branch="gate", actor="lower"
    )
    core.assign_work(
        conn, agent_id=lower, brief="plan the garden", task_id=None, branch="garden",
        actor="lower",
    )
    mark_mid_turn(conn, hand)
    mark_mid_turn(conn, lower)

    Bus(conn, config).recover_after_restart()

    to_lower = last_from(core.OFFICE_SENDER, "lower")
    assert "mend the gate" in to_lower
    assert "plan the garden" not in to_lower

    to_upper = last_from(core.OFFICE_SENDER, "upper")
    assert "plan the garden" in to_upper
    assert "These leads of yours were in a turn, which died with the hub: lower." in to_upper
    assert "mend the gate" not in to_upper

    to_director = last_from(core.OFFICE_SENDER, "director1")
    assert "mend the gate" in to_director and "plan the garden" in to_director
    assert last_from(core.OFFICE_SENDER, "hand") is None
