"""The hook a local service posts its answer to."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from office import core, db
from office import mcp as office_mcp
from office.hub import app

#: An owner's name a service could also give as its own.
OWNER = "boss"

#: The address the office answers at, as a service or the owner's browser uses it.
HUB = "http://127.0.0.1:7777"


def add_agent(conn, name, kind):
    with db.transaction(conn) as c:
        c.execute(
            "INSERT INTO agents (name, kind, runtime, model, status) "
            "VALUES (?, ?, 'claude', 'stub-model', 'idle')",
            (name, kind),
        )


def _address(conn, config, agent, about):
    """The path of the address the `expect` tool hands `agent`."""
    answer = office_mcp._h_expect(
        conn, config, agent, "executor", {"about": about, "within_seconds": 3600}
    )
    return urlsplit(re.search(r"http://\S+", answer).group()).path


def _latest_for(conn, recipient):
    return db.query_one(
        conn,
        "SELECT sender, body FROM messages WHERE recipient = ? ORDER BY id DESC",
        (recipient,),
    )


def test_a_service_speaks_under_no_participant_name(conn, config, monkeypatch):
    core.set_setting(conn, "owner_name", OWNER)
    add_agent(conn, "director1", "director")
    add_agent(conn, "exec1", "executor")
    participants = {OWNER, "director1", "exec1", core.OFFICE_SENDER}
    monkeypatch.setattr(app.state, "db", conn, raising=False)
    # Not entered as a context manager: the app's lifespan does not run.
    client = TestClient(app, base_url=HUB)

    for claimed in (OWNER, "director1", core.OFFICE_SENDER):
        text = f"job done, says {claimed}"
        response = client.post(
            _address(conn, config, "exec1", f"the job {claimed} answers"),
            json={"from": claimed, "text": text},
        )
        assert response.status_code == 202, response.text
        row = _latest_for(conn, "exec1")
        assert text in row["body"]
        assert row["sender"] not in participants
        assert claimed in row["sender"]

    # Control: a participant's own message, read the same way, carries its name.
    core.send_message(conn, "director1", "exec1", "from the director")
    assert _latest_for(conn, "exec1")["sender"] == "director1"


def test_a_page_of_another_site_cannot_change_anything(conn, config, monkeypatch):
    add_agent(conn, "exec1", "executor")
    monkeypatch.setattr(app.state, "db", conn, raising=False)
    body = {"from": "asset-factory", "text": "job done"}

    address = _address(conn, config, "exec1", "the render")
    foreign = TestClient(app, base_url=HUB).post(
        address, json=body, headers={"Origin": "http://elsewhere.example"}
    )
    rebound = TestClient(app, base_url="http://elsewhere.example:7777").post(
        address, json=body, headers={"Origin": "http://elsewhere.example:7777"}
    )
    assert foreign.status_code == 403
    assert rebound.status_code == 403
    assert _latest_for(conn, "exec1") is None
    assert len(core.open_expectations(conn)) == 1

    # Control: the same request from the owner's own page is taken.
    own = TestClient(app, base_url=HUB).post(address, json=body, headers={"Origin": HUB})
    assert own.status_code == 202, own.text
    assert "job done" in _latest_for(conn, "exec1")["body"]
    assert core.open_expectations(conn) == []
