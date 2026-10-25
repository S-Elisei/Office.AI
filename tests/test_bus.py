"""Bus state-machine tests."""

from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from office import core, db, wallets
from office.adapters import shared
from office.adapters.claude import ClaudeAdapter
import office.bus as busmod
import office.mcp as office_mcp
from office.bus import Bus
from test_mcp_surface import answer_to, look_at_roster

STUB_SCRIPT = Path(__file__).parent / "fixtures" / "stub_agent.py"
HOOK_SCRIPT = Path(shared.__file__).with_name("inbox_hook.py")

#: How long the stand-in binary stays alive where a turn has to be running
#: while the test does something else.
TURN_SECONDS = 6.0


# --------------------------------------------------------------------------- helpers


def make_agent(
    conn, name, *, kind="executor", manager=None, runtime="claude", model="stub-model",
    status="idle", role=None
):
    role = role or ("coding" if kind == "executor" else None)
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO agents (name, kind, manager_agent_id, role, runtime, model, status) "
            "VALUES (?, ?, (SELECT id FROM agents WHERE name = ?), ?, ?, ?, ?)",
            (name, kind, manager, role, runtime, model, status),
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

    def __init__(self, dump_path=None, context_pct=5.0, sleep=0.0, die=False):
        super().__init__(sys.executable)
        self.dump_path = dump_path
        self.context_pct = context_pct
        self.sleep = sleep
        self.die = die

    def build_command(self, ws, model, effort, office_url, session_id, resume=False):
        cmd = [self.bin_path, str(STUB_SCRIPT), "--context-pct", str(self.context_pct)]
        if self.dump_path:
            cmd += ["--dump", str(self.dump_path)]
        if self.sleep:
            cmd += ["--sleep", str(self.sleep)]
        if self.die:
            cmd += ["--die"]
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
    """The hook script, run the way the runtime's hook command runs it, by an
    interpreter that cannot import the office package."""
    proc = subprocess.run(
        [sys.executable, "-S", "-E", str(HOOK_SCRIPT), "claude", str(ws_path)],
        cwd=str(ws_path),
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


# --------------------------------------------------------------------------- delivery: the pending queue


def test_of_several_announcements_of_one_rule_only_the_latest_is_handed_over(conn, config):
    make_agent(conn, "director", kind="director")
    make_agent(conn, "exec1", manager="director")
    rule = core.create_rule(conn, "the first wording", "director", "Pace")
    core.create_rule(conn, "another rule's wording", "director", "Other")
    core.update_rule(conn, rule["id"], "the second wording", actor="director")
    core.update_rule(conn, rule["id"], "the third wording", actor="director")

    pending = Bus(conn, config)._undelivered(dict(get_agent(conn, "exec1")))

    handed_over = pending.render()
    assert "the third wording" in handed_over
    assert "the first wording" not in handed_over
    assert "the second wording" not in handed_over
    assert "another rule's wording" in handed_over
    assert pending.quiet_count == 2


def test_an_executors_system_prompt_holds_the_rules_for_everyone_executors_and_its_own_role_only(
    conn, config
):
    make_agent(conn, "director1", kind="director")
    make_agent(conn, "lead1", kind="lead", manager="director1")
    make_agent(conn, "hand", manager="lead1", role="coding")
    for audience in ("everyone", "managers", "executors", "role:coding", "role:review"):
        core.create_rule(conn, f"text-for-{audience}", "director1", f"Title {audience}", audience)

    prompt = Bus(conn, config)._system_prompt(dict(get_agent(conn, "hand")))

    for audience in ("everyone", "executors", "role:coding"):
        assert f"text-for-{audience}" in prompt
    for audience in ("managers", "role:review"):
        assert f"text-for-{audience}" not in prompt


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


def test_a_message_left_in_the_inbox_when_a_turn_ends_is_answered_for_by_the_turn_that_takes_it(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    ws_path = make_workspace(config, conn, agent_id, "ws-left")
    make_agent(conn, "director1", kind="director")
    bus = Bus(conn, config)
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(sleep=TURN_SECONDS)
    )

    core.send_message(conn, "director1", "exec1", "the first errand")
    start_turn(bus, conn, "exec1")
    core.send_message(conn, "director1", "exec1", "the errand no hook took")
    bus._tick()
    assert shared.inbox_path(ws_path).exists()
    finish_turn(bus, "exec1")

    dump = tmp_path / "second.txt"
    monkeypatch.setattr(
        busmod, "adapter_for",
        lambda runtime, cfg: StubClaudeAdapter(dump_path=dump, sleep=TURN_SECONDS),
    )
    start_turn(bus, conn, "exec1")
    assert wait_until(
        lambda: dump.exists() and "the errand no hook took" in dump.read_text(encoding="utf-8")
    )
    assert bus.stop_agent("exec1", core.get_owner_name(conn))["ok"]
    finish_turn(bus, "exec1")

    notice = db.query_one(
        conn,
        "SELECT body FROM messages WHERE sender = ? AND recipient = 'director1' ORDER BY id DESC",
        (core.OFFICE_SENDER,),
    )["body"]
    assert "the errand no hook took" in notice
    assert "the first errand" not in notice


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


def test_a_turn_leaves_one_usage_row_with_its_requests_and_who_woke_it(
    conn, config, monkeypatch
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-usage")
    make_agent(conn, "director1", kind="director")
    bus = Bus(conn, config)
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter())

    core.send_message(conn, "director1", "exec1", "count this")
    start_turn(bus, conn, "exec1")
    finish_turn(bus, "exec1")

    assert wait_until(
        lambda: db.query_one(conn, "SELECT id FROM usage_turns") is not None
    )
    (row,) = db.query(conn, "SELECT * FROM usage_turns")
    assert (row["agent"], row["process"], row["woken_by"], row["outcome"]) == (
        "exec1", "turn", "director1", "clean"
    )
    requests = db.query(conn, "SELECT * FROM usage_requests WHERE turn_id = ?", (row["id"],))
    assert len(requests) == 1 and requests[0]["input_tokens"] > 0


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


def test_a_dead_turn_does_not_report_its_own_brief_as_unprocessed(conn, config):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1")
    make_workspace(config, conn, exec_id, "ws-brief")
    core.assign_work(
        conn, agent_id=exec_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    core.send_message(conn, "director1", "exec1", "use the green paint")
    head = db.query_one(conn, "SELECT MAX(id) AS m FROM messages")["m"]
    with db.transaction(conn) as c:
        c.execute(
            "UPDATE agents SET turn_start_message_id = 0, last_seen_message_id = ? WHERE id = ?",
            (head, exec_id),
        )

    Bus(conn, config)._close_turn("exec1", "vendor_error", None)

    notice = db.query_one(
        conn,
        "SELECT body FROM messages WHERE sender = ? AND recipient = 'director1' ORDER BY id DESC",
        (core.OFFICE_SENDER,),
    )["body"]
    assert "use the green paint" in notice
    assert "paint the fence" not in notice


# --------------------------------------------------------------------------- retried deaths


def test_a_vendor_error_death_is_retried_and_the_fourth_in_a_row_fails_the_work(
    conn, config, monkeypatch
):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1")
    make_workspace(config, conn, exec_id, "ws-retry")
    work = core.assign_work(
        conn, agent_id=exec_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    bus = Bus(conn, config)
    state = bus._state("exec1")
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(die=True))

    def work_row():
        return db.query_one(
            conn, "SELECT status, fail_reason FROM works WHERE id = ?", (work["id"],)
        )

    start_turn(bus, conn, "exec1")
    for delay in busmod.RETRY_DELAYS:
        finish_turn(bus, "exec1")
        assert work_row()["status"] == "running"
        assert 0 < state.retry_at - time.monotonic() <= delay
        # The retry comes due, and the tick starts it.
        state.retry_at = time.monotonic()
        assert tick_until(bus, lambda: state.turn is not None, busmod.COALESCE_SECONDS * 2)

    finish_turn(bus, "exec1")
    assert (work_row()["status"], work_row()["fail_reason"]) == ("failed", "vendor_error")
    assert state.retry_at is None


def test_a_turn_that_dies_on_a_reported_work_is_counted_against_the_work(
    conn, config, monkeypatch
):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1")
    make_workspace(config, conn, exec_id, "ws-dies")
    work = core.assign_work(
        conn, agent_id=exec_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    core.finish_work(conn, work["id"], summary="painted", actor="exec1")
    bus = Bus(conn, config)
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(die=True))

    start_turn(bus, conn, "exec1")
    finish_turn(bus, "exec1")

    assert wait_until(lambda: db.query_one(conn, "SELECT id FROM usage_turns") is not None)
    row = db.query_one(conn, "SELECT * FROM usage_turns")
    assert row["outcome"] != "clean"
    assert row["work_id"] == work["id"]
    assert row["input_tokens"] > 0 and row["output_tokens"] == 7


# --------------------------------------------------------------------------- quota hold


def test_no_turn_starts_on_a_runtime_and_model_held_for_quota_until_its_time_of_return(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-held")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump)
    )

    core.hold_for_quota(conn, "claude", "stub-model", int(time.time()) + 3600)
    message_id = core.send_message(conn, "director", "exec1", "when it comes back")["id"]
    assert not tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 2
    )
    assert (get_agent(conn, "exec1")["last_seen_message_id"] or 0) < message_id

    core.hold_for_quota(conn, "claude", "stub-model", int(time.time()) - 1)
    assert tick_until(
        bus, lambda: bus._state("exec1").turn is not None, busmod.COALESCE_SECONDS * 4
    )
    finish_turn(bus, "exec1")
    assert "when it comes back" in dump.read_text(encoding="utf-8")


def test_a_paused_work_resumes_when_its_holders_next_turn_starts(conn, config, monkeypatch):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1")
    make_workspace(config, conn, exec_id, "ws-paused")
    work = core.assign_work(
        conn, agent_id=exec_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    core.pause_work(conn, work["id"], "quota_exhausted", resume_after=int(time.time()) - 1)
    bus = Bus(conn, config)
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter())

    core.send_message(conn, "director1", "exec1", "the quota is back, carry on")
    start_turn(bus, conn, "exec1")
    finish_turn(bus, "exec1")

    resumed = db.query_one(conn, "SELECT * FROM works WHERE id = ?", (work["id"],))
    assert (resumed["status"], resumed["pause_reason"], resumed["resume_after"]) == (
        "running", None, None
    )


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


# --------------------------------------------------------------------------- progress check


def read_everything(conn):
    """Every agent has taken every message there is."""
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET last_seen_message_id = (SELECT MAX(id) FROM messages)")


def office_bodies(conn, name):
    return [
        r["body"] for r in db.query(
            conn, "SELECT body FROM messages WHERE sender = ? AND recipient = ? ORDER BY id",
            (core.OFFICE_SENDER, name),
        )
    ]


def a_lead_with_a_failed_work(conn, director_model="stub-model"):
    make_agent(conn, "director1", kind="director", model=director_model)
    make_agent(conn, "lead1", kind="lead", manager="director1")
    hand = make_agent(conn, "hand", manager="lead1")
    work = core.assign_work(
        conn, agent_id=hand, brief="dig the well", task_id=None, branch="well",
        complexity="medium", complexity_reason="one module", actor="lead1"
    )
    core.fail_work(conn, work["id"], "crash")
    read_everything(conn)


def test_a_stuck_agent_is_told_then_the_manager_above_then_the_journal(conn, config):
    a_lead_with_a_failed_work(conn)
    bus = Bus(conn, config)

    def journal():
        return db.query_one(conn, "SELECT COUNT(*) AS n FROM notices")["n"]

    bus._check_progress()
    assert len(office_bodies(conn, "lead1")) == 1
    assert "dig the well" in office_bodies(conn, "lead1")[0]
    bus._check_progress()
    assert len(office_bodies(conn, "lead1")) == 1

    read_everything(conn)
    bus._check_progress()
    assert len(office_bodies(conn, "director1")) == 1

    read_everything(conn)
    before = journal()
    bus._check_progress()
    bus._check_progress()
    assert journal() == before + 1

    # A word to the stuck agent starts the chain again one level up.
    told = len(office_bodies(conn, "director1"))
    core.send_message(conn, "director1", "lead1", "what now?")
    read_everything(conn)
    bus._check_progress()
    assert len(office_bodies(conn, "director1")) == told + 1


def test_a_level_held_for_quota_is_passed_over(conn, config):
    a_lead_with_a_failed_work(conn, director_model="other-model")
    core.hold_for_quota(conn, "claude", "stub-model", int(time.time()) + 3600)

    Bus(conn, config)._check_progress()

    assert office_bodies(conn, "lead1") == []
    assert len(office_bodies(conn, "director1")) == 1


def test_nothing_moving_tells_the_director_once(conn, config):
    make_agent(conn, "director1", kind="director")
    make_agent(conn, "hand", manager="director1")
    core.send_message(conn, "director1", "hand", "look into it")
    read_everything(conn)
    bus = Bus(conn, config)

    bus._check_progress()
    assert len(office_bodies(conn, "director1")) == 1

    read_everything(conn)
    bus._check_progress()
    core.send_message(conn, core.get_owner_name(conn), "director1", "thanks")
    read_everything(conn)
    bus._check_progress()
    assert len(office_bodies(conn, "director1")) == 1


def test_only_an_open_ticket_to_the_owner_moves_the_works_of_an_agent_waiting_on_him(conn, config):
    a_lead_with_a_failed_work(conn)
    owner = core.get_owner_name(conn)

    def lead_is_told() -> bool:
        read_everything(conn)
        before = len(office_bodies(conn, "lead1"))
        Bus(conn, config)._check_progress()
        return len(office_bodies(conn, "lead1")) > before

    core.send_message(conn, "lead1", owner, "should I go on?")
    assert lead_is_told()

    core.create_ticket(
        conn, title="a question", body=None, author="lead1", addressee="director1", kind="question"
    )
    assert lead_is_told()

    ticket = core.create_ticket(
        conn, title="a question", body=None, author="lead1", addressee=owner, kind="question"
    )
    assert not lead_is_told()

    core.resolve_ticket(conn, ticket["id"], resolved_by=owner, resolution="go on")
    assert lead_is_told()


# --------------------------------------------------------------------------- no double delivery


def test_no_double_delivery_when_two_hooks_claim_at_once(config):
    ws = config.ws_dir / "ws-race"
    ws.mkdir(parents=True)
    shared.claim_owner(ws, "agent1")

    for i in range(20):
        shared.deliver(ws, f"msg-{i}", "agent1", 0)
        hooks = [
            subprocess.Popen(
                [sys.executable, "-S", "-E", str(HOOK_SCRIPT), "claude", str(ws)],
                cwd=str(ws),
                env={**os.environ, "OFFICE_AGENT": "agent1"},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [hook.communicate(timeout=10)[0] for hook in hooks]

        winners = [
            json.loads(out)["hookSpecificOutput"]["additionalContext"]
            for out in outputs if out.strip()
        ]
        assert len(winners) == 1, f"trial {i}: expected exactly one winner, got {outputs!r}"
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
    assert "(executor/claude/" in prompt1
    assert "turn one" in prompt1

    agent2 = dict(get_agent(conn, "exec1"))
    assert agent2["session_id"]

    dump2 = tmp_path / "dump2.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump2))
    core.send_message(conn, "director", "exec1", "turn two")

    assert bus._start_turn(agent2) is True
    finish_turn(bus, "exec1")

    prompt2 = dump2.read_text(encoding="utf-8")
    assert "(executor/claude/" not in prompt2
    assert "turn two" in prompt2


def test_assign_with_a_fresh_session_drops_the_session_before_the_brief_and_waits_out_a_running_turn(
    conn, config, monkeypatch
):
    make_agent(conn, "boss", kind="director")
    idle_id = make_agent(conn, "idle-hand", manager="boss")
    busy_id = make_agent(conn, "busy-hand", manager="boss")
    make_workspace(config, conn, idle_id, "ws-idle-hand")
    make_workspace(config, conn, busy_id, "ws-busy-hand")
    with db.transaction(conn) as c:
        # An invented session id.
        c.execute("UPDATE agents SET session_id = 'invented-session' WHERE id = ?", (idle_id,))
    bus = Bus(conn, config)
    monkeypatch.setattr(
        busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(sleep=TURN_SECONDS)
    )
    assignment = {
        "brief": "paint the fence", "branch": "fence", "complexity": "medium",
        "fresh_session": True, "complexity_reason": "one module",
    }
    office_mcp.set_new_session_hook(bus.new_session)
    try:
        look_at_roster(conn, config, "boss")

        failed, text = answer_to(conn, config, "boss", "assign", {"agent": "idle-hand", **assignment})

        assert not failed, text
        assert get_agent(conn, "idle-hand")["session_id"] is None
        assert db.query_one(
            conn, "SELECT COUNT(*) AS n FROM works WHERE agent_id = ?", (idle_id,)
        )["n"] == 1
        assert db.query_one(
            conn,
            "SELECT COUNT(*) AS n FROM messages WHERE recipient = 'idle-hand' AND body LIKE ?",
            ("%paint the fence%",),
        )["n"] == 1

        start_turn(bus, conn, "busy-hand")
        session = get_agent(conn, "busy-hand")["session_id"]
        messages = db.query_one(conn, "SELECT COUNT(*) AS n FROM messages")["n"]
        failed, text = answer_to(conn, config, "boss", "assign", {"agent": "busy-hand", **assignment})

        assert failed
        assert get_agent(conn, "busy-hand")["session_id"] == session
        assert db.query_one(
            conn, "SELECT COUNT(*) AS n FROM works WHERE agent_id = ?", (busy_id,)
        )["n"] == 0
        assert db.query_one(
            conn, "SELECT COUNT(*) AS n FROM work_log WHERE agent = 'busy-hand'"
        )["n"] == 0
        assert db.query_one(conn, "SELECT COUNT(*) AS n FROM messages")["n"] == messages

        assert bus.stop_agent("busy-hand", "boss")["ok"]
        finish_turn(bus, "busy-hand")
    finally:
        office_mcp.set_new_session_hook(None)


# --------------------------------------------------------------------------- restart path


def test_restart_recovery_fails_only_the_work_that_had_a_turn_on_it(conn, config):
    make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1", status="running")
    waiting_id = make_agent(conn, "exec2", manager="director1", status="idle")
    make_workspace(config, conn, exec_id, "ws-restart")
    core.assign_work(
        conn, agent_id=exec_id, brief="refactor the widget", task_id=None, branch="feature-x",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    core.assign_work(
        conn, agent_id=waiting_id, brief="waiting on an answer", task_id=None, branch="feature-y",
        complexity="medium", complexity_reason="one module", actor="director1",
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


def test_restart_recovery_with_nothing_interrupted_sends_no_message(conn, config):
    make_agent(conn, "director1", kind="director")

    Bus(conn, config).recover_after_restart()

    assert db.query_one(conn, "SELECT id FROM messages") is None


# --------------------------------------------------------------------------- the switch


def test_a_paused_office_starts_a_turn_for_the_owners_direct_message_only(
    conn, config, monkeypatch, tmp_path
):
    agent_id = make_agent(conn, "exec1")
    make_workspace(config, conn, agent_id, "ws-paused")
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(busmod, "adapter_for", lambda runtime, cfg: StubClaudeAdapter(dump_path=dump))
    state = bus._state("exec1")

    def tick_past_the_window() -> None:
        bus._tick()
        state.drain_at = time.monotonic() - 1
        bus._tick()

    bus.set_running(False, "by hand")
    core.send_message(conn, "director", "exec1", "from an agent")
    tick_past_the_window()
    assert state.turn is None

    core.send_message(conn, core.get_owner_name(conn), "exec1", "from the owner")
    bus._tick()
    assert state.turn is not None
    finish_turn(bus, "exec1")
    prompt = dump.read_text(encoding="utf-8")
    assert "from an agent" in prompt and "from the owner" in prompt

    core.send_message(conn, "director", "exec1", "waiting")
    tick_past_the_window()
    assert state.turn is None

    bus.set_running(True, "by hand")
    bus._tick()
    assert state.turn is not None
    finish_turn(bus, "exec1")
    assert "waiting" in dump.read_text(encoding="utf-8")


def test_a_due_timer_flips_the_office_and_is_cleared(conn, config):
    bus = Bus(conn, config)
    bus.set_switch_timer("pause", 1000, "Owner")

    bus._fire_due_switch(999)
    assert bus.running and bus.switch_timer() == ("pause", 1000)

    bus._fire_due_switch(1000)
    assert not bus.running and bus.switch_timer() is None

    bus.set_switch_timer("run", 2000, "Owner")
    bus._fire_due_switch(2001)
    assert bus.running and bus.switch_timer() is None


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
        complexity="medium", complexity_reason="one module", actor="lower",
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
        conn, agent_id=hand, brief="mend the gate", task_id=None, branch="gate",
        complexity="medium", complexity_reason="one module", actor="lower"
    )
    core.assign_work(
        conn, agent_id=lower, brief="plan the garden", task_id=None, branch="garden",
        complexity=None, complexity_reason=None, actor="lower",
    )
    make_workspace(config, conn, hand, "ws-hand")
    make_workspace(config, conn, lower, "ws-lower")
    mark_mid_turn(conn, hand)
    mark_mid_turn(conn, lower)

    Bus(conn, config).recover_after_restart()

    to_lower = last_from(core.OFFICE_SENDER, "lower")
    assert "mend the gate" in to_lower
    assert "plan the garden" not in to_lower

    to_upper = last_from(core.OFFICE_SENDER, "upper")
    assert "plan the garden" in to_upper
    assert "mend the gate" not in to_upper

    to_director = last_from(core.OFFICE_SENDER, "director1")
    assert "mend the gate" in to_director and "plan the garden" in to_director
    assert last_from(core.OFFICE_SENDER, "hand") is None


# --------------------------------------------------------------------------- the keep-alive turn


# Minutes since an agent's last process ended: inside the keep-alive window, before it
# and after it.
DUE_MINUTES = (busmod.KEEP_ALIVE_SECONDS + core.CACHE_SECONDS) // 120
TOO_SOON_MINUTES = busmod.KEEP_ALIVE_SECONDS // 60 - 1
TOO_LATE_MINUTES = core.CACHE_SECONDS // 60 + 1


def add_usage_row(conn, agent, process, ended_minutes_ago, runtime="claude"):
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO usage_turns (agent, agent_kind, runtime, model, process, started_at, "
            "ended_at, outcome) VALUES (?, 'executor', ?, 'stub-model', ?, "
            "strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?), "
            "'clean')",
            (agent, runtime, process, f"-{ended_minutes_ago + 1} minutes",
             f"-{ended_minutes_ago} minutes"),
        )


def test_a_keep_alive_turn_is_due_for_an_agent_waiting_on_a_holder_at_work_and_delivers_nothing(
    conn, config, monkeypatch, tmp_path
):
    director_id = make_agent(conn, "director1", kind="director")
    exec_id = make_agent(conn, "exec1", manager="director1")
    make_workspace(config, conn, director_id, "ws-director")
    make_workspace(config, conn, exec_id, "ws-exec")
    core.assign_work(
        conn, agent_id=exec_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="director1",
    )
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET session_id = 'sess-director' WHERE id = ?", (director_id,))
    bus = Bus(conn, config)
    dump = tmp_path / "dump.txt"
    monkeypatch.setattr(
        busmod, "adapter_for",
        lambda runtime, cfg: StubClaudeAdapter(dump_path=dump, sleep=TURN_SECONDS),
    )

    def due() -> bool:
        return bus._keep_alive_due(dict(get_agent(conn, "director1")), bus._state("director1"))

    def last_process_ended(minutes_ago: int) -> None:
        with db.transaction(conn) as c:
            c.execute(
                "UPDATE usage_turns SET ended_at = strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?) "
                "WHERE agent = 'director1'",
                (f"-{minutes_ago} minutes",),
            )

    add_usage_row(conn, "director1", "turn", DUE_MINUTES)
    assert not due()  # the holder has no turn
    core.send_message(conn, "director1", "exec1", "start")
    start_turn(bus, conn, "exec1")
    assert due()

    last_process_ended(TOO_SOON_MINUTES)
    assert not due()  # too soon
    last_process_ended(TOO_LATE_MINUTES)
    assert not due()  # too late
    last_process_ended(DUE_MINUTES)
    assert due()

    message_id = core.send_message(conn, "exec1", "director1", "an unread message")["id"]
    assert not due()

    # The executor's stub has written the dump before the keep-alive's overwrites it.
    assert wait_until(lambda: dump.exists() and dump.read_text(encoding="utf-8") != "")
    assert bus._start_turn(dict(get_agent(conn, "director1")), ping=True) is True
    finish_turn(bus, "director1")
    assert wait_until(lambda: bus._state("exec1").turn is None)

    assert dump.read_text(encoding="utf-8") == busmod.KEEP_ALIVE_PROMPT
    ping = db.query_one(conn, "SELECT * FROM usage_turns WHERE process = 'ping'")
    assert (ping["agent"], ping["session_id"]) == ("director1", "sess-director")
    director = get_agent(conn, "director1")
    assert (director["last_seen_message_id"] or 0) < message_id
    assert director["turn_start_message_id"] is None


def test_an_executor_with_an_open_work_of_its_own_is_due_a_keep_alive_up_to_its_runtimes_cap_in_a_row(
    conn, config
):
    make_agent(conn, "director1", kind="director")
    lead_id = make_agent(conn, "lead1", kind="lead", manager="director1")
    claude_id = make_agent(conn, "exec1", manager="lead1")
    codex_id = make_agent(conn, "exec2", manager="lead1", runtime="codex")
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET session_id = 'sess'")
    bus = Bus(conn, config)

    def due(name: str) -> bool:
        return bus._keep_alive_due(dict(get_agent(conn, name)), bus._state(name))

    def read_everything(name: str) -> None:
        with db.transaction(conn) as c:
            c.execute(
                "UPDATE agents SET last_seen_message_id = (SELECT MAX(id) FROM messages) WHERE name = ?",
                (name,),
            )

    add_usage_row(conn, "exec1", "turn", DUE_MINUTES)
    add_usage_row(conn, "lead1", "turn", DUE_MINUTES)
    assert not due("exec1")  # no work
    work = core.assign_work(
        conn, agent_id=claude_id, brief="paint the fence", task_id=None, branch="fence",
        complexity="medium", complexity_reason="one module", actor="lead1",
    )
    assert not due("exec1")  # its brief is unread
    read_everything("exec1")
    assert due("exec1")
    core.pause_work(conn, work["id"], "quota_exhausted", resume_after=None)
    assert not due("exec1")
    core.assign_work(
        conn, agent_id=lead_id, brief="plan the garden", task_id=None, branch="garden",
        complexity=None, complexity_reason=None, actor="director1",
    )
    read_everything("lead1")
    assert not due("lead1")  # a lead's own work does not make it wait

    core.assign_work(
        conn, agent_id=codex_id, brief="mend the gate", task_id=None, branch="gate",
        complexity="medium", complexity_reason="one module", actor="lead1"
    )
    read_everything("exec2")
    for _ in range(busmod.KEEP_ALIVE_CAPS["codex"] - 1):
        add_usage_row(conn, "exec2", "ping", DUE_MINUTES, runtime="codex")
    assert due("exec2")
    add_usage_row(conn, "exec2", "ping", DUE_MINUTES, runtime="codex")
    assert not due("exec2")
    add_usage_row(conn, "exec2", "turn", DUE_MINUTES, runtime="codex")
    assert due("exec2")

    core.hold_for_quota(conn, "codex", "stub-model", int(time.time()) + 600)
    assert not due("exec2")

    claude2_id = make_agent(conn, "exec3", manager="lead1")
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET session_id = 'sess' WHERE id = ?", (claude2_id,))
    core.assign_work(
        conn, agent_id=claude2_id, brief="weed the beds", task_id=None, branch="beds",
        complexity="medium", complexity_reason="one module", actor="lead1",
    )
    read_everything("exec3")
    for _ in range(busmod.KEEP_ALIVE_CAPS["claude"] - 1):
        add_usage_row(conn, "exec3", "ping", DUE_MINUTES)
    assert due("exec3")
    add_usage_row(conn, "exec3", "ping", DUE_MINUTES)
    assert not due("exec3")


# --------------------------------------------------------------------------- codex quota

CODEX_THREAD = "00000000-0000-4000-8000-000000000001"


def codex_quota_bus(conn, config, tmp_path, monkeypatch):
    """A bus over one codex agent whose thread is CODEX_THREAD."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    agent_id = make_agent(conn, "exec1", runtime="codex")
    with db.transaction(conn) as c:
        c.execute("UPDATE agents SET session_id = ? WHERE id = ?", (CODEX_THREAD, agent_id))
    return Bus(conn, dataclasses.replace(config, codex_bin="codex"))


def codex_reading(bus, tmp_path, second, week_remaining, week_resets_at):
    """Rewrite the thread's rollout file to one token_count line stamped `second`
    seconds past a fixed minute, then poll codex once. The line is constructed from
    the field names on record: top-level `timestamp`, `payload.type`, and
    `payload.rate_limits.secondary` with `used_percent`, `window_minutes`, `resets_at`."""
    day = tmp_path / "codex" / "sessions" / datetime.now().strftime("%Y/%m/%d")
    day.mkdir(parents=True, exist_ok=True)
    line = {
        "timestamp": f"2026-10-10T12:00:{second:02d}.000Z",
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "rate_limits": {
                "primary": None,
                "secondary": {
                    "used_percent": (1 - week_remaining) * 100,
                    "window_minutes": 10080,
                    "resets_at": week_resets_at,
                },
            },
        },
    }
    (day / f"rollout-2026-10-10T12-00-00-{CODEX_THREAD}.jsonl").write_text(
        json.dumps(line) + "\n", encoding="utf-8"
    )
    bus._poll_runtime("codex")


def codex_buckets(conn):
    return {q["label"]: q for q in core.quota_buckets(conn)}


def test_a_codex_reading_older_than_the_stored_one_is_discarded(conn, config, tmp_path, monkeypatch):
    bus = codex_quota_bus(conn, config, tmp_path, monkeypatch)
    resets = int(time.time()) + 3 * 24 * 3600

    codex_reading(bus, tmp_path, 2, 0.8, resets)
    codex_reading(bus, tmp_path, 1, 0.3, resets)
    week = codex_buckets(conn)["week"]
    assert (round(week["remaining_fraction"], 2), week["read_at"]) == (0.8, "2026-10-10T12:00:02.000Z")

    codex_reading(bus, tmp_path, 3, 0.7, resets)
    assert round(codex_buckets(conn)["week"]["remaining_fraction"], 2) == 0.7
    history = db.query(conn, "SELECT remaining_fraction FROM quota_polls WHERE label = 'week' ORDER BY id")
    assert [round(r["remaining_fraction"], 2) for r in history] == [0.8, 0.7]


def test_a_fall_of_codexs_week_lowers_its_emulated_window_by_the_fall_times_the_ratio(
    conn, config, tmp_path, monkeypatch
):
    # The limits are invented: a ratio of 100 to 20.
    core.set_setting(conn, wallets.week_key("codex"), "100")
    core.set_setting(conn, wallets.window_key("codex"), "20")
    core.set_setting(conn, wallets.WINDOW_EMULATED_KEY, "1")
    bus = codex_quota_bus(conn, config, tmp_path, monkeypatch)
    resets = int(time.time()) + 3 * 24 * 3600

    # No week is stored yet, so nothing is emulated.
    codex_reading(bus, tmp_path, 1, 0.9, resets)
    assert "5h" not in codex_buckets(conn)

    # The window was closed: it opens at what the fall leaves of it.
    codex_reading(bus, tmp_path, 2, 0.8, resets)
    window = codex_buckets(conn)["5h"]
    assert round(window["remaining_fraction"], 2) == 0.5
    assert abs(int(window["reset_time"]) - (time.time() + 5 * 3600)) < 60
    assert window["read_at"] is None

    # Open: the fall comes off what is left, and the reset stays.
    codex_reading(bus, tmp_path, 3, 0.78, resets)
    after = codex_buckets(conn)["5h"]
    assert round(after["remaining_fraction"], 2) == 0.4
    assert after["reset_time"] == window["reset_time"]

    codex_reading(bus, tmp_path, 4, 0.78, resets)
    assert round(codex_buckets(conn)["5h"]["remaining_fraction"], 2) == 0.4
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM quota_polls WHERE label = '5h'")["n"] == 0


def test_a_week_that_reset_between_two_codex_readings_counts_from_full(conn, config, tmp_path, monkeypatch):
    # The limits are invented: a ratio of 1.
    core.set_setting(conn, wallets.week_key("codex"), "100")
    core.set_setting(conn, wallets.window_key("codex"), "100")
    core.set_setting(conn, wallets.WINDOW_EMULATED_KEY, "1")
    bus = codex_quota_bus(conn, config, tmp_path, monkeypatch)
    resets = int(time.time()) + 3 * 24 * 3600
    codex_reading(bus, tmp_path, 1, 0.9, resets)
    codex_reading(bus, tmp_path, 2, 0.8, resets)
    assert round(codex_buckets(conn)["5h"]["remaining_fraction"], 2) == 0.9

    # The stored week's reset has passed since it was read.
    db.execute(
        conn, "UPDATE quota SET remaining_fraction = 0.1, reset_time = ? WHERE label = 'week'",
        (str(int(time.time()) - 1),),
    )
    codex_reading(bus, tmp_path, 3, 0.95, resets)
    assert round(codex_buckets(conn)["5h"]["remaining_fraction"], 2) == 0.85

    # The answered week's reset has passed: nothing was drawn.
    codex_reading(bus, tmp_path, 4, 0.5, int(time.time()) - 1)
    assert round(codex_buckets(conn)["5h"]["remaining_fraction"], 2) == 0.85


# --------------------------------------------------------------------------- wiki export mirror


def folder_files(folder: Path) -> dict[str, str]:
    """Every file under `folder`, by its path relative to it, with its text."""
    return {
        f.relative_to(folder).as_posix(): f.read_text(encoding="utf-8")
        for f in sorted(folder.rglob("*")) if f.is_file()
    }


def test_the_export_folder_follows_every_kind_of_write_to_the_wiki(conn, config, tmp_path):
    # Invented: pages, categories and bodies.
    def write(path, body, *, version=None, title="T", category="c"):
        core.note_wiki(
            conn, path=path, category=category, title=title, body=body, updated_by="owner",
            expected_version=version,
        )

    export = tmp_path / "export"
    core.set_setting(conn, busmod.WIKI_EXPORT_SETTING, str(export))
    bus = Bus(conn, config)
    write("a/b", "one")
    write("a/c", "two")
    write("solo", "three")
    bus._mirror_wiki()
    assert folder_files(export) == {
        "a/b.md": "# T\n\nCategory: c\n\none",
        "a/c.md": "# T\n\nCategory: c\n\ntwo",
        "solo.md": "# T\n\nCategory: c\n\nthree",
    }

    write("a/b", "one, edited", version=1, title="Renamed", category="d")
    write("a/c", "two, edited", version=1)
    core.undo_wiki_page(conn, "a/c")
    core.delete_wiki_page(conn, "solo", 1)
    bus._mirror_wiki()
    assert folder_files(export) == {
        "a/b.md": "# Renamed\n\nCategory: d\n\none, edited",
        "a/c.md": "# T\n\nCategory: c\n\ntwo",
    }

    core.delete_wiki_page(conn, "a/b", 2)
    core.delete_wiki_page(conn, "a/c", 3)
    bus._mirror_wiki()
    assert not (export / "a").exists()

    # A page deleted and written again at the same path and version between two passes.
    write("a/b", "first")
    bus._mirror_wiki()
    core.delete_wiki_page(conn, "a/b", 1)
    write("a/b", "second")
    bus._mirror_wiki()
    assert folder_files(export) == {"a/b.md": "# T\n\nCategory: c\n\nsecond"}


def test_a_full_pass_clears_what_is_not_a_page_but_leaves_other_files(conn, config, tmp_path):
    # Invented: the leftovers.
    export = tmp_path / "export"
    (export / "old" / "deep").mkdir(parents=True)
    (export / "old" / "deep" / "gone.md").write_text("stray", encoding="utf-8")
    (export / "keep.txt").write_text("not a page", encoding="utf-8")
    core.note_wiki(
        conn, path="p", category="c", title="T", body="b", updated_by="owner", expected_version=None
    )
    core.set_setting(conn, busmod.WIKI_EXPORT_SETTING, str(export))
    Bus(conn, config)._mirror_wiki()
    assert folder_files(export) == {"keep.txt": "not a page", "p.md": "# T\n\nCategory: c\n\nb"}
    assert not (export / "old").exists()
