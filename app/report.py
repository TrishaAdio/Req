"""The twice-daily owner report.

At 00:00 and 12:00 India Standard Time the owner is told how the last 12 hours
went: how many NEW users turned up (a first-ever join request) and how many were
RETURNING (a request from someone already in the audience). Each request is
counted as it arrives; the counters reset the moment a report is sent.

data/report.json  ->  {"v": 1, "new": N, "returning": R, "since": <unix>}

The counters live in a file, so a restart does not lose a half-finished window —
the next report simply covers from "since" to now, which is why the message
states the exact window it is reporting on rather than assuming a clean 12h.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from . import config, log, owner
from .storage import JsonStore

_logger = log.get("report")
_store = JsonStore(
    config.REPORT_FILE,
    lambda: {"v": 1, "new": 0, "returning": 0, "since": int(time.time())},
)

# India Standard Time is a fixed UTC+5:30 offset (no daylight saving), so a plain
# fixed-offset zone is exact and needs no tz database.
IST = timezone(timedelta(hours=5, minutes=30))


def record(is_new: bool) -> None:
    """Count one join request toward the current window. Hot path — only marks
    the store dirty and lets autoflush coalesce the writes."""
    data = _store.data
    key = "new" if is_new else "returning"
    data[key] = int(data.get(key, 0)) + 1
    _store.mark_dirty()


def _snapshot_and_reset() -> tuple[int, int, int]:
    """Read the window's totals, then start a fresh window from now."""
    data = _store.data
    new = int(data.get("new", 0))
    returning = int(data.get("returning", 0))
    since = int(data.get("since") or time.time())
    data["new"] = 0
    data["returning"] = 0
    data["since"] = int(time.time())
    _store.save()
    return new, returning, since


def _fmt(ts: int) -> str:
    return datetime.fromtimestamp(ts, IST).strftime("%d %b %H:%M")


def _next_boundary(now: datetime) -> datetime:
    """The next 00:00 or 12:00 IST strictly after `now`."""
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for candidate in (midnight,
                      midnight + timedelta(hours=12),
                      midnight + timedelta(hours=24)):
        if candidate > now:
            return candidate
    return midnight + timedelta(hours=24)


def _message(new: int, returning: int, since: int, now_ts: int) -> str:
    return (
        "📊 <b>12-hour report</b> · IST\n"
        f"{_fmt(since)} → {_fmt(now_ts)}\n"
        "\n"
        f"Total new users came: <b>{new}</b>\n"
        f"Returning: <b>{returning}</b>\n"
        "\n"
        f"Total this window: <b>{new + returning}</b>"
    )


async def periodic(bot) -> None:
    """Sleep until the next IST boundary, send the report, reset, repeat."""
    if not config.PERIODIC_REPORT:
        return
    while True:
        now = datetime.now(IST)
        target = _next_boundary(now)
        try:
            await asyncio.sleep((target - now).total_seconds())
        except asyncio.CancelledError:
            raise
        try:
            new, returning, since = _snapshot_and_reset()
            await owner.notify(bot, _message(new, returning, since, int(time.time())))
            _logger.info("12h report sent: new=%s returning=%s", new, returning)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a bad report must not end the loop
            _logger.error("report failed: %s: %s", type(exc).__name__, exc)
