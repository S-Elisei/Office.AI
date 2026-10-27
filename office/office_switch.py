"""The owner's run/pause controls and their displayed state."""

from datetime import datetime, timedelta


def snapshot(bus) -> dict:
    timer = bus.switch_timer()
    return {
        "running": bus.running,
        "timer": None if timer is None else {
            "to": timer[0], "at": datetime.fromtimestamp(timer[1]).strftime("%H:%M"),
        },
        "finishing": 0 if bus.running else len(bus.live_turns()),
        "zone": datetime.now().astimezone().strftime("%z"),
    }


def set_timer(bus, to: str, clock: str, actor: str) -> None:
    hour, minute = map(int, clock.split(":"))
    now = datetime.now().astimezone()
    at = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if at <= now:
        at += timedelta(days=1)
    bus.set_switch_timer(to, int(at.timestamp()), actor)
