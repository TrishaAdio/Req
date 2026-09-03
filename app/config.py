"""Configuration. Every knob is an environment variable (see .env.example).

Channels are NOT configured here — the owner adds them at runtime with /add,
so one deployment can serve any number of channels without a restart.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# --- env helpers ----------------------------------------------------------
def _raw(name: str) -> str:
    return (os.getenv(name) or "").strip()


def _int(name: str, default: int = 0) -> int:
    try:
        return int(_raw(name))
    except ValueError:
        return default


def _float(name: str, default: float) -> float:
    try:
        return float(_raw(name))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = _raw(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on")


def _ids(name: str) -> set[int]:
    out: set[int] = set()
    for chunk in _raw(name).replace(",", " ").split():
        try:
            out.add(int(chunk))
        except ValueError:
            continue
    return out


# --- Telegram credentials -------------------------------------------------
API_ID = _int("API_ID")
API_HASH = _raw("API_HASH")
BOT_TOKEN = _raw("BOT_TOKEN")

# The owner is the only account the bot obeys. ADMIN_IDS may add co-owners.
OWNER_ID = _int("OWNER_ID")
ADMIN_IDS = _ids("ADMIN_IDS") | ({OWNER_ID} if OWNER_ID else set())

# --- Behaviour ------------------------------------------------------------
# Serve only the channels added with /add. Turning this off makes the bot serve
# every chat it administers while no channel has been added, which means anyone
# who makes it an admin feeds your audience — so it defaults to on.
STRICT_CHANNELS = _bool("STRICT_CHANNELS", True)
# Off (default): every join request gets the post, including from someone who
# was welcomed before — a person who leaves and requests again is welcomed again.
# On: one post per user for as long as they stay in the store, so requesting a
# second channel (or re-requesting) sends nothing.
WELCOME_ONCE = _bool("WELCOME_ONCE", False)
# Parallel welcome sends during a join raid.
WELCOME_CONCURRENCY = max(1, _int("WELCOME_CONCURRENCY", 5))

# --- Broadcast pacing -----------------------------------------------------
# Effective rate is roughly WORKERS / SEND_DELAY_SECONDS messages per second.
# These are cold DMs to people who never started the bot, which is what
# Telegram polices hardest, so the defaults stay slow (~1/s).
BROADCAST_WORKERS = max(1, _int("BROADCAST_WORKERS", 1))
SEND_DELAY_SECONDS = _float("SEND_DELAY_SECONDS", 1.0)
BATCH_SIZE = _int("BATCH_SIZE", 50)
BATCH_PAUSE_SECONDS = _float("BATCH_PAUSE_SECONDS", 30)
# A flood wait longer than this stops the run instead of being waited out.
MAX_FLOOD_WAIT = _int("MAX_FLOOD_WAIT", 300)
# How often the live progress message is edited.
PROGRESS_EVERY_SECONDS = _float("PROGRESS_EVERY_SECONDS", 6)

# --- Logging --------------------------------------------------------------
LOG_LEVEL = (_raw("LOG_LEVEL") or "INFO").upper()
LOG_FILE = _raw("LOG_FILE")  # empty = console only

# --- Paths ----------------------------------------------------------------
DATA_DIR = Path(_raw("DATA_DIR") or (BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

USERS_FILE = DATA_DIR / "users.json"
CHANNELS_FILE = DATA_DIR / "channels.json"
POST_FILE = DATA_DIR / "post.json"
BUTTONS_FILE = DATA_DIR / "buttons.json"
# Flat id list written by older versions / the reference bot; imported once.
LEGACY_AUDIENCE_FILE = DATA_DIR / "audience.json"

SESSION = str(DATA_DIR / "bot")

REQUIRED = ("API_ID", "API_HASH", "BOT_TOKEN")


def is_admin(user_id: int | None) -> bool:
    return bool(user_id) and user_id in ADMIN_IDS


def missing() -> list[str]:
    return [name for name in REQUIRED if not globals().get(name)]


def require() -> None:
    """Abort with a readable message instead of a stack trace."""
    gaps = missing()
    if gaps:
        raise SystemExit(
            "Missing required config: "
            + ", ".join(gaps)
            + "\nCopy .env.example to .env and fill it in."
        )
