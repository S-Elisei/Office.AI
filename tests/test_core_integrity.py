from __future__ import annotations

import time

import pytest

from office import core, db, wallets
from office.adapters.base import QuotaSnapshot


def hire_row(conn, name, kind="executor", manager_id=None):
    with db.transaction(conn) as c:
        cur = c.execute(
            "INSERT INTO agents (name, kind, manager_agent_id, role, runtime, model, status, "
            "last_seen_message_id) "
            "VALUES (?, ?, ?, ?, 'claude', 'm', 'idle', "
            "(SELECT COALESCE(MAX(id), 0) FROM messages))",
            (name, kind, manager_id, "coding" if kind == "executor" else None),
        )
        return cur.lastrowid


def make_work(conn, agent_id, task_id=None):
    return core.assign_work(
        conn, agent_id=agent_id, brief="do the thing", task_id=task_id, branch="feature-x",
        complexity="medium", complexity_reason="one module", actor="dir",
    )["id"]


def test_a_reported_work_survives_until_its_creator_closes_it(conn):
    director_id = hire_row(conn, "dir", kind="director")
    agent_id = hire_row(conn, "exec1", manager_id=director_id)
    task = core.create_task(conn, "Build the widget")
    reported = make_work(conn, agent_id, task_id=task["id"])
    failed = make_work(conn, hire_row(conn, "exec2", manager_id=director_id))

    core.finish_work(conn, reported, summary="widget built", actor="exec1")
    core.fail_work(conn, failed, "crash", output_tail="boom")

    still_there = db.query_one(conn, "SELECT * FROM works WHERE id = ?", (reported,))
    assert still_there["status"] == "done"
    assert core._row(conn, "tasks", "id", task["id"])["result"] is None

    core.finish_work(conn, reported, summary="widget built properly", actor="exec1")
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM works WHERE id = ?", (reported,))["n"] == 1

    core.close_work(conn, reported, summary="widget built properly", actor="dir")

    assert db.query_one(conn, "SELECT * FROM works WHERE id = ?", (reported,)) is None
    assert core._row(conn, "tasks", "id", task["id"])["result"] == "widget built properly"

    survivor = db.query_one(conn, "SELECT * FROM works WHERE id = ?", (failed,))
    assert survivor["status"] == "failed"
    assert survivor["output_tail"] == "boom"


def test_a_bucket_whose_reset_has_passed_reads_as_unused(conn):
    now = int(time.time())
    polled = [
        QuotaSnapshot(runtime="claude", label="5h", remaining_fraction=0.2, reset_time=now - 3600),
        QuotaSnapshot(runtime="claude", label="week", remaining_fraction=0.5, reset_time=now + 3600),
    ]

    assert core.record_quota(conn, polled) == 2
    stored = {r["label"]: (r["remaining_fraction"], r["reset_time"]) for r in core.quota_buckets(conn)}
    assert stored == {"5h": (1.0, None), "week": (0.5, str(now + 3600))}
    history = db.query(conn, "SELECT label, remaining_fraction FROM quota_polls ORDER BY id")
    assert [(r["label"], r["remaining_fraction"]) for r in history] == [("5h", 0.2), ("week", 0.5)]

    # The same reading again changes nothing, in the snapshot or in the history.
    assert core.record_quota(conn, polled) == 0
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM quota_polls")["n"] == 2

    # A stored reading whose reset passes later, with no new reading, reads as unused too.
    db.execute(conn, "UPDATE quota SET remaining_fraction = 0.3, reset_time = ? WHERE label = 'week'",
               (str(now - 1),))
    assert core.expire_passed_quota(conn) == 1
    stored = {r["label"]: (r["remaining_fraction"], r["reset_time"]) for r in core.quota_buckets(conn)}
    assert stored == {"5h": (1.0, None), "week": (1.0, None)}


def test_a_work_is_committed_to_its_wallet_and_a_work_the_wallet_cannot_cover_is_refused(conn):
    # The price list, the wallet's settings and the claude buckets are invented.
    director_id = hire_row(conn, "dir", kind="director")
    first = hire_row(conn, "exec1", manager_id=director_id)
    second = hire_row(conn, "exec2", manager_id=director_id)
    core.set_price_lists(
        conn, [{"role": "coding", "complexity": "medium", "model": "m", "effort": "", "price": "6"}], [],
        actor="dir",
    )
    core.set_setting(conn, wallets.week_key("claude"), "100")
    core.set_setting(conn, wallets.window_key("claude"), "10")
    now = int(time.time())
    core.record_quota(conn, [
        QuotaSnapshot(runtime="claude", label="week (all models)", remaining_fraction=1.0,
                      reset_time=now + 3 * 24 * 3600),
        QuotaSnapshot(runtime="claude", label="5h", remaining_fraction=1.0, reset_time=now + 4 * 3600),
    ])

    committed = db.query_one(conn, "SELECT * FROM works WHERE id = ?", (make_work(conn, first),))
    assert (committed["price"], committed["price_wallet"]) == (6.0, "claude")
    assert wallets.available(conn, "claude")["available"] == 4.0
    assert (wallets.available(conn, "claude")["window"], wallets.available(conn, "claude")["window_size"]) == (
        "5h window", 10.0
    )

    with pytest.raises(ValueError):
        make_work(conn, second)
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM works")["n"] == 1

    core.record_quota(
        conn,
        [QuotaSnapshot(runtime="claude", label="5h", remaining_fraction=1.0, reset_time=now + 1800)],
        partial=True,
    )
    assert db.query_one(conn, "SELECT * FROM works WHERE id = ?", (make_work(conn, second),)) is not None
