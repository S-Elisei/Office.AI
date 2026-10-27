"""Registered-device WebSocket access and durable mobile delivery."""

from __future__ import annotations

import asyncio
import base64
from contextlib import contextmanager
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from office import core, db
from office.hub import Broadcaster, app
from office.mobile import Entrypoints, create_app, device_id

ORIGIN = "https://office.example"
SOCKET = "wss://office.example/api/socket"


@pytest.fixture
def phones(conn, config, monkeypatch):
    """Invented office state and generated device keys; no vendor processes."""
    monkeypatch.setattr(app.state, "db", conn, raising=False)
    monkeypatch.setattr(app.state, "config", config, raising=False)
    monkeypatch.setattr(app.state, "broadcaster", Broadcaster(), raising=False)
    mobile = create_app(app.state, ORIGIN)
    local = TestClient(app, base_url="http://localhost:7777")
    client = TestClient(mobile, base_url=ORIGIN)
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
    return {"challenge": challenge["challenge"],
            "signature": base64.b64encode(r.to_bytes(32, "big") + s.to_bytes(32, "big")).decode()}


@contextmanager
def connected(client, key, device):
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": device})
        challenge = socket.receive_json()
        assert challenge["event"] == "challenge"
        socket.send_json(proof(key, challenge["data"]))
        ready = socket.receive_json()
        assert ready["event"] == "ready"
        yield socket, ready["data"]


def rpc(socket, op, data=None):
    socket.send_json({"id": 1, "op": op, "data": data or {}})
    while True:
        response = socket.receive_json()
        if "id" in response or response.get("event") == "unauthorized":
            return response


def test_proof_is_single_use_and_requires_the_registered_private_key(phones):
    local, client, _ = phones
    key, device = register(local)
    other_key, _ = register(local, "Other phone")
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": device})
        challenge = socket.receive_json()["data"]
        socket.send_json(proof(other_key, challenge))
        assert socket.receive_json()["status"] == 401
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": device})
        socket.receive_json()
        socket.send_json(proof(key, challenge))
        assert socket.receive_json()["status"] == 401
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": device})
        challenge = socket.receive_json()["data"]
        signed = proof(key, challenge)
        socket.send_json(signed)
        assert socket.receive_json()["event"] == "ready"
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": device})
        socket.receive_json()
        socket.send_json(signed)
        assert socket.receive_json()["status"] == 401


def test_revoking_a_phone_ends_its_sessions_and_pending_signins(phones):
    local, client, _ = phones
    key, device = register(local)
    with connected(client, key, device) as (socket, _):
        with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as pending:
            pending.send_json({"device": device})
            challenge = pending.receive_json()["data"]
            assert local.post(f"/settings/mobile/{device}/revoke").is_success
            assert rpc(socket, "push")["event"] == "unauthorized"
            pending.send_json(proof(key, challenge))
            assert pending.receive_json()["status"] == 401
    raw = key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    assert local.post("/settings/mobile", data={
        "name": "Phone again", "public_key": "office-p256:" + base64.b64encode(raw).decode(),
    }).is_success
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as pending:
        pending.send_json({"device": device})
        pending.receive_json()
        pending.send_json(proof(key, challenge))
        assert pending.receive_json()["status"] == 401


def test_mobile_boundary_excludes_local_routes_and_cross_origin_sockets(phones):
    _, client, _ = phones
    for headers in ({"Origin": "https://another.example"}, {},
                    {"Origin": ORIGIN, "Host": "another.example"}):
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(SOCKET, headers=headers):
                pass
    with client.websocket_connect(SOCKET, headers={"Origin": ORIGIN}) as socket:
        socket.send_json({"device": "0" * 64})
        assert socket.receive_json()["status"] == 401
    for path in ("/settings", "/file?path=C:/secret", "/mcp/director", "/hooks/token", "/openapi.json"):
        assert client.get(path).status_code == 404


def test_retries_deliver_once_across_connections_and_restart(phones, conn):
    local, client, mobile = phones
    key, device = register(local)
    body = {"peer": "all", "body": "Keep exactly this instruction", "request_id": str(uuid4())}
    with connected(client, key, device) as (socket, _):
        first = rpc(socket, "send", body)
        assert first["status"] == 200
    with connected(client, key, device) as (socket, _):
        assert rpc(socket, "send", body)["data"]["id"] == first["data"]["id"]
    restarted = TestClient(create_app(mobile.state, ORIGIN), base_url=ORIGIN)
    with connected(restarted, key, device) as (socket, _):
        assert rpc(socket, "send", body)["data"]["id"] == first["data"]["id"]
        assert rpc(socket, "send", {**body, "body": "Another instruction"})["status"] == 409
    assert db.query_one(conn, "SELECT COUNT(*) AS n FROM messages")["n"] == 1


