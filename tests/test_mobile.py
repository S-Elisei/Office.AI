"""Registered-device access and delivery over the mobile HTTP boundary."""

from __future__ import annotations

import base64
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient

from office import core, db
from office.hub import Broadcaster, app
from office.mobile import Entrypoints, create_app, device_id


@pytest.fixture
def phones(conn, config, monkeypatch):
    """Invented office state and generated device keys; no vendor processes."""
    monkeypatch.setattr(app.state, "db", conn, raising=False)
    monkeypatch.setattr(app.state, "config", config, raising=False)
    monkeypatch.setattr(app.state, "broadcaster", Broadcaster(), raising=False)
    origin = "https://office.example"
    mobile = create_app(app.state, origin)
    local = TestClient(app, base_url="http://localhost:7777")
    client = TestClient(mobile, base_url=origin, headers={"Origin": origin, "X-Office-Mobile": "1"})
    return local, client, mobile


def register(local, name="Phone"):
    key = ec.generate_private_key(ec.SECP256R1())
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    response = local.post("/settings/mobile", data={
        "name": name, "public_key": "office-p256:" + base64.b64encode(raw).decode(),
    })
    assert response.is_success
    return key, device_id(raw)


def proof(key, challenge):
    signature = key.sign(challenge["message"].encode(), ec.ECDSA(hashes.SHA256()))
    r, s = utils.decode_dss_signature(signature)
    return {
        "challenge": challenge["challenge"],
        "signature": base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode(),
    }


def login(client, key, device):
    challenge = client.post("/api/challenge", json={"device": device})
    assert challenge.is_success
    signed = proof(key, challenge.json())
    response = client.post("/api/session", json=signed)
    assert response.is_success
    return signed


def test_proof_is_single_use_and_requires_the_registered_private_key(phones):
    local, client, _ = phones
    key, device = register(local)
    other_key, _ = register(local, "Other phone")
    challenge = client.post("/api/challenge", json={"device": device}).json()
    assert client.post("/api/session", json=proof(other_key, challenge)).status_code == 401
    assert client.post("/api/session", json=proof(key, challenge)).status_code == 401
    signed = login(client, key, device)
    assert client.post("/api/session", json=signed).status_code == 401
    assert client.get("/api/conversations").is_success


def test_revoking_a_phone_ends_its_sessions_and_pending_signins(phones):
    local, client, _ = phones
    key, device = register(local)
    login(client, key, device)
    challenge = client.post("/api/challenge", json={"device": device}).json()
    assert local.post(f"/settings/mobile/{device}/revoke").is_success
    assert client.get("/api/conversations").status_code == 401
    assert client.post("/api/session", json=proof(key, challenge)).status_code == 401
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert local.post("/settings/mobile", data={
        "name": "Phone again", "public_key": "office-p256:" + base64.b64encode(raw).decode(),
    }).is_success
    assert client.get("/api/conversations").status_code == 401


def test_mobile_boundary_excludes_local_routes_and_cross_origin_writes(phones, conn):
    local, client, mobile = phones
    assert client.get("/api/conversations").status_code == 401
    key, device = register(local)
    login(client, key, device)
    body = {"peer": "all", "body": "Do this once", "request_id": str(uuid4())}
    foreign = {"Origin": "https://another.example"}
    assert client.post("/api/messages", json=body, headers=foreign).status_code == 403
    without_origin = TestClient(mobile, base_url="https://office.example", headers={"X-Office-Mobile": "1"})
    without_origin.cookies.update(client.cookies)
    assert without_origin.post("/api/messages", json=body).status_code == 403
    assert client.post("/api/messages", json=body, headers={"X-Office-Mobile": ""}).status_code == 403
    assert client.get("/api/conversations", headers={"Host": "another.example"}).status_code == 403
    for path in ("/settings", "/file?path=C:/secret", "/mcp/director", "/hooks/token", "/openapi.json"):
        assert client.get(path).status_code == 404
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM messages")["n"] == 0


def test_retries_deliver_once_across_sessions_and_restart(phones, conn):
    local, client, mobile = phones
    key, device = register(local)
    login(client, key, device)
    body = {"peer": "all", "body": "Keep exactly this instruction", "request_id": str(uuid4())}
    first = client.post("/api/messages", json=body)
    assert first.is_success
    login(client, key, device)
    again = client.post("/api/messages", json=body)
    assert again.json()["id"] == first.json()["id"]
    restarted = TestClient(create_app(mobile.state, "https://office.example"),
                           base_url="https://office.example", headers=client.headers)
    login(restarted, key, device)
    assert restarted.post("/api/messages", json=body).json()["id"] == first.json()["id"]
    assert restarted.post("/api/messages", json={**body, "body": "Another instruction"}).status_code == 409
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM messages")["n"] == 1


def test_mobile_history_keeps_other_peoples_private_messages_private(phones, conn):
    local, client, _ = phones
    key, device = register(local)
    login(client, key, device)
    # Invented participants, inserted without starting their runtimes.
    for name, kind in (("director", "director"), ("worker", "executor")):
        db.execute(conn, "INSERT INTO agents(name, kind, runtime, model) VALUES (?, ?, 'claude', 'test')", (name, kind))
    owner = core.get_owner_name(conn)
    first = core.send_message(conn, "director", owner, "For the owner")
    core.send_message(conn, "director", "worker", "Only between agents")
    common = core.send_message(conn, "director", "all", "For everyone")
    assert [m["id"] for m in client.get("/api/messages", params={"peer": "director"}).json()["messages"]] == [first["id"]]
    assert [m["id"] for m in client.get("/api/messages", params={"peer": "all", "after_id": 0}).json()["messages"]] == [common["id"]]
    assert client.post("/api/seen", json={"peer": "director", "message_id": first["id"]}).is_success


def test_accepting_sockets_port_selects_the_application(phones):
    _, _, mobile = phones
    application = Entrypoints(app, mobile, 7788)
    local = TestClient(application, base_url="http://localhost:7777")
    remote = TestClient(application, base_url="https://office.example:7788", headers={"Host": "office.example"})
    assert local.get("/api/conversations").status_code == 404
    assert remote.get("/api/conversations").status_code == 401
    assert remote.get("/settings").status_code == 404


def test_reconnection_and_older_pages_keep_every_message(phones, conn):
    local, client, _ = phones
    key, device = register(local)
    login(client, key, device)
    owner = core.get_owner_name(conn)
    expected = [core.send_message(conn, owner, "all", f"Message {n}")["id"] for n in range(250)]
    caught_up = []
    after = 0
    while True:
        page = client.get("/api/messages", params={"peer": "all", "after_id": after}).json()
        caught_up.extend(m["id"] for m in page["messages"])
        if not page["has_newer"]:
            break
        after = caught_up[-1]
    assert caught_up == expected

    page = client.get("/api/messages", params={"peer": "all"}).json()
    history = [m["id"] for m in page["messages"]]
    while page["has_older"]:
        page = client.get("/api/messages", params={"peer": "all", "before_id": history[0]}).json()
        history = [m["id"] for m in page["messages"]] + history
    assert history == expected
