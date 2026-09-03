"""Everything the bot reacts to: join requests, and the owner's commands.

Owner commands work wherever the owner writes them — the bot's DM, a group, a
topic — they are gated on the sender's user id, not on the chat type.
"""
from __future__ import annotations

import asyncio
import html
import io
import re

from telethon import TelegramClient, events, utils
from telethon.errors import MessageNotModifiedError
from telethon.tl.types import UpdateBotChatInviteRequester

from . import broadcast, buttons, channels, config, copier, log, post, users

_logger = log.get("bot")

PANEL = (
    "<b>Panel</b>\n"
    "\n"
    "<code>/add</code> chat_id — serve that channel's join requests\n"
    "<code>/remove</code> chat_id — stop serving it\n"
    "<code>/chats</code> — served channels\n"
    "<code>/setpost</code> — reply to a message to make it the post\n"
    "<code>/clearpost</code> — drop the saved post\n"
    "<code>/setbutton</code> — inline URL buttons for the post\n"
    "<code>/clearbutton</code> — drop the buttons\n"
    "<code>/preview</code> — send the post to yourself\n"
    "<code>/bcast</code> — send the post (or a replied one) to every user\n"
    "<code>/cancel</code> — stop a running broadcast\n"
    "<code>/stats</code> — users, channels, post\n"
    "<code>/export</code> — download the user list"
)

BUTTON_USAGE = (
    "<b>Buttons</b>\n"
    "\n"
    "<code>/setbutton Join - https://t.me/yourchannel</code>\n"
    "\n"
    "One row per line, <code>|</code> splits a row:\n"
    "<blockquote>/setbutton\n"
    "Join - https://t.me/x | Chat - https://t.me/y\n"
    "Site - https://example.com</blockquote>"
)


def _cmd(*names: str) -> re.Pattern:
    """Match /name, /name@botusername, with or without arguments."""
    return re.compile(rf"^/(?:{'|'.join(names)})(?:@\w+)?(?:\s+([\s\S]*))?$", re.I)


def _args(event) -> str:
    parts = (event.raw_text or "").split(None, 1)
    return parts[1].strip() if len(parts) > 1 else ""


def _esc(text: str) -> str:
    return html.escape(str(text or ""))


def _label(chat_id: int, title: str = "") -> str:
    title = title or channels.title_of(chat_id)
    tag = f"<code>{chat_id}</code>"
    return f"{tag} — {_esc(title)}" if title else tag


def _who(user) -> tuple[str, str]:
    """(display name, username) for logging and the user store."""
    if user is None:
        return "", ""
    name = utils.get_display_name(user) or ""
    return name, getattr(user, "username", "") or ""


