"""Colourised logging (colorama) plus the small helpers used for the banner.

Colour is applied only when the stream is a TTY, so redirected logs and
journald stay clean. Set FORCE_COLOR=1 to override, NO_COLOR=1 to disable.
"""
from __future__ import annotations

import logging
import os
import re
import sys

from colorama import Fore, Style
from colorama import init as _colorama_init

_colorama_init()

_LEVEL_COLOUR = {
    logging.DEBUG: Fore.CYAN,
    logging.INFO: Fore.GREEN,
    logging.WARNING: Fore.YELLOW,
    logging.ERROR: Fore.RED,
    logging.CRITICAL: Fore.RED + Style.BRIGHT,
}

_LEVEL_LABEL = {
    logging.DEBUG: "DBG",
    logging.INFO: "INF",
    logging.WARNING: "WRN",
    logging.ERROR: "ERR",
    logging.CRITICAL: "CRT",
}


def colour_enabled() -> bool:
    if os.getenv("NO_COLOR"):
        return False
    if os.getenv("FORCE_COLOR"):
        return True
    return bool(getattr(sys.stdout, "isatty", lambda: False)())


_COLOUR = colour_enabled()


def _paint(text: str, *codes: str) -> str:
    if not _COLOUR or not codes:
        return text
    return "".join(codes) + text + Style.RESET_ALL


# Small vocabulary used in log messages and the startup banner.
def bold(text: str) -> str:
    return _paint(text, Style.BRIGHT)


def dim(text: str) -> str:
    return _paint(text, Style.DIM)


def ok(text: str) -> str:
    return _paint(text, Fore.GREEN)


def warn(text: str) -> str:
    return _paint(text, Fore.YELLOW)


def err(text: str) -> str:
    return _paint(text, Fore.RED)


def val(text: str) -> str:
    return _paint(str(text), Fore.CYAN)


def name(text: str) -> str:
    return _paint(text, Fore.MAGENTA)


class ColourFormatter(logging.Formatter):
    def __init__(self, colour: bool = True) -> None:
        super().__init__(datefmt="%H:%M:%S")
        self.colour = colour

    def format(self, record: logging.LogRecord) -> str:
        label = _LEVEL_LABEL.get(record.levelno, record.levelname[:3])
        stamp = self.formatTime(record, self.datefmt)
        where = record.name
        message = record.getMessage()

        if self.colour:
            stamp = _paint(stamp, Style.DIM)
            label = _paint(label, _LEVEL_COLOUR.get(record.levelno, ""))
            where = _paint(where, Fore.BLUE)
            if record.levelno >= logging.ERROR:
                message = _paint(message, Fore.RED)
            elif record.levelno == logging.WARNING:
                message = _paint(message, Fore.YELLOW)

        line = f"{stamp} {label} {where:<9} {message}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class PlainFormatter(logging.Formatter):
    """For the log FILE: colour is added at the call site (log.ok(...), and so
    on), so it has to be stripped back out here."""

    def format(self, record: logging.LogRecord) -> str:
        return _ANSI.sub("", super().format(record))


def setup() -> None:
    """Install handlers. Reads level/file from config."""
    from . import config

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(level)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(ColourFormatter(_COLOUR))
    root.addHandler(console)

    if config.LOG_FILE:
        handler = logging.FileHandler(config.LOG_FILE, encoding="utf-8")
        handler.setFormatter(PlainFormatter(
            "%(asctime)s %(levelname)-8s %(name)-12s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        root.addHandler(handler)

    # Telethon is chatty about connection internals; keep it to real problems.
    logging.getLogger("telethon").setLevel(logging.WARNING)


def get(module: str) -> logging.Logger:
    return logging.getLogger(module)


def banner(title: str, rows: list[tuple[str, str]]) -> None:
    """Print an aligned key/value block at startup."""
    width = max((len(k) for k, _ in rows), default=0)
    print(_paint(f"\n  {title}", Style.BRIGHT))
    print(dim("  " + "-" * (width + 30)))
    for key, value in rows:
        print(f"  {dim(key.ljust(width))}  {value}")
    print()
