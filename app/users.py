"""The audience: everyone who ever requested to join a served channel.

data/users.json  ->  {"v": 1, "users": {"<user_id>": {...}}}

Per-user record (short keys keep a 100k-user file small):
    n  display name          u  username           c  chat id first seen in
    t  first seen (unix)     w  last welcomed (unix)   s  status
    r  stamps of the last few messages sent, for the daily cap
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
# The daily cap counts over a rolling window rather than a calendar day, so it
# cannot be doubled by sending on both sides of midnight.
WINDOW = 86400


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


def mark_welcome(user_id: int) -> None:
    """Stamp a welcome that was actually delivered.

    Called after every successful send, not just the first, so `w` is the LAST
    time the post reached this user rather than the only time it ever did.
    """
    row = _users().get(str(int(user_id)))
    if row is None:
        return
    row["w"] = int(time.time())
    _store.mark_dirty()


def was_welcomed(user_id: int) -> bool:
    return bool(_users().get(str(int(user_id)), {}).get("w"))


# --- the daily cap --------------------------------------------------------
# Every message the bot delivers to a user is counted, welcomes and broadcasts
# alike, and once the allowance is gone that user is left alone until a stamp
# ages out of the window. Admins are never counted or capped: /preview and a
# test broadcast to yourself have to arrive however often you ask for them.
def _recent(row: dict, now: int) -> list[int]:
    """Send stamps still inside the window, oldest first."""
    stamps = row.get("r") or []
    if not isinstance(stamps, list):
        return []
    return [int(t) for t in stamps
            if isinstance(t, (int, float)) and int(t) > 0
            and now - int(t) < WINDOW]


def _capped(row: dict, now: int) -> bool:
    return len(_recent(row, now)) >= config.DAILY_USER_LIMIT


def sends_today(user_id: int) -> int:
    """How many messages this user has had inside the window."""
    row = _users().get(str(int(user_id)))
    return 0 if row is None else len(_recent(row, int(time.time())))


def reserve_send(user_id: int) -> bool:
    """Take one message off the allowance BEFORE sending it.

    Checking and stamping have to happen together, with no await in between:
    a raid can put several sends to the same user in flight at once, and a
    plain "is there room" check would let every one of them through. Give the
    reservation back with release_send() when the send then fails.
    """
    if config.DAILY_USER_LIMIT <= 0 or config.is_admin(user_id):
        return True
    row = _users().get(str(int(user_id)))
    if row is None:
        return True
    now = int(time.time())
    stamps = _recent(row, now)
    if len(stamps) >= config.DAILY_USER_LIMIT:
        # A raid can ask this a lot; only a real change is worth a write.
        if len(stamps) != len(row.get("r") or []):
            row["r"] = stamps  # the pruning is worth keeping
            _store.mark_dirty()
        return False
    stamps.append(now)
    # Only the newest DAILY_USER_LIMIT stamps can ever change the answer above,
    # so older ones are dropped instead of growing the file forever.
    row["r"] = stamps[-config.DAILY_USER_LIMIT:]
    _store.mark_dirty()
    return True


def release_send(user_id: int) -> None:
    """Hand back a reservation whose message never arrived.

    Stamps are interchangeable, so dropping the newest is enough to give the
    allowance back — it does not have to be the very one this caller took.
    """
    if config.DAILY_USER_LIMIT <= 0 or config.is_admin(user_id):
        return
    row = _users().get(str(int(user_id)))
    if row is None:
        return
    stamps = _recent(row, int(time.time()))
    if stamps:
        row["r"] = stamps[:-1]
        _store.mark_dirty()


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
        "capped": 0,
        "blocked": 0,
        "deleted": 0,
        "invalid": 0,
        "is_bot": 0,
        "new_today": 0,
    }
    now = int(time.time())
    cutoff = now - WINDOW
    capping = config.DAILY_USER_LIMIT > 0
    for row in users.values():
        status = row.get("s", "ok")
        if status in out:
            out[status] += 1
        if status not in SKIP:
            out["reachable"] += 1
        if row.get("w"):
            out["welcomed"] += 1
        if capping and _capped(row, now):
            out["capped"] += 1
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
