"""In-process Telegram Bot API telemetry, exported to a small JSON status file.

Why a file: the CLI runs as a separate (root) process and cannot see counters in
the bot's memory. The service writes ``<data dir>/run/telegram-status.json``
atomically (temp file + ``os.replace``, mode 0600) every few seconds; the CLI reads it.

What is recorded per bot: request/failure counts, per-error-class counts,
rate-limit (HTTP 429 / FloodWait) hits and the last ``retry_after``, a bounded
latency window (p50/p95/max), the time of the last success and of the last
error (error *class name* only).

What is never recorded: tokens, URLs, request payloads, chat IDs, message text.
Overhead: one ``perf_counter`` pair and a few dict updates per API call; memory
is bounded (``WINDOW`` samples per bot, bots capped at ``MAX_BOTS``).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("ziko.telegram_stats")

WINDOW = 256          # latency samples kept per bot
MAX_BOTS = 1000       # hard cap so a bug can never grow memory without bound
STATUS_FILE = "telegram-status.json"
SCHEMA = 1

# aiogram exception class name -> stable category shown to operators.
_CATEGORIES = {
    "TelegramRetryAfter": "rate_limited",
    "TelegramUnauthorizedError": "unauthorized",
    "TelegramForbiddenError": "forbidden",
    "TelegramBadRequest": "bad_request",
    "TelegramConflictError": "conflict",
    "TelegramNotFound": "not_found",
    "TelegramEntityTooLarge": "too_large",
    "TelegramServerError": "server_error",
    "RestartingTelegram": "server_error",
    "TelegramNetworkError": "network",
    "TimeoutError": "timeout",
    "ClientConnectorError": "network",
}


def categorize(exc: BaseException) -> str:
    # Walk the class hierarchy so subclasses (e.g. RestartingTelegram) map correctly.
    for name, category in _CATEGORIES.items():
        if any(c.__name__ == name for c in type(exc).mro()):
            return category
    return "other"


@dataclass(slots=True)
class BotStats:
    bot_id: int
    role: str
    requests: int = 0
    failures: int = 0
    errors: dict[str, int] = field(default_factory=dict)
    rate_limited: int = 0
    last_retry_after: float | None = None
    last_success_at: float | None = None
    last_error_at: float | None = None
    last_error: str | None = None
    latencies_ms: deque[float] = field(default_factory=lambda: deque(maxlen=WINDOW))
    methods: dict[str, int] = field(default_factory=dict)

    def snapshot(self) -> dict[str, Any]:
        lat = sorted(self.latencies_ms)

        def pct(p: float) -> float | None:
            if not lat:
                return None
            return round(lat[min(len(lat) - 1, int(p * (len(lat) - 1) + 0.5))], 1)

        return {
            "bot_id": self.bot_id, "role": self.role, "requests": self.requests,
            "failures": self.failures, "errors": dict(self.errors),
            "rate_limited": self.rate_limited, "last_retry_after": self.last_retry_after,
            "last_success_at": self.last_success_at, "last_error_at": self.last_error_at,
            "last_error": self.last_error,
            "latency_ms": {"p50": pct(0.50), "p95": pct(0.95),
                           "max": round(lat[-1], 1) if lat else None, "samples": len(lat)},
            # getUpdates dominates counts in polling mode; top methods only.
            "methods": dict(sorted(self.methods.items(), key=lambda kv: -kv[1])[:8]),
        }


class TelegramStats:
    def __init__(self) -> None:
        self._bots: dict[int, BotStats] = {}
        self.started_at = time.time()

    def bot(self, bot_id: int, role: str = "child") -> BotStats:
        s = self._bots.get(bot_id)
        if s is None:
            if len(self._bots) >= MAX_BOTS:
                # Drop the bot that has been quiet longest; never grow unbounded.
                oldest = min(self._bots.values(), key=lambda b: b.last_success_at or 0)
                self._bots.pop(oldest.bot_id, None)
            s = self._bots[bot_id] = BotStats(bot_id, role)
        return s

    def forget(self, bot_id: int) -> None:
        self._bots.pop(bot_id, None)

    def record(self, bot_id: int, role: str, method: str, elapsed_ms: float,
               exc: BaseException | None = None) -> None:
        s = self.bot(bot_id, role)
        s.requests += 1
        s.methods[method] = s.methods.get(method, 0) + 1
        now = time.time()
        # Long-poll getUpdates waits on purpose; its duration is not API latency.
        if method != "GetUpdates":
            s.latencies_ms.append(elapsed_ms)
        if exc is None:
            s.last_success_at = now
            return
        cat = categorize(exc)
        s.failures += 1
        s.errors[cat] = s.errors.get(cat, 0) + 1
        s.last_error_at, s.last_error = now, cat
        if cat == "rate_limited":
            s.rate_limited += 1
            retry = getattr(exc, "retry_after", None)
            s.last_retry_after = float(retry) if isinstance(retry, (int, float)) else None

    def snapshot(self) -> dict[str, Any]:
        return {"schema": SCHEMA, "pid": os.getpid(), "written_at": time.time(),
                "process_started_at": self.started_at,
                "bots": [b.snapshot() for b in self._bots.values()]}


STATS = TelegramStats()


class TelemetryRequestMiddleware:
    """aiogram session middleware: ``bot.session.middleware(TelemetryRequestMiddleware(...))``.

    Implements aiogram's request-middleware calling convention
    ``(make_request, bot, method)``; it never alters the request or the result
    and always re-raises, so retry/FloodWait behaviour is unchanged.
    """

    def __init__(self, bot_id: int, role: str, stats: TelegramStats = STATS) -> None:
        self.bot_id, self.role, self.stats = bot_id, role, stats

    async def __call__(self, make_request: Any, bot: Any, method: Any) -> Any:
        name = type(method).__name__
        t0 = time.perf_counter()
        try:
            result = await make_request(bot, method)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self.stats.record(self.bot_id, self.role, name, (time.perf_counter() - t0) * 1000, exc)
            raise
        self.stats.record(self.bot_id, self.role, name, (time.perf_counter() - t0) * 1000)
        return result


def write_status(path: Path, stats: TelegramStats = STATS) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix=".tg-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(stats.snapshot(), fh, separators=(",", ":"))
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def status_writer(path: Path, interval: float = 10.0, stats: TelegramStats = STATS) -> None:
    """Background task. The file write is tiny, so it runs off-loop in a thread."""
    while True:
        try:
            await asyncio.to_thread(write_status, path, stats)
        except OSError as exc:
            logger.warning("Could not write Telegram status file error=%s", type(exc).__name__)
        await asyncio.sleep(interval)


def read_status(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("schema") == SCHEMA else None
