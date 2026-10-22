"""The owner side-channel: status pings and the startup audience backup.

Everything here is addressed to config.OWNER_ID and fails quietly. A bot that
cannot reach its owner — the owner never pressed Start, or blocked the bot —
must still go on serving join requests, so a failed DM is logged and swallowed,
never raised.
"""
from __future__ import annotations

import io
import time

from . import config, log, users

_logger = log.get("owner")


async def notify(bot, text: str) -> bool:
    """DM the owner. True when it was actually sent."""
    if not config.NOTIFY_OWNER or not config.OWNER_ID:
        return False
    try:
        await bot.send_message(config.OWNER_ID, text, link_preview=False)
        return True
    except Exception as exc:  # noqa: BLE001 - a missed ping must never crash us
        _logger.warning("could not message owner %s: %s",
                        config.OWNER_ID, type(exc).__name__)
        return False


async def send_backup(bot, reason: str = "startup") -> bool:
    """Send the whole user store to the owner as a JSON file, so the audience
    is preserved off the box. True when it was delivered."""
    if not config.OWNER_ID:
        return False
    try:
        payload = users.export_json()
        stream = io.BytesIO(payload.encode("utf-8"))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        stream.name = f"users-backup-{stamp}.json"
        await bot.send_file(
            config.OWNER_ID,
            stream,
            caption=(f"<b>Backup</b> · {reason}\n"
                     f"{users.total()} users · {stamp}"),
        )
        _logger.info("sent %s-backup to owner (%s users)", reason, users.total())
        return True
    except Exception as exc:  # noqa: BLE001 - backup failure must not stop boot
        _logger.warning("backup to owner failed: %s: %s",
                        type(exc).__name__, exc)
        return False
