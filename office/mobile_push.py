"""Web Push subscriptions and queued direct-message notifications."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from pywebpush import WebPushException, webpush
from requests.exceptions import RequestException

from office import db

log = logging.getLogger(__name__)


class PushDelivery:
    def __init__(self, state, origin: str):
        self.state = state
        self.origin = origin
        self.wake = asyncio.Event()
        self.views: dict[str, tuple[str, str | None]] = {}

    def identity(self):
        with db.transaction(self.state.db):
            row = db.query_one(self.state.db, "SELECT private_key FROM mobile_push_identity WHERE id = 1")
            if row is None:
                key = ec.generate_private_key(ec.SECP256R1())
                raw = key.private_bytes(
                    serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
                db.execute(self.state.db, "INSERT INTO mobile_push_identity (id, private_key) VALUES (1, ?)", (raw,))
            else:
                key = serialization.load_der_private_key(row["private_key"], password=None)
        return key

    def status(self, device: str) -> dict:
        public = self.identity().public_key().public_bytes(
            serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
        )
        row = db.query_one(
            self.state.db, "SELECT subscription FROM mobile_push_subscriptions WHERE device_id = ?", (device,)
        )
        return {
            "public_key": base64.urlsafe_b64encode(public).decode().rstrip("="),
            "endpoint": json.loads(row["subscription"])["endpoint"] if row else None,
        }

    def subscribe(self, device: str, subscription: dict) -> None:
        db.execute(
            self.state.db,
            "INSERT INTO mobile_push_subscriptions (device_id, subscription) VALUES (?, ?) "
            "ON CONFLICT(device_id) DO UPDATE SET subscription = excluded.subscription",
            (device, json.dumps(subscription)),
        )

    def unsubscribe(self, device: str) -> None:
        db.execute(self.state.db, "DELETE FROM mobile_push_subscriptions WHERE device_id = ?", (device,))

    async def step(self) -> None:
        rows = db.query(
            self.state.db,
            "SELECT p.* FROM mobile_push_pending p "
            "WHERE p.retry_at <= ? ORDER BY p.message_id LIMIT 50",
            (time.time(),),
        )
        if not rows:
            return
        raw = self.identity().private_bytes(
            serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        private = base64.urlsafe_b64encode(raw).decode()
        for row in rows:
            current = db.query_one(
                self.state.db,
                "SELECT s.subscription FROM mobile_push_subscriptions s JOIN mobile_push_pending p "
                "ON p.device_id = s.device_id WHERE p.device_id = ? AND p.peer = ? AND p.message_id = ?",
                (row["device_id"], row["peer"], row["message_id"]),
            )
            if current is None:
                continue
            if (row["device_id"], row["peer"]) in self.views.values():
                db.execute(
                    self.state.db,
                    "DELETE FROM mobile_push_pending WHERE device_id = ? AND peer = ? AND message_id = ?",
                    (row["device_id"], row["peer"], row["message_id"]),
                )
                continue
            payload = json.dumps({
                "peer": row["peer"], "message_id": row["message_id"],
                "title": f"New message from {row['peer']}", "body": "Open Office.AI to read it.",
            })
            try:
                await asyncio.to_thread(
                    webpush, subscription_info=json.loads(current["subscription"]), data=payload,
                    vapid_private_key=private, vapid_claims={"sub": self.origin},
                    ttl=24 * 60 * 60, timeout=15, headers={"Urgency": "high"},
                )
            except (WebPushException, RequestException) as exc:
                response = getattr(exc, "response", None)
                status = response.status_code if response is not None else None
                if status in {404, 410}:
                    db.execute(
                        self.state.db,
                        "DELETE FROM mobile_push_subscriptions WHERE device_id = ? AND subscription = ?",
                        (row["device_id"], current["subscription"]),
                    )
                else:
                    db.execute(
                        self.state.db,
                        "UPDATE mobile_push_pending SET failures = failures + 1, retry_at = ? "
                        "WHERE device_id = ? AND peer = ? AND message_id = ?",
                        (time.time() + min(3600, 30 * 2 ** min(row["failures"], 7)),
                         row["device_id"], row["peer"], row["message_id"]),
                    )
                    log.warning("Push delivery for device %s failed (HTTP %s); queued for retry", row["device_id"][:16], status)
            else:
                db.execute(
                    self.state.db,
                    "DELETE FROM mobile_push_pending WHERE device_id = ? AND peer = ? AND message_id = ?",
                    (row["device_id"], row["peer"], row["message_id"]),
                )

    async def run(self) -> None:
        while True:
            self.wake.clear()
            await self.step()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass
