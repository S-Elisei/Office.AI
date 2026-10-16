"""Quota as wallets: a pool of a vendor's quota counted in its own currency, one unit
being one dollar at the vendor's API list prices."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from office import core, db
from office.adapters.base import QUOTA_5H, QUOTA_WEEK


@dataclass(frozen=True)
class Wallet:
    """One pool of quota. `week_bucket` and `window_bucket` are the labels of its weekly
    and its 5h quota buckets (no `window_bucket`: the pool has no 5h limit); `group` is
    the vendor's name for the models that share the pool, where a runtime has several."""

    id: str
    runtime: str
    currency: str
    week_bucket: str
    window_bucket: str | None
    group: str | None = None

    @property
    def name(self) -> str:
        """The runtime, and its group where it has one: "agy · Gemini 4 Argon"."""
        return f"{self.runtime} · {self.group}" if self.group else self.runtime


_GEMINI_GROUP = "Gemini Models"
_ARGON_GROUP = "Gemini 4 Argon"

#: The wallet of each runtime's models is the one whose id is the runtime; the
#: models of the Argon group draw on `argon` instead.
WALLETS = (
    Wallet("claude", "claude", "CL", f"{QUOTA_WEEK} (all models)", QUOTA_5H),
    Wallet("codex", "codex", "CD", QUOTA_WEEK, QUOTA_5H),
    Wallet("agy", "agy", "GM", f"{QUOTA_WEEK} ({_GEMINI_GROUP})", f"{QUOTA_5H} ({_GEMINI_GROUP})", _GEMINI_GROUP),
    Wallet("argon", "agy", "AR", f"{QUOTA_WEEK} ({_ARGON_GROUP})", None, _ARGON_GROUP),
)

BY_ID = {wallet.id: wallet for wallet in WALLETS}

#: The span of one weekly bucket.
_WEEK_SECONDS = 7 * 24 * 3600

#: The span of one 5h window.
WINDOW_SECONDS = 5 * 3600

#: How long before a bucket's reset its next period's amount counts as available.
CREDIT_SECONDS = 3600


def of_model(conn, runtime: str, model: str) -> Wallet:
    """The wallet a model on `runtime` draws on: `argon` for an agy model whose catalogue
    display name starts with the Argon group's name, the runtime's own wallet otherwise."""
    argon = BY_ID["argon"]
    if runtime == argon.runtime:
        row = db.query_one(
            conn, "SELECT display_name FROM models WHERE runtime = ? AND model_id = ?", (runtime, model)
        )
        if row is not None and (row["display_name"] or "").startswith(argon.group):
            return argon
    return BY_ID[runtime]


def week_key(wallet: str) -> str:
    """The setting holding a wallet's weekly limit, in units."""
    return f"wallet_{wallet}_week"


def window_key(wallet: str) -> str | None:
    """The setting holding a wallet's 5h window, in units; None for a wallet without a 5h
    bucket."""
    return f"wallet_{wallet}_window" if BY_ID[wallet].window_bucket else None


def reserve_key(wallet: str) -> str:
    """The setting holding a wallet's reserve: the percentage of its week the team
    leaves untouched."""
    return f"wallet_{wallet}_reserve"


def withhold_key(wallet: str) -> str:
    """The setting that, when "1", shows a wallet's spare as 0."""
    return f"wallet_{wallet}_withhold"


#: The setting that, when "1", makes the office keep codex's 5h window itself.
WINDOW_EMULATED_KEY = "wallet_codex_window_emulated"


#: Every setting the owner edits per wallet: weekly limit, 5h window (of a wallet with
#: a 5h bucket) in units, reserve in percent.
SETTING_KEYS = tuple(
    key for wallet in WALLETS
    for key in (week_key(wallet.id), window_key(wallet.id), reserve_key(wallet.id))
    if key is not None
)


def _number(raw: str | None) -> float | None:
    return float(raw) if raw and raw.strip() else None


def week_remaining(conn, wallet: str) -> float | None:
    """The remaining fraction of a wallet's weekly bucket, or None when none is stored."""
    pool = BY_ID[wallet]
    return next(
        (q["remaining_fraction"] for q in core.quota_buckets(conn)
         if q["runtime"] == pool.runtime and q["label"] == pool.week_bucket),
        None,
    )


def units(value: float) -> str:
    """A wallet figure: whole units from a hundred up, one decimal from ten, two below."""
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"