def register(bot: TelegramClient, caster: broadcast.Broadcaster) -> None:
    welcome_gate = asyncio.Semaphore(config.WELCOME_CONCURRENCY)
    running_tasks: set[asyncio.Task] = set()
    missing_post_warned = False

    def owner_only(handler):
        async def wrapper(event):
            if not config.is_admin(event.sender_id):
                return
            try:
                await handler(event)
            except Exception as exc:  # noqa: BLE001 - report, never die
                _logger.exception("%s failed", handler.__name__)
                await event.reply(f"Failed: {type(exc).__name__}: {_esc(exc)}")

        wrapper.__name__ = handler.__name__
        return wrapper

    def on(*names: str):
        def decorator(handler):
            bot.add_event_handler(
                owner_only(handler), events.NewMessage(pattern=_cmd(*names))
            )
            return handler

        return decorator

    async def resolve_chat(ident):
        """(chat_id, title) for owner input, or (None, error) if unresolvable."""
        try:
            entity = await bot.get_entity(ident)
        except Exception as exc:  # noqa: BLE001
            if isinstance(ident, int):
                # A bare id cannot be resolved until the bot has seen the chat;
                # accept it anyway and learn the title on the first request.
                return ident, ""
            return None, f"could not resolve {_esc(ident)} ({type(exc).__name__})"
        return utils.get_peer_id(entity), getattr(entity, "title", "") or ""

    # --- join requests ----------------------------------------------------
    def markup():
        try:
            return buttons.to_markup()
        except Exception as exc:  # noqa: BLE001 - unusable buttons file
            _logger.error("ignoring unusable buttons: %s", type(exc).__name__)
            return None

    async def send_post(target) -> copier.Result:
        """One attempt at sending the saved post to one peer."""
        message = await post.resolve(bot)
        if message is None or not post.is_sendable(message):
            return copier.Result("no_post")
        return await copier.copy_to(bot, target, message, markup(),
                                    on_refresh=post.remember)

    async def deliver(target, user_id: int, claimed: bool = True) -> copier.Result:
        """Send the post to a requester, honouring the shared flood gate.

        The gate is the broadcaster's: a flood wait discovered here also slows a
        running broadcast down, and vice versa, instead of both sides finding
        the same limit separately. The wait happens OUTSIDE the welcome
        semaphore so a slow retry doesn't block other requesters.
        """
        await caster.gate()
        async with welcome_gate:
            result = await send_post(target)

        if result.status == copier.FLOOD:
            # Hold the shared gate for the whole wait either way — Telegram will
            # refuse everyone else's sends too — but only retry this requester
            # if the wait is short enough to be worth it.
            caster.hold(result.flood + 1)
            if result.flood <= config.MAX_FLOOD_WAIT:
                await caster.gate()
                async with welcome_gate:
                    result = await send_post(target)

        if result.status in copier.USER_VERDICTS:
            users.set_status(user_id, result.status)
        if result.ok:
            # Re-stamp on every delivery, so a returning requester's record
            # shows the welcome they just got, not the one from months ago.
            users.mark_welcome(user_id)
        else:
            # Nothing was delivered: give back the day's allowance, and the
            # welcome claim too, so a later request can try again.
            users.release_send(user_id)
            if claimed:
                users.release_welcome(user_id)
        return result

    async def on_join_request(update: UpdateBotChatInviteRequester):
        nonlocal missing_post_warned
        chat_id = utils.get_peer_id(update.peer)
        user_id = update.user_id

        if not channels.accepts(chat_id):
            _logger.debug("join request in %s ignored (not served)", chat_id)
            return

        # Resolving now also caches the user's access hash from this very
        # update, which is what makes the DM below possible.
        target = user_id
        name = username = ""
        try:
            entity = await bot.get_entity(user_id)
            target = entity
            name, username = _who(entity)
        except Exception:  # noqa: BLE001
            pass

        is_new = users.add(user_id, name, username, chat_id)
        if channels.count() and not channels.title_of(chat_id):
            try:
                chat = await bot.get_entity(update.peer)
                channels.set_title(chat_id, getattr(chat, "title", "") or "")
            except Exception:  # noqa: BLE001
                pass

        who = log.name(f"{name or user_id}")
        # Taken before the welcome claim below, so a capped requester leaves
        # nothing to release — and after users.add(), so they are in the
        # audience for a later /bcast even though today's post is not sent.
        if not users.reserve_send(user_id):
            _logger.info("%s requested %s — %s", who, log.val(chat_id),
                         log.dim(f"{users.sends_today(user_id)} in 24h, at the cap"))
            return

        # Claim the welcome BEFORE sending: two requests from the same person
        # (two channels, same second) arrive as two concurrent tasks, and the
        # claim is what stops both of them sending. A user who was welcomed
        # before cannot claim it again — under WELCOME_ONCE that ends the
        # request here, otherwise the send goes ahead unclaimed.
        claimed = users.claim_welcome(user_id)
        if not claimed and config.WELCOME_ONCE:
            _logger.info("%s requested %s — %s", who, log.val(chat_id),
                         log.dim("already welcomed, WELCOME_ONCE"))
            return

        try:
            result = await deliver(target, user_id, claimed)
        except Exception:  # noqa: BLE001 - a claim must never be left dangling
            _logger.exception("welcome for %s failed", user_id)
            users.release_send(user_id)
            if claimed:
                users.release_welcome(user_id)
            return

        if result.ok:
            _logger.info("%s requested %s — %s%s", who, log.val(chat_id),
                         log.ok("sent"), "" if is_new else log.dim(" (returning)"))
        elif result.status == "no_post":
            if not missing_post_warned:
                _logger.warning("no saved post — use /setpost (users are still collected)")
                missing_post_warned = True
        else:
            _logger.warning("%s requested %s — %s", who, log.val(chat_id),
                            log.err(str(result)))

    bot.add_event_handler(on_join_request, events.Raw(UpdateBotChatInviteRequester))

    # --- discovery aid: who is talking to a bot with no OWNER_ID set -------
    seen_strangers: set[int] = set()

    async def on_stranger(event):
        if config.ADMIN_IDS or not event.is_private:
            return
        if event.sender_id in seen_strangers:
            return
        seen_strangers.add(event.sender_id)
        _logger.warning("message from user id %s — set OWNER_ID to that id to "
                        "control the bot", log.val(event.sender_id))

    bot.add_event_handler(on_stranger, events.NewMessage(incoming=True))

    # --- panel ------------------------------------------------------------
    @on("start", "help", "panel")
    async def start(event):
        await event.reply(PANEL)

    # --- channels ---------------------------------------------------------
    @on("add")
    async def add(event):
        raw = _args(event)
        if not raw:
            await event.reply("Usage: <code>/add -1001234567890</code>")
            return

        added, known, failed = [], [], []
        for token in raw.replace(",", " ").split():
            ident = channels.parse_ident(token)
            if ident is None:
                failed.append(f"{_esc(token)} — not a chat id")
                continue
            chat_id, title = await resolve_chat(ident)
            if chat_id is None:
                failed.append(title)
                continue
            (added if channels.add(chat_id, title) else known).append(
                _label(chat_id, title)
            )

        lines = []
        if added:
            lines.append("<b>Added</b>\n" + "\n".join(added))
        if known:
            lines.append("<b>Already served</b>\n" + "\n".join(known))
        if failed:
            lines.append("<b>Skipped</b>\n" + "\n".join(failed))
        lines.append(f"Channels: <b>{channels.count()}</b>")
        await event.reply("\n\n".join(lines))
        _logger.info("channels: %s served", log.val(channels.count()))

    @on("remove", "rm", "del")
    async def remove(event):
        raw = _args(event)
        ident = channels.parse_ident(raw)
        if ident is None:
            await event.reply("Usage: <code>/remove -1001234567890</code>")
            return
        chat_id, title = await resolve_chat(ident)
        if chat_id is None:
            await event.reply(title)
            return
        gone = channels.remove(chat_id)
        await event.reply(
            (f"Removed {_label(chat_id, title)}" if gone
             else f"{_label(chat_id, title)} was not served")
            + f"\nChannels: <b>{channels.count()}</b>"
        )

    @on("chats", "channels", "list")
    async def chats(event):
        items = channels.items()
        if not items:
            mode = ("Nothing is served — add a channel with /add."
                    if config.STRICT_CHANNELS
                    else "No channel added — every chat the bot administers is served.")
            await event.reply(mode)
            return
        counts = users.per_chat()
        lines = [
            f"{_label(chat_id, title)} · {counts.get(chat_id, 0)} first seen here"
            for chat_id, title in items
        ]
        await event.reply(f"<b>Channels ({len(items)})</b>\n\n" + "\n".join(lines))

    # --- the post ---------------------------------------------------------
    @on("setpost")
    async def setpost(event):
        source = await event.get_reply_message()
        if source is None:
            await event.reply("Reply to the message you want to send, with /setpost.")
            return
        if not post.is_sendable(source):
            await event.reply("That message has no text and no media.")
            return
        if getattr(source, "grouped_id", None):
            # Only one message id is stored, so an album would arrive as a
            # single item — say so rather than silently dropping the rest.
            await event.reply("That is an album. Send the media as one message.")
            return

        kind = post.describe(source)
        post.save(utils.get_peer_id(source.peer_id), source.id, kind)
        # Confirm the bot can fetch it back — a cold entity cache would only
        # show up later, on the first join request.
        reachable = await post.resolve(bot, use_cache=False) is not None
        await event.reply(
            f"Post saved · {kind}"
            + ("" if reachable else "\nThe bot cannot read it back — set it from "
                                    "a chat the bot is in.")
        )
        _logger.info("post saved: %s %s", kind, log.val(source.id))

    @on("clearpost")
    async def clearpost(event):
        nonlocal missing_post_warned
        missing_post_warned = False  # warn again next time a requester arrives
        await event.reply("Post cleared." if post.clear() else "No post saved.")

    @on("preview")
    async def preview(event):
        message = await post.resolve(bot, use_cache=False)
        if message is None:
            await event.reply("No post saved.")
            return
        result = await copier.copy_to(bot, event.sender_id, message, markup(),
                                      on_refresh=post.remember)
        if not result.ok:
            await event.reply(f"Preview failed: {_esc(result)}")

    # --- buttons ----------------------------------------------------------
    @on("setbutton", "setbuttons")
    async def setbutton(event):
        body = _args(event)
        if not body:
            await event.reply(BUTTON_USAGE)
            return
        rows, error = buttons.parse(body)
        if error:
            await event.reply(_esc(error))
            return
        buttons.save(rows)
        await event.reply(f"Buttons: {_esc(buttons.describe())}")

    @on("clearbutton", "clearbuttons")
    async def clearbutton(event):
        await event.reply("Buttons cleared." if buttons.clear() else "No buttons set.")

    # --- stats ------------------------------------------------------------
    @on("stats", "status")
    async def stats(event):
        count = users.counts()
        lines = [
            "<b>Stats</b>",
            "",
            f"Users <b>{count['total']}</b> · new today {count['new_today']}",
            f"Reachable {count['reachable']} · welcomed {count['welcomed']}",
        ]
        if config.DAILY_USER_LIMIT:
            lines.append(f"Daily cap {config.DAILY_USER_LIMIT} per user"
                         f" · at it now {count['capped']}")
        dead = " · ".join(
            f"{label} {count[label]}"
            for label in ("blocked", "deleted", "invalid", "is_bot")
            if count[label]
        )
        if dead:
            lines.append(dead)
        lines.append(f"Channels {channels.count()}")
        lines.append(f"Post {post.kind() if post.exists() else 'none'}"
                     f" · buttons {buttons.count()}")
        if caster.running and caster.stats:
            lines.append(f"Broadcast {caster.stats.sent}/{caster.stats.total}")
        else:
            lines.append("Broadcast idle")
        await event.reply("\n".join(lines))

    @on("export")
    async def export(event):
        # The whole audience goes in this file, so it is only ever sent to the
        # requester's own chat with the bot, never into a group.
        payload = await asyncio.to_thread(users.export_json)
        stream = io.BytesIO(payload.encode("utf-8"))
        stream.name = "users.json"
        await bot.send_file(event.sender_id, stream,
                            caption=f"{users.total()} users")
        if not event.is_private:
            await event.reply("Sent to your chat with the bot.")

    # --- broadcast --------------------------------------------------------
    @on("bcast", "broadcast")
    async def bcast(event):
        message = await event.get_reply_message()
        if message is None:
            message = await post.resolve(bot, use_cache=False)
        if message is None:
            await event.reply("Nothing to send — reply to a post or use /setpost.")
            return
        if not post.is_sendable(message):
            await event.reply("That message has no text and no media.")
            return

        targets = users.reachable_ids()
        if not targets:
            await event.reply("No users yet.")
            return

        # Claim the engine here, synchronously. run() only reaches its own flag
        # a turn later, which is long enough for a second /bcast to slip past.
        if not caster.reserve():
            await event.reply("A broadcast is already running.")
            return

        try:
            status = await event.reply(f"Broadcasting to <b>{len(targets)}</b> users...")
        except Exception:
            caster.release()
            raise

        async def on_progress(stats):
            try:
                await status.edit(broadcast.render(stats, running=True))
            except MessageNotModifiedError:
                pass

        async def run():
            try:
                stats = await caster.run(message, targets, on_progress)
                await status.edit(broadcast.render(stats))
            except Exception as exc:  # noqa: BLE001
                _logger.exception("broadcast failed")
                caster.release()
                try:
                    await status.edit(f"Broadcast failed: {_esc(type(exc).__name__)}")
                except Exception:  # noqa: BLE001
                    pass

        # Keep a reference so the task cannot be garbage collected mid-run.
        task = asyncio.create_task(run())
        running_tasks.add(task)
        task.add_done_callback(running_tasks.discard)

    @on("cancel", "stop")
    async def cancel(event):
        await event.reply("Cancelling." if caster.cancel() else "Nothing is running.")
