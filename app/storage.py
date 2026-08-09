"""Tiny JSON persistence layer.

Every store keeps its data in memory and writes the whole file back through a
temporary file + os.replace, so a crash mid-write can never leave a truncated
JSON behind. Hot paths (a join raid touching the user store hundreds of times a
minute) call mark_dirty() and let the autoflush task coalesce the writes.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from . import log

_logger = log.get("storage")
_stores: list["JsonStore"] = []


def _fsync_dir(path: Path) -> None:
    """Make a rename durable, not just visible."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


class JsonStore:
    def __init__(self, path: Path, default: Callable[[], Any]) -> None:
        self.path = Path(path)
        self._default = default
        self._data: Any = None
        self._dirty = False
        _stores.append(self)

    # --- access -----------------------------------------------------------
    @property
    def data(self) -> Any:
        if self._data is None:
            self._data = self._read()
        return self._data

    def _read(self) -> Any:
        if not self.path.exists():
            return self._default()
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
        except (ValueError, OSError) as exc:
            _logger.error("%s is unreadable (%s) — starting from empty",
                          self.path.name, type(exc).__name__)
            self._backup()
            return self._default()
        if not isinstance(loaded, type(self._default())):
            _logger.error("%s has an unexpected shape — starting from empty",
                          self.path.name)
            self._backup()
            return self._default()
        return loaded

    def _backup(self) -> None:
        """Never delete data we failed to parse — move it aside instead.

        The name carries a timestamp so a second corruption cannot overwrite
        the evidence of the first.
        """
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            self.path.replace(self.path.with_name(f"{self.path.name}.{stamp}.bad"))
        except OSError:
            pass

    # --- writing ----------------------------------------------------------
    def mark_dirty(self) -> None:
        self._dirty = True

    def flush(self, force: bool = False) -> bool:
        """Write the file out. Returns True when something was written.

        Temp file -> fsync -> rename -> fsync(dir): survives both a crash
        mid-write and the machine losing power right after the rename.
        """
        if not force and not self._dirty:
            return False
        if self._data is None:
            return False

        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            payload = json.dumps(self._data, ensure_ascii=False,
                                 separators=(",", ":"))
            with open(tmp, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, self.path)
            _fsync_dir(self.path.parent)
        except Exception as exc:  # noqa: BLE001 - a failed write must not crash
            _logger.error("could not write %s: %s: %s",
                          self.path.name, type(exc).__name__, exc)
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            return False
        self._dirty = False
        return True

    def save(self) -> None:
        """Mark dirty and write immediately (used by owner commands)."""
        self.mark_dirty()
        self.flush()

    def delete(self) -> bool:
        self._data = self._default()
        self._dirty = False
        if self.path.exists():
            try:
                self.path.unlink()
                return True
            except OSError:
                return False
        return False


def flush_all(force: bool = False) -> int:
    return sum(1 for store in _stores if store.flush(force=force))


async def autoflush(interval: float = 2.0) -> None:
    """Background task: persist dirty stores every `interval` seconds."""
    while True:
        try:
            await asyncio.sleep(interval)
            flush_all()
        except asyncio.CancelledError:
            flush_all()
            raise
        except Exception as exc:  # noqa: BLE001 - never kill the bot over a write
            _logger.error("autoflush failed: %s: %s", type(exc).__name__, exc)
