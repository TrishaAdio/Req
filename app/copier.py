"""Copying one message to one user.

"Copy", not forward: the recipient sees a normal message with no "forwarded
from" header. Media is re-sent by reference (nothing is re-uploaded), and the
original entities are passed straight through, which is what keeps bold/links
and premium (custom) emoji intact.
"""
from __future__ import annotations

from dataclasses import dataclass

from telethon import TelegramClient, utils
from telethon.errors import (
    ChatWriteForbiddenError,
    FileReferenceExpiredError,
    FloodWaitError,
    InputUserDeactivatedError,
    PeerFloodError,
    PeerIdInvalidError,
    UserIsBlockedError,
    UserIsBotError,
)
from telethon.tl.types import InputPeerUser, Message, MessageMediaWebPage

from . import log

_logger = log.get("copier")

# A non-premium bot may attach at most this many UTF-16 units as a caption.
# Longer text is sent as a follow-up message so nothing (links!) is dropped.
CAPTION_LIMIT = 1024

SENT = "sent"
# Verdicts about the RECIPIENT — safe to remember against that user.
BLOCKED = "blocked"
DELETED = "deleted"
INVALID = "invalid"
IS_BOT = "is_bot"
USER_VERDICTS = (BLOCKED, DELETED, INVALID, IS_BOT)
# Verdicts about the RUN or the POST — never recorded against a user.
FLOOD = "flood"
PEERFLOOD = "peerflood"
BAD_POST = "bad_post"
ERROR = "error"


class UnsendablePost(Exception):
    """The saved post itself cannot be re-sent (album-only media, a quiz, a
    story...). A property of the post, not of the recipient."""


@dataclass(frozen=True)
class Result:
    status: str
    detail: str = ""
    flood: int = 0

    @property
    def ok(self) -> bool:
        return self.status == SENT

    def __str__(self) -> str:
        if self.status == FLOOD:
            return f"flood {self.flood}s"
        return f"{self.status} {self.detail}".strip()


def utf16_len(text: str) -> int:
    """Telegram measures text in UTF-16 code units, not characters."""
    return len(text.encode("utf-16-le")) // 2


async def resolve_peer(bot: TelegramClient, target):
    """Turn a user id into something sendable.

    Join requesters arrive as live updates, so their access hash is usually
    already in the session cache; if it is not, bots may address users with a
    zero hash, which is the fallback here.
    """
    if not isinstance(target, int):
        return target
    try:
        return await bot.get_input_entity(target)
    except (ValueError, TypeError):
        return InputPeerUser(target, 0)


async def refresh(bot: TelegramClient, message: Message) -> Message:
    """Re-fetch a message for a fresh file reference (they expire)."""
    try:
        fresh = await bot.get_messages(message.peer_id, ids=message.id)
        if fresh is not None:
            return fresh
    except Exception as exc:  # noqa: BLE001
        _logger.warning("could not refresh the post: %s: %s",
                        type(exc).__name__, exc)
    return message


def has_media(message: Message) -> bool:
    media = getattr(message, "media", None)
    return media is not None and not isinstance(media, MessageMediaWebPage)


async def _send(bot: TelegramClient, peer, message: Message, markup) -> None:
    text = message.message or ""
    entities = message.entities or None

    if not has_media(message):
        await bot.send_message(
            peer,
            text,
            formatting_entities=entities,
            parse_mode=None,
            link_preview=isinstance(getattr(message, "media", None), MessageMediaWebPage),
            buttons=markup,
        )
        return

    try:
        media = utils.get_input_media(message.media)
    except TypeError as exc:
        raise UnsendablePost(
            f"{type(message.media).__name__} cannot be re-sent"
        ) from exc
    # send_file has no spoiler kwarg, so the flag goes on the InputMedia.
    if getattr(message.media, "spoiler", False) and hasattr(media, "spoiler"):
        media.spoiler = True

    if utf16_len(text) <= CAPTION_LIMIT:
        await bot.send_file(
            peer,
            media,
            caption=text,
            formatting_entities=entities,
            parse_mode=None,
            buttons=markup,
        )
        return

    # Caption too long for a bot: media first, then the full text as its own
    # message so every line and link survives.
    await bot.send_file(peer, media, caption="", parse_mode=None)
    await bot.send_message(
        peer,
        text,
        formatting_entities=entities,
        parse_mode=None,
        link_preview=True,
        buttons=markup,
    )


async def copy_to(bot: TelegramClient, target, message: Message,
                  markup=None, on_refresh=None) -> Result:
    """Send a copy of `message` to one target. Never raises.

    Only USER_VERDICTS say something about the recipient; every other status is
    about this attempt, this run or the post, and callers must not persist them
    against the user. `on_refresh` is called with a refetched message when a
    stale file reference forced one, so the caller can reuse it.
    """
    try:
        # Inside the try: resolving can itself hit a flood wait, and that is a
        # flood, not a broken user.
        peer = await resolve_peer(bot, target)
        try:
            await _send(bot, peer, message, markup)
        except FileReferenceExpiredError:
            _logger.info("file reference expired — refreshing the post")
            fresh = await refresh(bot, message)
            if on_refresh is not None:
                on_refresh(fresh)
            await _send(bot, peer, fresh, markup)
        return Result(SENT)
    except FloodWaitError as exc:
        return Result(FLOOD, flood=int(exc.seconds))
    except PeerFloodError:
        return Result(PEERFLOOD)
    except UnsendablePost as exc:
        return Result(BAD_POST, str(exc))
    except UserIsBlockedError:
        return Result(BLOCKED)
    except InputUserDeactivatedError:
        return Result(DELETED)
    except UserIsBotError:
        return Result(IS_BOT)
    except (PeerIdInvalidError, ChatWriteForbiddenError):
        return Result(INVALID)
    except Exception as exc:  # noqa: BLE001 - one bad send must not stop a run
        return Result(ERROR, f"{type(exc).__name__}: {exc}")
