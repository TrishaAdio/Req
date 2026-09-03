"""The broadcast engine.

A pool of workers pulls user ids off a queue and copies the post to each one.
Pacing is deliberate: a per-send delay, a pause every BATCH_SIZE sends, and a
shared back-off gate so that when Telegram answers with a flood wait, *every*
worker waits it out instead of piling on. A hard PeerFlood stops the run.

Users that turn out to be unreachable are marked in the user store, so the next
broadcast skips them instead of burning rate limit on them again.
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from telethon import TelegramClient
from telethon.tl.types import Message

from . import buttons, config, copier, log, post, users
from .copier import Result

_logger = log.get("bcast")


@dataclass
class Stats:
    total: int = 0
    sent: int = 0
    blocked: int = 0
    deleted: int = 0
    invalid: int = 0
    is_bot: int = 0
    error: int = 0
    skipped: int = 0
    capped: int = 0
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    stopped: str = ""
    note: str = ""

    @property
    def done(self) -> int:
        return (self.sent + self.blocked + self.deleted + self.invalid
                + self.is_bot + self.error + self.skipped + self.capped)

    @property
    def failed(self) -> int:
        return self.done - self.sent

    @property
    def elapsed(self) -> float:
        return (self.finished or time.monotonic()) - self.started

    @property
    def rate(self) -> float:
        return self.done / self.elapsed if self.elapsed > 0 else 0.0

    @property
    def eta(self) -> float | None:
        remaining = self.total - self.done
        if remaining <= 0 or self.rate <= 0:
            return None
        return remaining / self.rate

    def bump(self, status: str) -> None:
        if hasattr(self, status):
            setattr(self, status, getattr(self, status) + 1)
        else:
            self.error += 1


def _clock(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60:02d}s"
    return f"{seconds // 3600}h {(seconds % 3600) // 60:02d}m"


def render(stats: Stats, running: bool = False) -> str:
    """Progress / result block. HTML, no filler."""
    head = "Broadcasting" if running else "Broadcast done"
    lines = [
        f"<b>{head}</b>",
        "",
        f"Sent <b>{stats.sent}</b> / {stats.total}",
    ]
    breakdown = [
        f"{label} {value}"
        for label, value in (
            ("blocked", stats.blocked),
            ("deleted", stats.deleted),
            ("invalid", stats.invalid),
            ("bots", stats.is_bot),
            ("errors", stats.error),
            ("skipped", stats.skipped),
            ("at daily cap", stats.capped),
        )
        if value
    ]
    if breakdown:
        lines.append(" · ".join(breakdown))
    lines.append(f"{_clock(stats.elapsed)} elapsed · {stats.rate:.1f}/s")
    if running and stats.eta is not None:
        lines.append(f"ETA {_clock(stats.eta)}")
    reason = {
        "cancelled": "Cancelled.",
        "peerflood": "Stopped — Telegram rate limit hit.",
        "flood": f"Stopped — flood wait over {config.MAX_FLOOD_WAIT}s.",
        "bad_post": "Stopped — this post cannot be re-sent.",
        "crashed": "Stopped — internal error, see the log.",
    }.get(stats.stopped)
    if reason:
        lines.append(reason)
    if stats.note:
        lines.append(stats.note)
    return "\n".join(lines)


class Broadcaster:
    """One at a time; /cancel stops the current run."""

    def __init__(self, bot: TelegramClient) -> None:
        self.bot = bot
        self.running = False
        self.stats: Stats | None = None
        self._message: Message | None = None
        self._markup = None
        self._cancel = False
        self._gate_until = 0.0
        self._done_lock = asyncio.Lock()
        self._paced = 0

    # --- control ----------------------------------------------------------
    def reserve(self) -> bool:
        """Claim the engine synchronously.

        run() is normally started as a task, so a caller that only checked
        `running` could let a second /bcast through in the meantime. Callers
        reserve first, then start the run (and release() if they bail out).
        """
        if self.running:
            return False
        self.running = True
        return True

    def release(self) -> None:
        """Give back a reservation that never turned into a run."""
        self.running = False

    def cancel(self) -> bool:
        if not self.running:
            return False
        self._cancel = True
        return True

    @property
    def cancelled(self) -> bool:
        return self._cancel

    # The gate is shared with the welcome path: when either side is told to
    # wait, both back off, instead of taking turns discovering the same limit.
    def hold(self, seconds: float) -> None:
        """Make every sender back off for at least `seconds`."""
        self._gate_until = max(self._gate_until, time.monotonic() + seconds)

    async def gate(self) -> None:
        while True:
            wait = self._gate_until - time.monotonic()
            if wait <= 0 or self._cancel:
                return
            await asyncio.sleep(min(wait, 1.0))

    # --- run --------------------------------------------------------------
    async def run(self, message: Message, targets: list[int],
                  on_progress=None) -> Stats:
        targets = list(dict.fromkeys(int(t) for t in targets))  # dedupe, keep order
        stats = Stats(total=len(targets))
        self.stats = stats
        self._message = message
        # Built once: doing it per send would put an unguarded call outside
        # copy_to's protection, where an exception kills the worker.
        self._markup = self._build_markup()
        self._cancel = False
        self._gate_until = 0.0
        self._paced = 0
        self.running = True

        queue: asyncio.Queue[int] = asyncio.Queue()
        for user_id in targets:
            queue.put_nowait(user_id)

        reporter = asyncio.create_task(self._report(stats, on_progress))
        workers = [
            asyncio.create_task(self._worker(queue, stats))
            for _ in range(min(config.BROADCAST_WORKERS, max(1, len(targets))))
        ]
        try:
            results = await asyncio.gather(*workers, return_exceptions=True)
            for outcome in results:
                if isinstance(outcome, BaseException) and not isinstance(
                        outcome, asyncio.CancelledError):
                    # One worker dying must not be reported as a clean finish.
                    stats.stopped = stats.stopped or "crashed"
                    _logger.error("broadcast worker crashed: %s: %s",
                                  type(outcome).__name__, outcome)
        finally:
            reporter.cancel()
            for worker in workers:
                worker.cancel()
            stats.finished = time.monotonic()
            self.running = False

        if self._cancel and not stats.stopped:
            stats.stopped = "cancelled"
        _logger.info(
            "broadcast finished: sent %s, failed %s, %s%s",
            log.ok(str(stats.sent)), log.warn(str(stats.failed)),
            _clock(stats.elapsed), f" ({stats.stopped})" if stats.stopped else "",
        )
        return stats

    async def _report(self, stats: Stats, on_progress) -> None:
        if on_progress is None:
            return
        while True:
            await asyncio.sleep(config.PROGRESS_EVERY_SECONDS)
            try:
                await on_progress(stats)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _logger.debug("progress update failed: %s", type(exc).__name__)

    async def _worker(self, queue: asyncio.Queue, stats: Stats) -> None:
        while not self._cancel and not stats.stopped:
            try:
                user_id = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

            # Reserved before the send, and given back in _record() if it
            # fails, so a broadcast cannot push anyone over the daily cap that
            # welcomes are already counting against.
            if not users.reserve_send(user_id):
                stats.capped += 1
                continue

            await self.gate()
            if self._cancel or stats.stopped:
                users.release_send(user_id)
                return

            result = await self._send(user_id, stats)
            self._record(user_id, result, stats)
            await self._pace()

    def _build_markup(self):
        try:
            return buttons.to_markup()
        except Exception as exc:  # noqa: BLE001 - bad buttons file
            _logger.error("ignoring unusable buttons: %s: %s",
                          type(exc).__name__, exc)
            return None

    async def _send(self, user_id: int, stats: Stats) -> Result:
        result = await copier.copy_to(self.bot, user_id, self._message,
                                      self._markup, on_refresh=self._adopt)
        if result.status == copier.FLOOD and result.flood <= config.MAX_FLOOD_WAIT:
            # Back everyone off, then give this user one more try.
            _logger.warning("flood wait %ss — holding every sender", result.flood)
            self.hold(result.flood + 1)
            await self.gate()
            if self._cancel:
                return result
            result = await copier.copy_to(self.bot, user_id, self._message,
                                          self._markup, on_refresh=self._adopt)
        return result

    def _adopt(self, message: Message) -> None:
        """Reuse a message the copier had to refetch."""
        self._message = message
        post.remember(message)

    def _record(self, user_id: int, result: Result, stats: Stats) -> None:
        if result.status != copier.SENT:
            # The allowance taken before the send was never spent.
            users.release_send(user_id)
        if result.status == copier.PEERFLOOD:
            stats.stopped = "peerflood"
            stats.skipped += 1  # this target was never delivered to
            _logger.error("PeerFlood — stopping the broadcast, retry later")
            return
        if result.status == copier.BAD_POST:
            stats.stopped = "bad_post"
            stats.note = result.detail
            stats.skipped += 1
            _logger.error("post cannot be re-sent (%s) — stopping", result.detail)
            return
        if result.status == copier.FLOOD:
            stats.skipped += 1
            if result.flood > config.MAX_FLOOD_WAIT:
                # Waiting it out would take longer than the owner asked for, and
                # carrying on would just fail for everyone left.
                stats.stopped = "flood"
                _logger.error("flood wait %ss exceeds MAX_FLOOD_WAIT — stopping",
                              result.flood)
            return

        stats.bump(result.status)
        if result.status in copier.USER_VERDICTS:
            users.set_status(user_id, result.status)
        elif result.status == copier.ERROR:
            _logger.warning("send to %s failed: %s", user_id, result.detail)

    async def _pace(self) -> None:
        """Per-send delay, plus a pause every BATCH_SIZE sends.

        The batch counter is its own value, incremented under the lock: reading
        stats.done here would race the workers that increment it and make the
        pause fire twice, or not at all.
        """
        async with self._done_lock:
            self._paced += 1
            if config.BATCH_SIZE and self._paced % config.BATCH_SIZE == 0:
                self.hold(config.BATCH_PAUSE_SECONDS)
                # Long runs outlive a file reference; refresh it each batch.
                self._adopt(await copier.refresh(self.bot, self._message))
                _logger.info("batch pause %ss after %s sends",
                             config.BATCH_PAUSE_SECONDS, log.val(self._paced))
        if config.SEND_DELAY_SECONDS > 0:
            await asyncio.sleep(config.SEND_DELAY_SECONDS)
