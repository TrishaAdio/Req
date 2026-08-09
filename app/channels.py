"""Served channels — the chats whose join requests the bot answers.

Managed live from Telegram, so adding a channel never needs a restart:

    /add -1001234567890        /add @publicchannel        /remove -100...

data/channels.json  ->  {"v": 1, "chats": {"<chat_id>": {"title": ..., "t": ts}}}

Every added channel is served in addition to the ones already there. While the
list is empty the bot answers join requests from every chat it administers
(set STRICT_CHANNELS=true to require an explicit /add instead).
"""
from __future__ import annotations

import time

from . import config
from .storage import JsonStore

_store = JsonStore(config.CHANNELS_FILE, lambda: {"v": 1, "chats": {}})


def _chats() -> dict:
    return _store.data.setdefault("chats", {})


def parse_ident(text: str):
    """Owner input -> chat id (int) or public ident (str). None if unusable.

    Accepts -1001234567890, a bare 1234567890 channel id (normalised to the
    -100 form), @username, or a t.me/username link.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("@"):
        return raw if len(raw) > 1 else None
    if "t.me/" in raw:
        handle = raw.rstrip("/").rsplit("/", 1)[-1]
        if not handle or handle.startswith("+") or handle == "joinchat":
            return None  # private invite links cannot be resolved by a bot
        return "@" + handle.lstrip("@")

    body = raw[1:] if raw.startswith("-") else raw
    if not body.isdigit() or int(body) == 0:
        return None
    if raw.startswith("-"):
        return int(raw)
    # A bare id copied out of a client. Some clients already show the 100
    # prefix, so only add one when it isn't there — a second prefix would
    # register an id that can never match, silently shadowing real channels.
    return int(body if body.startswith("100") else "100" + body) * -1


def add(chat_id: int, title: str = "") -> bool:
    """Serve a chat. True if it was newly added."""
    chats = _chats()
    key = str(int(chat_id))
    if key in chats:
        if title and chats[key].get("title") != title:
            chats[key]["title"] = title
            _store.save()
        return False
    chats[key] = {"title": title or "", "t": int(time.time())}
    _store.save()
    return True


def remove(chat_id: int) -> bool:
    if _chats().pop(str(int(chat_id)), None) is None:
        return False
    _store.save()
    return True


def set_title(chat_id: int, title: str) -> None:
    row = _chats().get(str(int(chat_id)))
    if row is not None and title and row.get("title") != title:
        row["title"] = title
        _store.save()


def title_of(chat_id: int) -> str:
    return _chats().get(str(int(chat_id)), {}).get("title", "")


def items() -> list[tuple[int, str]]:
    return sorted(
        (int(cid), row.get("title", "")) for cid, row in _chats().items()
    )


def ids() -> list[int]:
    return [int(cid) for cid in _chats()]


def count() -> int:
    return len(_chats())


def accepts(chat_id: int) -> bool:
    """True when join requests from this chat should be answered."""
    chats = _chats()
    if not chats:
        return not config.STRICT_CHANNELS
    return str(int(chat_id)) in chats
