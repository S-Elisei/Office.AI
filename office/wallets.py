"""Quota as three wallets: each runtime's subscription counted in its own currency,
one unit being one dollar at the vendor's API list prices."""

from __future__ import annotations

import math
import time

from office import core, db

#: A runtime's currency.
CURRENCY = {"claude": "CL", "codex": "CD", "agy": "GM"}

#: The quota bucket that is each runtime's weekly wallet.
_WEEK_BUCKET = {"claude": "week (all models)", "codex": "week", "agy": "week"}

#: The quota bucket that limits what may be spent right now.
_WINDOW_BUCKET = "5h"

#: The span of one weekly bucket.
_WEEK_SECONDS = 7 * 24 * 3600

#: The span of one 5h window.
WINDOW_SECONDS = 5 * 3600

#: How long before a bucket's reset its next period's amount counts as available.
CREDIT_SECONDS = 3600


def week_key(runtime: str) -> str:
    """The setting holding a runtime's weekly limit, in units."""
    return f"wallet_{runtime}_week"


def window_key(runtime: str) -> str:
    """The setting holding a runtime's 5h window, in units."""
    return f"wallet_{runtime}_window"


def reserve_key(runtime: str) -> str:
    """The setting holding a runtime's reserve: the percentage of its week the team
    leaves untouched."""
    return f"wallet_{runtime}_reserve"


def withhold_key(runtime: str) -> str:
    """The setting that, when "1", shows a runtime's spare as 0."""
    return f"wallet_{runtime}_withhold"


#: Every setting the owner edits per runtime: weekly limit and 5h window in units,
#: reserve in percent.
SETTING_KEYS = tuple(
    key for runtime in CURRENCY
    for key in (week_key(runtime), window_key(runtime), reserve_key(runtime))
)


def _number(raw: str | None) -> float | None:
    return float(raw) if raw and raw.strip() else None


def week_remaining(conn, runtime: str) -> float | None:
    """The remaining fraction of a runtime's weekly bucket, or None when none is stored."""
    return next(
        (q["remaining_fraction"] for q in core.quota_buckets(conn)
         if q["runtime"] == runtime and q["label"] == _WEEK_BUCKET[runtime]),
        None,
    )


def units(value: float) -> str:
    """A wallet figure: whole units from a hundred up, one decimal from ten, two below."""
    if abs(value) >= 100:
        return f"{value:.0f}"
    return f"{value:.1f}" if abs(value) >= 10 else f"{value:.2f}"


def _committed(conn, runtime: str, leaving: int | None) -> tuple[float, int]:
    """What the open works other than work `leaving` commit on a runtime's wallet, and
    how many of them do."""
    row = db.query_one(
        conn,
        "SELECT COALESCE(SUM(CASE WHEN price_runtime = :r THEN price ELSE 0 END), 0) "
        "+ COALESCE(SUM(CASE WHEN coordination_runtime = :r THEN coordination ELSE 0 END), 0) "
        "AS amount, COUNT(*) AS n FROM works "
        "WHERE (price_runtime = :r OR coordination_runtime = :r) AND id IS NOT :leaving",
        {"r": runtime, "leaving": leaving},
    )
    return row["amount"], row["n"]


def _wallet(conn, runtime: str, buckets: dict, now: float, leaving: int | None = None) -> dict | None:
    """The wallet of one runtime, or None unless its weekly limit is set and its weekly
    bucket is stored. `buckets` is the stored quota by (runtime, label); the work
    `leaving`, if any, commits nothing.

    Each carries `runtime`, `currency`, `available` (what may be committed to a new
    work now: the lesser of what the 5h window and the week let it draw, less what is
    committed), `committed` and `works` (what the open works commit on this runtime,
    and how many do), `spare` (what may go to work that can wait: what is left above
    the line the share times 1 - e² draws, e being the part of the week gone; 0
    while the owner withholds it), `more_at` (epoch seconds when what limits it
    next lets more through, None when that reset is not stored), and `window` and
    `window_size` (the period that limits `available` — "5h window" or "week" — and its
    size: the window's setting, or the week's limit less the reserve).

    The window limits only when its size is set and its bucket is stored. The
    amount of a period counts as available from CREDIT_SECONDS before its reset.
    """
    limit = _number(core.get_setting(conn, week_key(runtime)))
    week = buckets.get((runtime, _WEEK_BUCKET[runtime]))
    if not limit or week is None:
        return None
    reserve = (_number(core.get_setting(conn, reserve_key(runtime))) or 0) / 100
    left = max(0, week["remaining_fraction"] * limit - reserve * limit)
    share = limit * (1 - reserve)
    size = _number(core.get_setting(conn, window_key(runtime)))
    five_hours = buckets.get((runtime, _WINDOW_BUCKET))
    window_cap = math.inf
    window_credit = 0
    if size and five_hours is not None:
        window_cap = five_hours["remaining_fraction"] * size
        if five_hours["reset_time"] and int(five_hours["reset_time"]) - now <= CREDIT_SECONDS:
            window_credit = size
        window_cap += window_credit
    week_credit = share if week["reset_time"] and int(week["reset_time"]) - now <= CREDIT_SECONDS else 0
    week_cap = left + week_credit
    committed, works = _committed(conn, runtime, leaving)
    available = max(0, min(window_cap, week_cap) - committed)
    gone = 1 - min(1, (int(week["reset_time"]) - now) / _WEEK_SECONDS) if week["reset_time"] else 0
    withheld = core.get_setting(conn, withhold_key(runtime)) == "1"
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
        "runtime": runtime,
        "currency": CURRENCY[runtime],
        "available": available,
        "committed": committed,
        "works": works,
        "spare": 0 if withheld else max(0, min(available, left - committed - share * (1 - gone ** 2))),
        "more_at": more_at,
        "window": window,
        "window_size": window_size,
    }


def overview(conn) -> list[dict]:
    """The wallet of every runtime whose weekly limit is set and whose weekly bucket
    is stored, in the order claude, codex, agy."""
    buckets = {(q["runtime"], q["label"]): q for q in core.quota_buckets(conn)}
    now = time.time()
    return [w for runtime in CURRENCY if (w := _wallet(conn, runtime, buckets, now)) is not None]


def available(conn, runtime: str, leaving: int | None = None) -> dict | None:
    """The wallet of one runtime as overview() returns it, or None where overview() has
    none. The work `leaving`, if any, commits nothing."""
    buckets = {(q["runtime"], q["label"]): q for q in core.quota_buckets(conn)}
    return _wallet(conn, runtime, buckets, time.time(), leaving)