def _committed(conn, wallet: str, leaving: int | None) -> tuple[float, int]:
    """What the open works other than work `leaving` commit on a wallet, and how many
    of them do."""
    row = db.query_one(
        conn,
        "SELECT COALESCE(SUM(CASE WHEN price_wallet = :w THEN price ELSE 0 END), 0) "
        "+ COALESCE(SUM(CASE WHEN coordination_wallet = :w THEN coordination ELSE 0 END), 0) "
        "AS amount, COUNT(*) AS n FROM works "
        "WHERE (price_wallet = :w OR coordination_wallet = :w) AND id IS NOT :leaving",
        {"w": wallet, "leaving": leaving},
    )
    return row["amount"], row["n"]


def _wallet(conn, wallet: Wallet, buckets: dict, now: float, leaving: int | None = None) -> dict | None:
    """One wallet, or None unless its weekly limit is set and its weekly bucket is
    stored. `buckets` is the stored quota by (runtime, label); the work `leaving`, if
    any, commits nothing.

    Each carries `id`, `currency`, `name` (the wallet's runtime and group),
    `available` (what may be committed to a new work now: the lesser of what the 5h
    window and the week let it draw, less what is committed), `committed` and `works`
    (what the open works commit on this wallet, and how many do), `spare` (what may go
    to work that can wait: what is left above the line the share times 1 - e² draws,
    e being the part of the week gone; 0
    while the owner withholds it), `more_at` (epoch seconds when what limits it
    next lets more through, None when that reset is not stored), and `window` and
    `window_size` (the period that limits `available` — "5h window" or "week" — and its
    size: the window's setting, or the week's limit less the reserve).

    The window limits only when its size is set and its bucket is stored. The
    amount of a period counts as available from CREDIT_SECONDS before its reset.
    """
    limit = _number(core.get_setting(conn, week_key(wallet.id)))
    week = buckets.get((wallet.runtime, wallet.week_bucket))
    if not limit or week is None:
        return None
    reserve = (_number(core.get_setting(conn, reserve_key(wallet.id))) or 0) / 100
    left = max(0, week["remaining_fraction"] * limit - reserve * limit)
    share = limit * (1 - reserve)
    size = _number(core.get_setting(conn, window_key(wallet.id))) if wallet.window_bucket else None
    five_hours = buckets.get((wallet.runtime, wallet.window_bucket))
    window_cap = math.inf
    window_credit = 0
    if size and five_hours is not None:
        window_cap = five_hours["remaining_fraction"] * size
        if five_hours["reset_time"] and int(five_hours["reset_time"]) - now <= CREDIT_SECONDS:
            window_credit = size
        window_cap += window_credit
    week_credit = share if week["reset_time"] and int(week["reset_time"]) - now <= CREDIT_SECONDS else 0
    week_cap = left + week_credit
    committed, works = _committed(conn, wallet.id, leaving)
    available = max(0, min(window_cap, week_cap) - committed)
    gone = 1 - min(1, (int(week["reset_time"]) - now) / _WEEK_SECONDS) if week["reset_time"] else 0
    withheld = core.get_setting(conn, withhold_key(wallet.id)) == "1"
    if week_cap <= window_cap:
        reset, credit, period = week["reset_time"], week_credit, _WEEK_SECONDS
        window, window_size = "week", share
    else:
        reset, credit, period = five_hours["reset_time"], window_credit, WINDOW_SECONDS
        window, window_size = "5h window", size
    more_at = None
    if reset:
        more_at = int(reset) - CREDIT_SECONDS + (period if credit else 0)
    return {
        "id": wallet.id,
        "currency": wallet.currency,
        "name": wallet.name,
        "available": available,
        "committed": committed,
        "works": works,
        "spare": 0 if withheld else max(0, min(available, left - committed - share * (1 - gone ** 2))),
        "more_at": more_at,
        "window": window,
        "window_size": window_size,
    }


def overview(conn) -> list[dict]:
    """Every wallet whose weekly limit is set and whose weekly bucket is stored, in the
    order of WALLETS."""
    buckets = {(q["runtime"], q["label"]): q for q in core.quota_buckets(conn)}
    now = time.time()
    return [w for wallet in WALLETS if (w := _wallet(conn, wallet, buckets, now)) is not None]


def available(conn, wallet: str, leaving: int | None = None) -> dict | None:
    """One wallet, by id, as overview() returns it, or None where overview() has none.
    The work `leaving`, if any, commits nothing."""
    buckets = {(q["runtime"], q["label"]): q for q in core.quota_buckets(conn)}
    return _wallet(conn, BY_ID[wallet], buckets, time.time(), leaving)
