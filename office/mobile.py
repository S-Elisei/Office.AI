"""The mobile HTTP application and its registered-device authentication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from office import core, db, monitor, office_switch
from office.mobile_push import PushDelivery

ASSETS = Path(__file__).parent / "web" / "mobile"
SESSION_SECONDS = 24 * 60 * 60


def device_id(public_key: bytes) -> str:
    return hashlib.sha256(public_key).hexdigest()


def register_device(conn, name: str, text: str) -> None:
    raw = base64.b64decode(text.removeprefix("office-p256:"))
    db.execute(
        conn,
        "INSERT INTO mobile_devices (id, name, public_key) VALUES (?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET name = excluded.name",
        (device_id(raw), name, raw),
    )


class Protocol(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ChallengeRequest(Protocol):
    device: str = Field(min_length=64, max_length=64)


class ProofRequest(Protocol):
    challenge: str = Field(max_length=64)
    signature: str = Field(max_length=128)


class SendRequest(Protocol):
    request_id: str = Field(max_length=36)
    peer: str
    body: str


class SeenRequest(Protocol):
    peer: str
    message_id: int


class ViewRequest(Protocol):
    peer: str | None
    works: bool = False
    quota: bool = False
    outputs: list[int] = Field(default_factory=list)


class SwitchRequest(Protocol):
    to: Literal["run", "pause"]


class TimerRequest(SwitchRequest):
    at: str


class StopRequest(Protocol):
    agent: str


class WakeRequest(Protocol):
    wake_id: int


class HistoryRequest(Protocol):
    peer: str
    before_id: int | None = None
    after_id: int | None = None


class PushSubscription(Protocol):
    endpoint: str
    keys: dict[str, str]
    expirationTime: float | None = None


class SocketRequest(Protocol):
    id: int
    op: Literal[
        "messages", "send", "seen", "view", "push", "push_subscribe", "push_unsubscribe",
        "switch", "timer", "timer_cancel", "work_stop", "wake_cancel",
    ]
    data: dict = Field(default_factory=dict)


class MobileAuth:
    def __init__(self, state, origin: str):
        self.state = state
        self.origin = origin
        self.challenges: dict[str, tuple[str, bytes, float]] = {}
        self.sessions: dict[str, tuple[str, float]] = {}

    def challenge(self, device: str) -> dict:
        now = time.time()
        self.challenges = {k: v for k, v in self.challenges.items() if v[2] > now}
        if db.query_one(self.state.db, "SELECT 1 FROM mobile_devices WHERE id = ?", (device,)) is None:
            raise HTTPException(401, "Device not registered. Add its public key in local Settings.")
        nonce = secrets.token_urlsafe(32)
        message = f"Office.AI mobile login\n{self.origin}\n{device}\n{nonce}".encode()
        self.challenges[nonce] = device, message, now + 60
        return {"challenge": nonce, "message": message.decode()}

    def prove(self, proof: ProofRequest) -> str:
        pending = self.challenges.pop(proof.challenge, None)
        if pending is None or pending[2] <= time.time():
            raise HTTPException(401, "Sign-in expired. Reconnect.")
        device, message, _ = pending
        row = db.query_one(self.state.db, "SELECT public_key FROM mobile_devices WHERE id = ?", (device,))
        try:
            signature = base64.b64decode(proof.signature, validate=True)
            der = utils.encode_dss_signature(
                int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
            )
            key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), row["public_key"])
            key.verify(der, message, ec.ECDSA(hashes.SHA256()))
        except (ValueError, InvalidSignature):
            raise HTTPException(401, "Signature rejected. Reconnect.") from None
        now = time.time()
        self.sessions = {k: v for k, v in self.sessions.items() if v[1] > now}
        token = secrets.token_urlsafe(32)
        self.sessions[hashlib.sha256(token.encode()).hexdigest()] = device, now + SESSION_SECONDS
        return token

    def current(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        session = self.sessions.get(digest)
        if session is None:
            return None
        if session[1] <= time.time():
            self.sessions.pop(digest, None)
            return None
        return session[0]

    def revoke(self, device: str) -> None:
        self.sessions = {k: v for k, v in self.sessions.items() if v[0] != device}
        self.challenges = {k: v for k, v in self.challenges.items() if v[0] != device}


class MobileBoundary:
    def __init__(self, app, origin: str):
        self.app = app
        self.origin = origin
        self.host = urlsplit(origin).netloc

    async def __call__(self, scope, receive, send):
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        if scope["type"] == "websocket":
            if headers.get("host") != self.host or headers.get("origin") != self.origin:
                await send({"type": "websocket.close", "code": 1008})
                return
            await self.app(scope, receive, send)
            return

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"].extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", (
                        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; "
                        f"connect-src 'self' wss://{self.host}; manifest-src 'self'; "
                        "worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
                    ).encode("ascii")),
                ])
            await send(message)

        if headers.get("host") != self.host:
            await PlainTextResponse("Open the configured mobile address.", 403)(scope, receive, secured_send)
            return
        await self.app(scope, receive, secured_send)


def create_app(state, origin: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state = state
    auth = MobileAuth(state, origin)
    state.mobile_auth = auth
    state.mobile_push = PushDelivery(state, origin)
    app.add_middleware(MobileBoundary, origin=origin)

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/sw.js")
    def service_worker():
        version = hashlib.sha256()
        files = [ASSETS / "index.html", *sorted(p for p in (ASSETS / "assets").rglob("*") if p.is_file())]
        for path in files:
            version.update(path.relative_to(ASSETS).as_posix().encode())
            version.update(b"\0")
            version.update(path.read_bytes())
            version.update(b"\0")
        source = (ASSETS / "sw.js").read_text(encoding="utf-8")
        return Response(
            f'const CACHE = "office-mobile-{version.hexdigest()}";\n{source}',
            media_type="text/javascript",
        )

    app.mount("/assets", StaticFiles(directory=ASSETS / "assets"))

    def conversations():
        conn = state.db
        owner = core.get_owner_name(conn)
        threads = {t["peer"]: t for t in core.dm_threads(conn, owner)}
        peers = [dict(r) for r in db.query(
            conn, "SELECT name, kind, status FROM agents ORDER BY name COLLATE NOCASE"
        )]
        names = {p["name"] for p in peers}
        peers.extend({"name": n, "kind": "", "status": ""} for n in threads if n not in names)
        peers.insert(0, {"name": "all", "kind": "common", "status": ""})
        for peer in peers:
            name = peer["name"]
            common = name == "all"
            last = db.query_one(
                conn, "SELECT id, body AS preview FROM messages WHERE channel = 'all' AND recipient IS NULL "
                "ORDER BY id DESC LIMIT 1",
            ) if common else threads.get(name)
            peer["preview"] = last["preview"][:160] if last else ""
            peer["last_id"] = last["id"] if last else 0
            peer["unread"] = db.query_one(
                conn,
                "SELECT COUNT(*) AS n FROM messages WHERE "
                + ("channel = 'all' AND recipient IS NULL AND sender <> ?" if common else
                   "channel = 'dm' AND sender = ? AND recipient = ?")
                + " AND id > COALESCE((SELECT seen_message_id FROM owner_reading "
                  "WHERE channel = ? AND peer = ?), 0)",
                ((owner,) if common else (name, owner)) + ("all" if common else "dm", "" if common else name),
            )["n"]
            peer["can_send"] = common or bool(peer["kind"])
        peers.sort(key=lambda p: (p["kind"] != "director", -p["last_id"]))
        return {"owner": owner, "conversations": peers}

    def messages(peer: str, before_id: int | None = None, after_id: int | None = None):
        rows, more = core.conversation_history(
            state.db, peer, core.get_owner_name(state.db), before_id=before_id, after_id=after_id, limit=100,
        )
        return {"messages": [dict(row) for row in rows], "has_newer" if after_id is not None else "has_older": more}

    def send_message(data: SendRequest, device: str):
        try:
            request_key = f"{device}:{UUID(data.request_id)}"
        except ValueError:
            raise HTTPException(400, "Send identifier rejected. Start a new send.") from None
        owner = core.get_owner_name(state.db)
        try:
            return core.send_message(
                state.db, owner, data.peer, data.body, actor=owner, request_key=request_key
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    def seen(data: SeenRequest):
        common = data.peer == "all"
        core.see_conversation(
            state.db, channel="all" if common else "dm", peer="" if common else data.peer,
            up_to_message_id=data.message_id,
        )

    @app.websocket("/api/socket")
    async def socket(websocket: WebSocket):
        await websocket.accept()
        try:
            hello = ChallengeRequest.model_validate_json(await asyncio.wait_for(websocket.receive_text(), timeout=30))
            await websocket.send_json({"event": "challenge", "data": auth.challenge(hello.device)})
            proof = ProofRequest.model_validate_json(await asyncio.wait_for(websocket.receive_text(), timeout=30))
            token = auth.prove(proof)
        except HTTPException as exc:
            await websocket.send_json({"event": "error", "status": exc.status_code, "detail": exc.detail})
            await websocket.close(code=4401)
            return
        except (ValidationError, asyncio.TimeoutError):
            await websocket.close(code=1008, reason="Sign-in incomplete")
            return
        except WebSocketDisconnect:
            return
        queue = state.broadcaster.subscribe()
        receive = asyncio.create_task(websocket.receive_text())
        update = asyncio.create_task(queue.get())
        watching_works = False
        watching_quota = False
        quota_snapshot = None
        output_ids = []
        works_snapshot = None
        output_snapshots = {}
        refresh_at = 0.0
        refresh_due = None

        def schedule_works():
            nonlocal refresh_due
            if watching_works and refresh_due is None:
                refresh_due = asyncio.get_running_loop().time() + 0.1

        async def send_works():
            nonlocal refresh_at, refresh_due, works_snapshot
            snapshot = await asyncio.to_thread(monitor.snapshot, state.db, state.config, state.bus)
            if works_snapshot is not None:
                for name, at in monitor.new_clock_activity(works_snapshot["works"], snapshot["works"]).items():
                    await websocket.send_json({"event": "activity", "data": {"subject_type": "agent", "name": name, "at": at}})
            changes = snapshot if works_snapshot is None else monitor.works_delta(works_snapshot, snapshot)
            if changes:
                await websocket.send_json({
                    "event": "works", "kind": "snapshot" if works_snapshot is None else "delta",
                    "server_time": time.time(), "data": changes,
                })
            works_snapshot = snapshot
            for work_id in output_ids:
                output = await asyncio.to_thread(monitor.output, state.db, state.config, state.bus, work_id)
                changes = monitor.output_delta(output_snapshots.get(work_id), output)
                if changes:
                    await websocket.send_json({
                        "event": "work_output", "work_id": work_id, "server_time": time.time(), "data": changes,
                    })
                output_snapshots[work_id] = output
            refresh_at = asyncio.get_running_loop().time() + 10
            refresh_due = None

        async def send_quota():
            nonlocal quota_snapshot
            current = monitor.quota(state.db)
            if current != quota_snapshot:
                await websocket.send_json({"event": "quota", "data": current})
                quota_snapshot = current

        try:
            await websocket.send_json({
                "event": "ready", "data": conversations(), "office": office_switch.snapshot(state.bus),
            })
            while True:
                deadline = min(refresh_at, refresh_due) if refresh_due is not None else refresh_at
                timeout = max(0, deadline - asyncio.get_running_loop().time()) if watching_works else 15
                done, _ = await asyncio.wait({receive, update}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
                device = auth.current(token)
                if device is None:
                    await websocket.send_json({"event": "unauthorized", "detail": "Session ended. Reconnect."})
                    await websocket.close(code=4401)
                    return
                if receive in done:
                    raw = receive.result()
                    try:
                        request = SocketRequest.model_validate_json(raw)
                    except ValidationError:
                        await websocket.close(code=1008, reason="Invalid request")
                        return
                    try:
                        if request.op == "messages":
                            result = messages(**HistoryRequest.model_validate(request.data).model_dump())
                        elif request.op == "send":
                            result = send_message(SendRequest.model_validate(request.data), device)
                        elif request.op == "seen":
                            result = seen(SeenRequest.model_validate(request.data))
                        elif request.op == "view":
                            view = ViewRequest.model_validate(request.data)
                            state.mobile_push.views[token] = (device, view.peer)
                            if not view.works or not watching_works:
                                works_snapshot = None
                                output_snapshots.clear()
                            refresh_due = None
                            watching_works = view.works
                            if not view.quota or not watching_quota:
                                quota_snapshot = None
                            watching_quota = view.quota
                            output_ids = view.outputs if watching_works else []
                            output_snapshots = {key: value for key, value in output_snapshots.items() if key in output_ids}
                            if watching_works:
                                await send_works()
                            if watching_quota:
                                await send_quota()
                            result = None
                        elif request.op == "switch":
                            data = SwitchRequest.model_validate(request.data)
                            state.bus.set_running(data.to == "run", "by hand", actor=core.get_owner_name(state.db))
                            result = office_switch.snapshot(state.bus)
                        elif request.op == "timer":
                            data = TimerRequest.model_validate(request.data)
                            office_switch.set_timer(state.bus, data.to, data.at, core.get_owner_name(state.db))
                            result = office_switch.snapshot(state.bus)
                        elif request.op == "timer_cancel":
                            state.bus.cancel_switch_timer(core.get_owner_name(state.db))
                            result = office_switch.snapshot(state.bus)
                        elif request.op == "work_stop":
                            data = StopRequest.model_validate(request.data)
                            result = await asyncio.to_thread(state.bus.stop_agent, data.agent, core.get_owner_name(state.db))
                            if not result["ok"]:
                                raise HTTPException(409, result["reason"])
                            schedule_works()
                            result = None
                        elif request.op == "wake_cancel":
                            data = WakeRequest.model_validate(request.data)
                            core.cancel_scheduled_message(state.db, data.wake_id, sender=None, actor=core.get_owner_name(state.db))
                            schedule_works()
                            result = None
                        elif request.op == "push":
                            result = state.mobile_push.status(device)
                        elif request.op == "push_subscribe":
                            subscription = PushSubscription.model_validate(request.data)
                            if not subscription.endpoint.startswith("https://"):
                                raise HTTPException(400, "Push endpoint requires HTTPS. Enable notifications again.")
                            state.mobile_push.subscribe(device, subscription.model_dump())
                            result = state.mobile_push.status(device)
                        elif request.op == "push_unsubscribe":
                            state.mobile_push.unsubscribe(device)
                            result = state.mobile_push.status(device)
                    except ValidationError:
                        await websocket.send_json({"id": request.id, "status": 422, "detail": "Request rejected. Refresh the app."})
                    except HTTPException as exc:
                        await websocket.send_json({"id": request.id, "status": exc.status_code, "detail": exc.detail})
                    else:
                        await websocket.send_json({"id": request.id, "status": 200, "data": result})
                    receive = asyncio.create_task(websocket.receive_text())
                if update in done:
                    kind, encoded = update.result().split("\ndata: ", 1)
                    update = asyncio.create_task(queue.get())
                    if kind == "event: messages":
                        payload = json.loads(encoded)
                        if payload["subject_type"] != "message":
                            schedule_works()
                        else:
                            row = db.query_one(state.db, "SELECT * FROM messages WHERE id = ?", (payload["subject_id"],))
                            owner = core.get_owner_name(state.db)
                            common = row["channel"] == "all" and row["recipient"] is None
                            direct = row["channel"] == "dm" and owner in {row["sender"], row["recipient"]}
                            if common or direct:
                                peer = "all" if common else row["recipient"] if row["sender"] == owner else row["sender"]
                                conversation = next(p for p in conversations()["conversations"] if p["name"] == peer)
                                await websocket.send_json({"event": "message", "message": dict(row), "conversation": conversation})
                    elif kind == "event: agents":
                        await websocket.send_json({"event": "conversations", "data": conversations()})
                    elif watching_works and kind == "event: activity":
                        payload = json.loads(encoded)
                        if payload["subject_type"] == "agent":
                            await websocket.send_json({"event": "activity", "data": payload})
                    if kind in {"event: notices", "event: settings", "event: agents"}:
                        await websocket.send_json({"event": "office", "data": office_switch.snapshot(state.bus)})
                    if watching_works and kind in {"event: works", "event: agents", "event: tasks"}:
                        schedule_works()
                    if watching_quota and kind == "event: quota":
                        await send_quota()
                if watching_works and asyncio.get_running_loop().time() >= (
                    min(refresh_at, refresh_due) if refresh_due is not None else refresh_at
                ):
                    await send_works()
        except WebSocketDisconnect:
            pass
        finally:
            state.mobile_push.views.pop(token, None)
            auth.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
            state.broadcaster.unsubscribe(queue)
            for task in (receive, update):
                task.cancel()
                with suppress(asyncio.CancelledError, WebSocketDisconnect):
                    await task

    return app


class Entrypoints:
    """Dispatch by the accepted socket's port; the hub owns the lifespan."""

    def __init__(self, local, mobile, port: int):
        self.local = local
        self.mobile = mobile
        self.port = port

    async def __call__(self, scope, receive, send):
        target = self.mobile if scope["type"] in {"http", "websocket"} and scope["server"][1] == self.port else self.local
        await target(scope, receive, send)
