"""The saved post — the single message the bot re-sends.

Only its location (chat id + message id) is stored; the content itself stays on
Telegram's servers, so it can be re-sent verbatim by reference (no re-upload) to
any number of people: media, caption, formatting, spoilers, premium emoji.

Fetching a message by a bare numeric chat id needs that chat's access_hash in
the session cache. On a fresh session that cache is cold, so resolve() warms it
through the served channels before giving up, and keeps the resolved message
briefly cached so a join raid doesn't refetch it once per user.
"""
from __future__ import annotations

import time

from telethon import TelegramClient, utils
from telethon.tl.types import Message, MessageMediaWebPage

from . import channels, config, log
from .storage import JsonStore

_logger = log.get("post")
_store = JsonStore(config.POST_FILE, lambda: {})

# Short on purpose: an edit to the post should go live quickly, while a join
# raid still gets one fetch instead of one per requester.
CACHE_TTL = 60.0
_cache: tuple[Message, float] | None = None


# --- stored reference -----------------------------------------------------
def save(chat_id: int, message_id: int, kind: str = "") -> None:
    global _cache
    _store.data.clear()
    _store.data.update(
        {
            "chat_id": int(chat_id),
            "message_id": int(message_id),
            "kind": kind,
            "t": int(time.time()),
        }
    )
    _store.save()
    _cache = None


def load() -> dict | None:
    data = _store.data
    if data.get("chat_id") and data.get("message_id"):
        return data
    return None


def clear() -> bool:
    global _cache
    _cache = None
    had = load() is not None
    _store.delete()
    return had


def exists() -> bool:
    return load() is not None


def kind() -> str:
    return (load() or {}).get("kind", "")


def is_sendable(message: Message) -> bool:
    """A message can be copied only if it carries media or text."""
    if message is None:
        return False
    media = getattr(message, "media", None)
    return bool(media) or bool(getattr(message, "message", ""))


def describe(message: Message) -> str:
    """Short human label for a message: photo, video, text..."""
    media = getattr(message, "media", None)
    if media is None or isinstance(media, MessageMediaWebPage):
        return "text"
    for attr, label in (
        ("photo", "photo"),
        ("video", "video"),
        ("voice", "voice"),
        ("audio", "audio"),
        ("gif", "gif"),
        ("sticker", "sticker"),
        ("document", "document"),
    ):
        if getattr(message, attr, None):
            return label
    return "media"


# --- resolving to a live Message -----------------------------------------
def invalidate() -> None:
    global _cache
    _cache = None


def remember(message: Message) -> None:
    """Cache a message we already hold (e.g. one refetched for a fresh file
    reference) so the next reader doesn't fetch it again."""
    global _cache
    info = load()
    if info and message is not None and getattr(message, "id", None) == info["message_id"]:
        _cache = (message, time.monotonic())


async def _warm_peer(bot: TelegramClient, chat_id: int):
    """Get a usable peer for the saved post's chat, warming the cache if needed.

    A bot cannot list its dialogs, so the served channels are the way in:
    resolving one of them populates the entity cache for its id.
    """
    try:
        return await bot.get_input_entity(chat_id)
    except (ValueError, TypeError):
        pass
    for ident in channels.ids():
        try:
            entity = await bot.get_entity(ident)
        except Exception as exc:  # noqa: BLE001
            _logger.debug("could not warm %s: %s", ident, type(exc).__name__)
            continue
        if utils.get_peer_id(entity) == chat_id:
            return entity
    return chat_id


async def resolve(bot: TelegramClient, use_cache: bool = True) -> Message | None:
    """Return the saved post as a live Message, or None if unset/unreachable."""
    global _cache

    info = load()
    if not info:
        return None

    if use_cache and _cache and (time.monotonic() - _cache[1]) < CACHE_TTL:
        return _cache[0]

    chat_id, message_id = info["chat_id"], info["message_id"]

    message = None
    try:
        message = await bot.get_messages(chat_id, ids=message_id)
    except Exception as exc:  # noqa: BLE001
        _logger.debug("direct fetch of %s/%s failed (%s); warming cache",
                      chat_id, message_id, type(exc).__name__)
        try:
            peer = await _warm_peer(bot, chat_id)
            message = await bot.get_messages(peer, ids=message_id)
        except Exception as warm_exc:  # noqa: BLE001
            _logger.error(
                "saved post %s/%s is unreachable (%s: %s) — re-run /setpost",
                chat_id, message_id, type(warm_exc).__name__, warm_exc,
            )
            return None

    if message is None:
        _logger.error("saved post %s/%s no longer exists — re-run /setpost",
                      chat_id, message_id)
        return None

    _cache = (message, time.monotonic())
    return message
