"""The audience: everyone who ever requested to join a served channel.

data/users.json  ->  {"v": 1, "users": {"<user_id>": {...}}}

Per-user record (short keys keep a 100k-user file small):
    n  display name          u  username           c  chat id first seen in
    t  first seen (unix)     w  welcomed (unix)    s  status
Status is "ok" until Telegram says otherwise. Anything in SKIP means later
broadcasts leave that user alone.
"""
from __future__ import annotations

import json
import time

from . import config, log
from .storage import JsonStore

_logger = log.get("users")
_store = JsonStore(config.USERS_FILE, lambda: {"v": 1, "users": {}})

# Statuses that make a user no longer worth sending to. One definition, used by
# both reachable_ids() and counts(), so /stats cannot disagree with a run.
SKIP = ("blocked", "deleted", "invalid", "is_bot")
# A user who shows up with a fresh join request is worth trying again — except
# a deleted account, which never comes back.
REVIVABLE = ("blocked", "invalid", "is_bot")


def _users() -> dict:
    return _store.data.setdefault("users", {})


def add(user_id: int, name: str = "", username: str = "", chat_id: int = 0) -> bool:
    """Record a user (or refresh what we know). True if newly added."""
    users = _users()
    key = str(int(user_id))
    row = users.get(key)
    now = int(time.time())
    if row is None:
        users[key] = {
            "n": name or "",
            "u": username or "",
            "c": int(chat_id),
            "t": now,
            "w": 0,
            "s": "ok",
        }
        _store.mark_dirty()
        return True

    changed = False
    if name and row.get("n") != name:
        row["n"] = name
        changed = True
    if username and row.get("u") != username:
        row["u"] = username
        changed = True
    if chat_id and not row.get("c"):
        row["c"] = int(chat_id)
        changed = True
    if row.get("s") in REVIVABLE:
        row["s"] = "ok"
        changed = True
    if changed:
        _store.mark_dirty()
    return False


def claim_welcome(user_id: int) -> bool:
    """Reserve the right to welcome this user, before anything is sent.

    Two join requests from the same person (two channels, same second) are
    dispatched as two concurrent tasks; claiming the flag up front — with no
    await in between — is what stops both of them sending. Release it with
    release_welcome() if the send then fails.
    """
    row = _users().get(str(int(user_id)))
    if row is None or row.get("w"):
        return False
    row["w"] = int(time.time())
    _store.mark_dirty()
    return True


def release_welcome(user_id: int) -> None:
    row = _users().get(str(int(user_id)))
    if row is not None and row.get("w"):
        row["w"] = 0
        _store.mark_dirty()


def was_welcomed(user_id: int) -> bool:
    return bool(_users().get(str(int(user_id)), {}).get("w"))


def set_status(user_id: int, status: str) -> None:
    row = _users().get(str(int(user_id)))
    if row is not None and row.get("s") != status:
        row["s"] = status
        _store.mark_dirty()


def total() -> int:
    return len(_users())


def reachable_ids() -> list[int]:
    """Broadcast targets: everyone we have no reason to skip."""
    return [int(uid) for uid, row in _users().items() if row.get("s") not in SKIP]


def counts() -> dict[str, int]:
    users = _users()
    out = {
        "total": len(users),
        "reachable": 0,
        "welcomed": 0,
        "blocked": 0,
        "deleted": 0,
        "invalid": 0,
        "is_bot": 0,
        "new_today": 0,
    }
    cutoff = int(time.time()) - 86400
    for row in users.values():
        status = row.get("s", "ok")
        if status in out:
            out[status] += 1
        if status not in SKIP:
            out["reachable"] += 1
        if row.get("w"):
            out["welcomed"] += 1
        if row.get("t", 0) >= cutoff:
            out["new_today"] += 1
    return out


def per_chat() -> dict[int, int]:
    """Users grouped by the channel they were FIRST seen in."""
    out: dict[int, int] = {}
    for row in _users().values():
        chat_id = int(row.get("c") or 0)
        out[chat_id] = out.get(chat_id, 0) + 1
    return out


def prune_dead() -> int:
    """Drop users no run will target again. Returns how many went."""
    users = _users()
    gone = [uid for uid, row in users.items() if row.get("s") in SKIP]
    for uid in gone:
        users.pop(uid, None)
    if gone:
        _store.save()
    return len(gone)


def export_json() -> str:
    return json.dumps(_store.data, ensure_ascii=False, indent=1)


def import_legacy() -> int:
    """One-off import of a flat id list (data/audience.json) if one exists."""
    path = config.LEGACY_AUDIENCE_FILE
    if not path.exists():
        return 0
    try:
        ids = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError) as exc:
        _logger.warning("could not read %s: %s", path.name, type(exc).__name__)
        return 0
    if not isinstance(ids, list):
        return 0

    added = sum(1 for uid in ids if isinstance(uid, int) and add(uid))
    _store.save()
    try:
        path.replace(path.with_suffix(".json.imported"))
    except OSError:
        pass
    if added:
        _logger.info("imported %d user(s) from %s", added, path.name)
    return added
