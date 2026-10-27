"""The owner side-channel: status pings and the startup audience backup.

Everything here is addressed to config.OWNER_ID and fails quietly. A bot that
cannot reach its owner — the owner never pressed Start, or blocked the bot —
must still go on serving join requests, so a failed DM is logged and swallowed,
never raised.
"""
from __future__ import annotations

import io
import time

from telethon.errors import RPCError
from telethon.tl.functions.channels import EditAdminRequest, GetParticipantRequest
from telethon.tl.functions.messages import EditChatAdminRequest
from telethon.tl.types import (
    ChannelParticipantCreator,
    ChatAdminRights,
    InputPeerSelf,
)

from . import config, log, users

_logger = log.get("owner")

# Every grantable right on a ChatAdminRights. "anonymous" is deliberately NOT
# in here: it hides the owner's posts behind the channel name, which is not
# part of "full rights" and would surprise them.
RIGHT_FIELDS = (
    "change_info", "post_messages", "edit_messages", "delete_messages",
    "ban_users", "invite_users", "pin_messages", "add_admins", "manage_call",
    "other", "manage_topics", "post_stories", "edit_stories", "delete_stories",
    "manage_direct_messages", "manage_ranks",
)

# RPC errors worth explaining instead of printing raw.
_REASONS = {
    "USER_NOT_PARTICIPANT": "the owner is not in that chat — join it first",
    "USER_ID_INVALID": "the bot has never seen the owner — DM the bot once",
    "USER_NOT_MUTUAL_CONTACT": "the owner is not in that chat — join it first",
    "CHAT_ADMIN_REQUIRED": "the bot is not an admin there",
    "RIGHT_FORBIDDEN": "the bot lacks the \u201cAdd new admins\u201d right",
    "ADMIN_RIGHTS_EMPTY": "the bot has no rights it can pass on",
    "USER_CREATOR": "the owner already owns that chat",
    "USER_PRIVACY_RESTRICTED": "the owner's privacy settings block this",
    "BOT_GROUPS_BLOCKED": "the bot cannot act in that chat",
}


def _reason(exc: BaseException) -> str:
    text = str(getattr(exc, "message", "") or exc).upper()
    for key, explanation in _REASONS.items():
        if key in text:
            return explanation
    return type(exc).__name__


def _rights_like(mask) -> ChatAdminRights:
    """Full rights, clipped to what `mask` (the bot's own rights) allows.

    Telegram refuses to let a bot grant a right it does not hold itself, so
    asking for everything unconditionally would fail the whole call instead of
    handing over the rights that *are* available.
    """
    granted = {
        field: (True if mask is None else bool(getattr(mask, field, False)))
        for field in RIGHT_FIELDS
    }
    granted["anonymous"] = False
    return ChatAdminRights(**granted)


def summarise(rights: ChatAdminRights) -> str:
    held = [f for f in RIGHT_FIELDS if getattr(rights, f, False)]
    return f"{len(held)} right(s)" if held else "no rights"


async def promote(bot, chat_id: int) -> tuple[bool, str]:
    """Give OWNER_ID every admin right the bot holds in `chat_id`.

    Returns (ok, detail). Never raises: a chat the bot cannot promote in is
    reported, not fatal.
    """
    if not config.PROMOTE_OWNER:
        return False, "disabled"
    if not config.OWNER_ID:
        return False, "no OWNER_ID set"

    try:
        chat = await bot.get_entity(chat_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"chat not resolvable ({type(exc).__name__})"

    # A basic group has no granular rights — one flag is all there is.
    if not hasattr(chat, "megagroup") and not hasattr(chat, "broadcast"):
        try:
            await bot(EditChatAdminRequest(
                chat_id=chat.id, user_id=config.OWNER_ID, is_admin=True))
        except RPCError as exc:
            return False, _reason(exc)
        except Exception as exc:  # noqa: BLE001
            return False, type(exc).__name__
        return True, "group admin"

    # Channel / supergroup: mirror whatever the bot itself was given.
    mask = None
    try:
        mine = await bot(GetParticipantRequest(
            channel=chat, participant=InputPeerSelf()))
        mask = getattr(mine.participant, "admin_rights", None)
        if mask is None:
            return False, "the bot is not an admin there"
    except RPCError as exc:
        return False, _reason(exc)
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__

    # The creator already outranks every admin; editing them is an error.
    try:
        theirs = await bot(GetParticipantRequest(
            channel=chat, participant=config.OWNER_ID))
        if isinstance(theirs.participant, ChannelParticipantCreator):
            return True, "already the creator"
    except RPCError as exc:
        if "USER_NOT_PARTICIPANT" in str(getattr(exc, "message", "") or exc).upper():
            return False, _REASONS["USER_NOT_PARTICIPANT"]
    except Exception:  # noqa: BLE001 - not knowing is fine, just try the edit
        pass

    rights = _rights_like(mask)
    try:
        await bot(EditAdminRequest(channel=chat, user_id=config.OWNER_ID,
                                   admin_rights=rights, rank=config.OWNER_RANK))
    except RPCError as exc:
        return False, _reason(exc)
    except Exception as exc:  # noqa: BLE001
        return False, type(exc).__name__
    return True, summarise(rights)


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
