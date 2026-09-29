"""Quota as three wallets: each runtime's subscription counted in its own currency,
one unit being one dollar at the vendor's API list prices."""

from __future__ import annotations

from office import core

#: A runtime's currency.
CURRENCY = {"claude": "CL", "codex": "CD", "agy": "GM"}

#: The quota bucket that is each runtime's weekly wallet; payday is its reset time.
_WEEK_BUCKET = {"claude": "week (all models)", "codex": "week", "agy": "week"}

#: The quota bucket that limits what may be spent right now.
_WINDOW_BUCKET = "5h"


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


def overview(conn) -> list[dict]:
    """The wallet of every runtime whose weekly limit is set and whose weekly bucket
    is stored, in the order claude, codex, agy.

    Each carries `runtime`, `currency`, `left` (what the team may spend before
    payday: the weekly remainder less the reserve, not below zero), `share` (the
    team's weekly share: the weekly limit less the reserve), `window_left` and
    `window` (what the 5h window lets it spend now, and its limit; None unless the
    5h window is set and its bucket is stored), `payday` and `next_window`.
    """
    buckets = {(q["runtime"], q["label"]): q for q in core.quota_buckets(conn)}
    wallets = []
    for runtime, currency in CURRENCY.items():
        limit = _number(core.get_setting(conn, week_key(runtime)))
        week = buckets.get((runtime, _WEEK_BUCKET[runtime]))
        if not limit or week is None:
            continue
        reserve = (_number(core.get_setting(conn, reserve_key(runtime))) or 0) / 100
        left = max(0, week["remaining_fraction"] * limit - reserve * limit)
        window = _number(core.get_setting(conn, window_key(runtime)))
        five_hours = buckets.get((runtime, _WINDOW_BUCKET))
        has_window = window and five_hours is not None
        wallets.append({
            "runtime": runtime,
            "currency": currency,
            "left": left,
            "share": limit * (1 - reserve),
            "window_left": min(five_hours["remaining_fraction"] * window, left) if has_window else None,
            "window": window if has_window else None,
            "payday": week["reset_time"],
            "next_window": five_hours["reset_time"] if has_window else None,
        })
    return wallets