def test_mobile_history_keeps_other_peoples_private_messages_private(phones, conn):
    local, client, _ = phones
    key, device = register(local)
    # Invented participants, inserted without starting their runtimes.
    for name, kind in (("director", "director"), ("worker", "executor")):
        db.execute(conn, "INSERT INTO agents(name, kind, runtime, model) VALUES (?, ?, 'claude', 'test')", (name, kind))
    owner = core.get_owner_name(conn)
    first = core.send_message(conn, "director", owner, "For the owner")
    core.send_message(conn, "director", "worker", "Only between agents")
    common = core.send_message(conn, "director", "all", "For everyone")
    with connected(client, key, device) as (socket, _):
        direct = rpc(socket, "messages", {"peer": "director"})["data"]["messages"]
        public = rpc(socket, "messages", {"peer": "all", "after_id": 0})["data"]["messages"]
        assert [m["id"] for m in direct] == [first["id"]]
        assert [m["id"] for m in public] == [common["id"]]
        assert rpc(socket, "seen", {"peer": "director", "message_id": first["id"]})["status"] == 200


def test_accepting_sockets_port_selects_the_application(phones):
    _, _, mobile = phones
    application = Entrypoints(app, mobile, 7788)
    client = TestClient(application)
    assert client.get("http://localhost:7777/sw.js", headers={"Host": "office.example"}).status_code == 403
    with client.websocket_connect("wss://office.example:7788/api/socket",
                                  headers={"Origin": ORIGIN, "Host": "office.example"}) as socket:
        socket.send_json({"device": "0" * 64})
        assert socket.receive_json()["status"] == 401


def test_reconnection_and_older_pages_keep_every_message(phones, conn):
    local, client, _ = phones
    key, device = register(local)
    owner = core.get_owner_name(conn)
    expected = [core.send_message(conn, owner, "all", f"Message {n}")["id"] for n in range(250)]
    with connected(client, key, device) as (socket, _):
        caught_up = []
        after = 0
        while True:
            page = rpc(socket, "messages", {"peer": "all", "after_id": after})["data"]
            caught_up.extend(m["id"] for m in page["messages"])
            if not page["has_newer"]:
                break
            after = caught_up[-1]
        assert caught_up == expected
        page = rpc(socket, "messages", {"peer": "all"})["data"]
        history = [m["id"] for m in page["messages"]]
        while page["has_older"]:
            page = rpc(socket, "messages", {"peer": "all", "before_id": history[0]})["data"]
            history = [m["id"] for m in page["messages"]] + history
        assert history == expected


def test_push_queue_tracks_incoming_direct_messages_and_device_revocation(phones, conn):
    local, client, _ = phones
    key, device = register(local)
    owner = core.get_owner_name(conn)
    # Invented push subscription fields; this test sends no network requests.
    subscription = {"endpoint": "https://push.example.invalid/phone", "keys": {"auth": "invented", "p256dh": "invented"}}
    with connected(client, key, device) as (socket, _):
        identity = rpc(socket, "push")["data"]["public_key"]
        insecure = {**subscription, "endpoint": subscription["endpoint"].replace("https://", "http://", 1)}
        assert rpc(socket, "push_subscribe", insecure)["status"] == 400
        assert db.query(conn, "SELECT * FROM mobile_push_subscriptions") == []
        assert rpc(socket, "push_subscribe", subscription)["status"] == 200
        db.execute(conn, "INSERT INTO agents(name, kind, runtime, model) VALUES ('director', 'director', 'claude', 'test')")
        core.send_message(conn, "director", "all", "Common message")
        assert db.query(conn, "SELECT * FROM mobile_push_pending") == []
        first = core.send_message(conn, "director", owner, "First direct message")
        second = core.send_message(conn, "director", owner, "Second direct message")
        pending = db.query(conn, "SELECT * FROM mobile_push_pending")
        assert len(pending) == 1
        assert pending[0]["message_id"] == second["id"]
        assert pending[0]["message_id"] != first["id"]
    with connected(client, key, device) as (socket, _):
        assert rpc(socket, "push")["data"]["public_key"] == identity
    local.post(f"/settings/mobile/{device}/revoke")
    assert db.query(conn, "SELECT * FROM mobile_push_subscriptions") == []
    assert db.query(conn, "SELECT * FROM mobile_push_pending") == []


def test_visible_conversation_suppresses_push_and_views_belong_to_sockets(phones, conn):
    local, client, mobile = phones
    key, device = register(local)
    owner = core.get_owner_name(conn)
    # Invented subscription fields; the visible conversation is suppressed before delivery.
    subscription = {"endpoint": "https://push.example.invalid/phone", "keys": {"auth": "invented", "p256dh": "invented"}}
    db.execute(conn, "INSERT INTO agents(name, kind, runtime, model) VALUES ('director', 'director', 'claude', 'test')")
    push = mobile.state.mobile_push
    with connected(client, key, device) as (first, _):
        rpc(first, "push_subscribe", subscription)
        rpc(first, "view", {"peer": "director"})
        with connected(client, key, device) as (second, _):
            rpc(second, "view", {"peer": None})
            assert set(push.views.values()) == {(device, "director"), (device, None)}
            core.send_message(conn, "director", owner, "On the phone's screen")
            asyncio.run(push.step())
            assert db.query(conn, "SELECT * FROM mobile_push_pending") == []
        assert list(push.views.values()) == [(device, "director")]
        rpc(first, "view", {"peer": None})
        assert list(push.views.values()) == [(device, None)]
    assert push.views == {}
