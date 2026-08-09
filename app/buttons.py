"""Inline URL buttons attached to the post and to broadcasts (/setbutton).

Syntax — one line per row, "|" splits buttons inside a row:

    /setbutton
    Join - https://t.me/yourchannel | Chat - https://t.me/yourgroup
    Website - https://example.com

data/buttons.json  ->  [[{"text": ..., "url": ...}, ...], ...]
"""
from __future__ import annotations

from telethon import Button

from . import config
from .storage import JsonStore

_store = JsonStore(config.BUTTONS_FILE, lambda: [])

MAX_ROWS = 8
MAX_PER_ROW = 4
SCHEMES = ("http://", "https://", "tg://")


def rows() -> list:
    return _store.data


def save(new_rows: list) -> None:
    _store.data.clear()
    _store.data.extend(new_rows)
    _store.save()


def clear() -> bool:
    had = bool(_store.data)
    _store.delete()
    return had


def count() -> int:
    return sum(len(row) for row in _store.data)


def row_count() -> int:
    return len(_store.data)


def to_markup():
    """Telethon markup for the stored buttons, or None if there are none."""
    markup = []
    for row in _store.data:
        built = [
            Button.url(button["text"], button["url"])
            for button in row
            if button.get("text") and button.get("url")
        ]
        if built:
            markup.append(built)
    return markup or None


def describe() -> str:
    if not _store.data:
        return "none"
    labels = " | ".join(
        button["text"] for row in _store.data for button in row
    )
    return f"{count()} in {row_count()} row(s): {labels}"


def parse(text: str) -> tuple[list | None, str | None]:
    """Parse a /setbutton body. Returns (rows, error)."""
    parsed: list[list[dict]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        row: list[dict] = []
        for part in line.split("|"):
            part = part.strip()
            if not part:
                continue
            label, separator, url = part.rpartition(" - ")
            label, url = label.strip(), url.strip()
            if not separator or not label or not url:
                return None, f"Bad button: {part}\nUse: Label - https://url"
            if not url.lower().startswith(SCHEMES):
                return None, f"URL must start with http(s):// or tg:// — {url}"
            row.append({"text": label, "url": url})
        if len(row) > MAX_PER_ROW:
            return None, f"Max {MAX_PER_ROW} buttons per row."
        if row:
            parsed.append(row)
    if not parsed:
        return None, "No buttons found. Example: Join - https://t.me/yourchannel"
    if len(parsed) > MAX_ROWS:
        return None, f"Max {MAX_ROWS} rows."
    return parsed, None
