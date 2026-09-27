from __future__ import annotations

from office import core, db


def hire_row(conn, name, kind="executor", manager_id=None):
    with db.transaction(conn) as c:
        cur = c.execute(
            "INSERT INTO agents (name, kind, manager_agent_id, runtime, model, status, "
            "last_seen_message_id) "
            "VALUES (?, ?, ?, 'claude', 'm', 'idle', "
            "(SELECT COALESCE(MAX(id), 0) FROM messages))",
            (name, kind, manager_id),
        )
        return cur.lastrowid


def make_work(conn, agent_id, task_id=None):
    return core.assign_work(
        conn, agent_id=agent_id, brief="do the thing", task_id=task_id, branch="feature-x",
        actor="dir",
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
