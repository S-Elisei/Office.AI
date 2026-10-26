"""The mobile HTTP application and its registered-device authentication."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, utils
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from office import core, db

ASSETS = Path(__file__).parent / "web" / "mobile"
COOKIE = "__Host-office_mobile"
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
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}

        async def secured_send(message):
            if message["type"] == "http.response.start":
                message["headers"].extend([
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"referrer-policy", b"no-referrer"),
                    (b"content-security-policy", b"default-src 'none'; script-src 'self'; "
                     b"style-src 'self'; img-src 'self'; connect-src 'self'; manifest-src 'self'; "
                     b"worker-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"),
                ])
            await send(message)

        if headers.get("host") != self.host:
            await PlainTextResponse("Open the configured mobile address.", 403)(scope, receive, secured_send)
            return
        if scope["method"] not in {"GET", "HEAD"}:
            if headers.get("origin") != self.origin or headers.get("x-office-mobile") != "1":
                await PlainTextResponse("Open the mobile app and retry.", 403)(scope, receive, secured_send)
                return
            body = bytearray()
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return
                body.extend(message.get("body", b""))
                if len(body) > 128 * 1024:
                    await PlainTextResponse("Request too large. Send a shorter message.", 413)(
                        scope, receive, secured_send
                    )
                    return
                if not message.get("more_body", False):
                    break

            async def buffered_receive():
                nonlocal body
                if body is not None:
                    result = {"type": "http.request", "body": bytes(body), "more_body": False}
                    body = None
                    return result
                return await receive()

            await self.app(scope, buffered_receive, secured_send)
            return
        await self.app(scope, receive, secured_send)


def create_app(state, origin: str) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.state = state
    auth = MobileAuth(state, origin)
    state.mobile_auth = auth
    app.add_middleware(MobileBoundary, origin=origin)

    async def authenticated(request: Request) -> str:
        device = auth.current(request.cookies.get(COOKIE, ""))
        if device is None:
            raise HTTPException(401, "Session ended. Reconnect.")
        return device

    @app.get("/")
    def index():
        return FileResponse(ASSETS / "index.html")

    @app.get("/sw.js")
    def service_worker():
        return FileResponse(ASSETS / "sw.js", media_type="text/javascript")

    app.mount("/assets", StaticFiles(directory=ASSETS / "assets"))

    @app.post("/api/challenge")
    async def challenge(data: ChallengeRequest):
        return auth.challenge(data.device)

    @app.post("/api/session")
    async def session(data: ProofRequest, response: Response):
        token = auth.prove(data)
        response.set_cookie(
            COOKIE, token, max_age=SESSION_SECONDS, secure=True, httponly=True, samesite="strict"
        )
        return {"connected": True}

    @app.delete("/api/session", dependencies=[Depends(authenticated)])
    async def logout(request: Request, response: Response):
        token = request.cookies[COOKIE]
        auth.sessions.pop(hashlib.sha256(token.encode()).hexdigest(), None)
        response.delete_cookie(COOKIE, secure=True, httponly=True, samesite="strict")
        return {"connected": False}

    @app.get("/api/conversations", dependencies=[Depends(authenticated)])
    def conversations():
        conn = state.db
        owner = core.get_owner_name(conn)
        threads = {t["peer"]: t for t in core.dm_threads(conn, owner)}
        peers = [dict(r) for r in db.query(
            conn, "SELECT name, kind, status FROM agents ORDER BY kind = 'director' DESC, name COLLATE NOCASE"
        )]
        names = {p["name"] for p in peers}
        peers.extend({"name": n, "kind": "", "status": ""} for n in threads if n not in names)
        peers.insert(0, {"name": "all", "kind": "common", "status": ""})
        for peer in peers:
            name = peer["name"]
            common = name == "all"
            last = db.query_one(
                conn, "SELECT body FROM messages WHERE channel = 'all' AND recipient IS NULL "
                "ORDER BY id DESC LIMIT 1",
            ) if common else threads.get(name)
            peer["preview"] = (last["body"] if common else last["preview"])[:160] if last else ""
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
        return {"owner": owner, "conversations": peers}

    @app.get("/api/messages", dependencies=[Depends(authenticated)])
    def messages(peer: str, before_id: int | None = None, after_id: int | None = None):
        if after_id is not None:
            owner = core.get_owner_name(state.db)
            where = "channel = 'all' AND recipient IS NULL" if peer == "all" else (
                "channel = 'dm' AND ((sender = ? AND recipient = ?) OR (sender = ? AND recipient = ?))"
            )
            params = () if peer == "all" else (owner, peer, peer, owner)
            rows = db.query(
                state.db, f"SELECT * FROM messages WHERE {where} AND id > ? ORDER BY id LIMIT 101",
                params + (after_id,),
            )
            return {"messages": [dict(r) for r in rows[:100]], "has_newer": len(rows) > 100}
        if peer == "all":
            rows, older = core.chat_history(state.db, before_id, limit=100)
        else:
            rows, older = core.dm_history(state.db, peer, core.get_owner_name(state.db), before_id, limit=100)
        return {"messages": [dict(r) for r in rows], "has_older": older}

    @app.post("/api/messages")
    def send_message(data: SendRequest, device: str = Depends(authenticated)):
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

    @app.post("/api/seen", dependencies=[Depends(authenticated)])
    def seen(data: SeenRequest):
        common = data.peer == "all"
        core.see_conversation(
            state.db, channel="all" if common else "dm", peer="" if common else data.peer,
            up_to_message_id=data.message_id,
        )
        return Response(status_code=204)

    @app.get("/api/events", dependencies=[Depends(authenticated)])
    async def events(request: Request):
        queue = state.broadcaster.subscribe()
        token = request.cookies[COOKIE]

        async def stream():
            try:
                yield "event: refresh\ndata: {}\n\n"
                while not await request.is_disconnected():
                    try:
                        message = await asyncio.wait_for(queue.get(), timeout=15)
                    except asyncio.TimeoutError:
                        message = ""
                    if auth.current(token) is None:
                        yield "event: revoked\ndata: {}\n\n"
                        return
                    if message.startswith(("event: messages\n", "event: agents\n")):
                        yield "event: refresh\ndata: {}\n\n"
                    else:
                        yield ": ping\n\n"
            finally:
                state.broadcaster.unsubscribe(queue)

        return StreamingResponse(stream(), media_type="text/event-stream", headers={"X-Accel-Buffering": "no"})

    return app


class Entrypoints:
    """Dispatch by the accepted socket's port; the hub owns the lifespan."""

    def __init__(self, local, mobile, port: int):
        self.local = local
        self.mobile = mobile
        self.port = port

    async def __call__(self, scope, receive, send):
        target = self.mobile if scope["type"] == "http" and scope["server"][1] == self.port else self.local
        await target(scope, receive, send)
