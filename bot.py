"""Entry point.

What this bot is for
--------------------
Telegram lets a bot message a user it has never talked to in exactly one case:
that user has a PENDING JOIN REQUEST in a channel where the bot is an admin.
This bot lives on that rule.

  1. Someone asks to join one of the served channels.
  2. The bot sees the request live and sends them the saved post.
  3. That user is kept in the audience, so the owner can /bcast to them later.

Channels are added at runtime with /add <chat_id>; every added channel is served
in addition to the ones already there, so one bot covers as many channels as
you like without a restart.

Run:  python bot.py
"""
from __future__ import annotations

import asyncio
import logging
import signal

from telethon import TelegramClient

from app import (
    __version__,
    broadcast,
    buttons,
    channels,
    config,
    handlers,
    log,
    post,
    storage,
    users,
)

_logger = log.get("boot")


def build() -> TelegramClient:
    return TelegramClient(
        config.SESSION,
        config.API_ID,
        config.API_HASH,
        connection_retries=None,   # keep reconnecting forever
        retry_delay=5,
        # Raise flood waits instead of sleeping silently: the broadcast engine
        # backs every worker off together when one appears.
        flood_sleep_threshold=0,
    )


async def warm_channels(bot: TelegramClient) -> None:
    """Pre-resolve served channels so the first welcome cannot fail on a cold
    session (fetching the saved post by numeric id needs the access hash)."""
    for chat_id, title in channels.items():
        try:
            entity = await bot.get_entity(chat_id)
        except Exception as exc:  # noqa: BLE001
            _logger.warning("%s not resolvable yet (%s) — will retry on the "
                            "first request", chat_id, type(exc).__name__)
            continue
        name = getattr(entity, "title", "") or title
        channels.set_title(chat_id, name)
        _logger.info("serving %s %s", log.val(chat_id), log.dim(name))


def show_banner(me) -> None:
    counts = users.counts()
    rate = config.BROADCAST_WORKERS / max(config.SEND_DELAY_SECONDS, 0.001)
    admins = ", ".join(str(i) for i in sorted(config.ADMIN_IDS)) or log.err("not set")
    scope = (f"{channels.count()} served" if channels.count()
             else ("none added — /add required" if config.STRICT_CHANNELS
                   else "none added — every admined chat"))
    audience = "{} {}".format(
        counts["total"], log.dim("reachable {}".format(counts["reachable"]))
    )
    pacing = "{} worker(s), {}s delay {}".format(
        config.BROADCAST_WORKERS,
        config.SEND_DELAY_SECONDS,
        log.dim("~{:.1f}/s".format(rate)),
    )
    log.banner(
        "welcome + broadcast bot " + log.dim("v" + __version__),
        [
            ("bot", f"@{me.username} {log.dim(str(me.id))}"),
            ("owner", admins),
            ("channels", scope),
            ("welcome", "once per user" if config.WELCOME_ONCE
                        else "every join request"),
            ("post", log.ok(post.kind() or "set") if post.exists()
                     else log.warn("none — /setpost")),
            ("buttons", str(buttons.count())),
            ("users", audience),
            ("pacing", pacing),
            ("data", str(config.DATA_DIR)),
        ],
    )


async def main() -> None:
    log.setup()
    config.require()
    if not config.ADMIN_IDS:
        _logger.warning("OWNER_ID is not set — nobody can command the bot yet")

    users.import_legacy()

    bot = build()
    caster = broadcast.Broadcaster(bot)
    handlers.register(bot, caster)

    await bot.start(bot_token=config.BOT_TOKEN)
    bot.parse_mode = "html"
    me = await bot.get_me()

    await warm_channels(bot)
    show_banner(me)

    flusher = asyncio.create_task(storage.autoflush())

    stopping = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stopping.set)
        except NotImplementedError:  # pragma: no cover - non-unix
            pass

    if config.STRICT_CHANNELS and not channels.count():
        _logger.warning("no channel added — nothing is served yet; use /add")

    _logger.info("listening for join requests")
    waiter = asyncio.create_task(bot.run_until_disconnected())
    stopper = asyncio.create_task(stopping.wait())
    try:
        done, _ = await asyncio.wait({waiter, stopper},
                                     return_when=asyncio.FIRST_COMPLETED)
        if waiter in done:
            # Re-raise whatever ended the connection (revoked token, auth
            # error): exiting 0 on those looks exactly like a clean stop.
            waiter.result()
    finally:
        for task in (waiter, stopper):
            task.cancel()
        _logger.info("shutting down")
        # Stop a broadcast and let its workers record their last results before
        # anything is written, otherwise those verdicts are lost.
        if caster.cancel():
            _logger.info("cancelling the running broadcast")
            for _ in range(40):
                if not caster.running:
                    break
                await asyncio.sleep(0.25)
        flusher.cancel()
        try:
            storage.flush_all(force=True)
        except Exception:  # noqa: BLE001 - never skip the disconnect
            _logger.exception("final flush failed")
        await bot.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except SystemExit:
        raise
    except Exception:
        # systemd restarts us; make sure the reason is in the log first.
        logging.getLogger("boot").exception("bot stopped with an error")
        raise SystemExit(1)
