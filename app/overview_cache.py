"""Shared, background-refreshed cache of the platform overview.

Why: building the overview reads every tenant database. ``/platform``, its buttons and
the alert loop all need it; without a cache each tap re-reads N databases.

Behaviour:
* ``get()`` returns the cached snapshot if it is younger than ``max_age``; otherwise it
  refreshes. Concurrent callers share ONE refresh (single flight): ten admins tapping
  at once cause one scan, not ten.
* ``get(fresh=True)`` (the 🔄 button, and after enable/disable) forces a new scan but
  still shares an in-flight one.
* A background task keeps the snapshot warm every ``interval`` seconds, so taps are
  normally instant.
* A failed refresh never replaces a good snapshot: the old one is served (its age is
  visible in the message) and the error is logged. With no snapshot at all, the error
  propagates so the caller can report it.
* ``invalidate()`` marks the snapshot stale after a state change, without blocking.

Correctness note: the cache only ever serves data that was read with normal SQLite
locking; it trades freshness (bounded by ``max_age``) for speed, never consistency.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

logger = logging.getLogger("ziko.overview_cache")


class OverviewCache:
    def __init__(self, build: Callable[[], Awaitable[Any]], *, max_age: float = 30.0,
                 interval: float = 30.0, clock: Callable[[], float] = time.monotonic,
                 on_refresh: Callable[[Any], Awaitable[Any]] | None = None) -> None:
        if max_age <= 0 or interval <= 0:
            raise ValueError("max_age and interval must be positive")
        self._build = build
        self.max_age = max_age
        self.interval = interval
        self._clock = clock
        self._on_refresh = on_refresh
        self._value: Any = None
        self._built_at: float | None = None
        self._inflight: asyncio.Task[Any] | None = None
        self.refreshes = 0
        self.failures = 0
        self.last_build_seconds: float | None = None

    @property
    def age(self) -> float | None:
        return None if self._built_at is None else self._clock() - self._built_at

    def invalidate(self) -> None:
        """Make the next ``get()`` rescan. The old snapshot stays available as a fallback."""
        if self._built_at is not None:
            self._built_at -= self.max_age + 1

    async def _refresh(self) -> Any:
        started = self._clock()
        try:
            value = await self._build()
        except asyncio.CancelledError:
            raise
        except Exception:
            self.failures += 1
            raise
        self._value, self._built_at = value, self._clock()
        self.refreshes += 1
        self.last_build_seconds = self._built_at - started
        if self._on_refresh is not None:
            try:  # exporting the snapshot must never break the panel or alerts
                await self._on_refresh(value)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Overview snapshot export failed error=%s", type(exc).__name__)
        return value

    async def _shared_refresh(self) -> Any:
        if self._inflight is None or self._inflight.done():
            self._inflight = asyncio.ensure_future(self._refresh())
        # shield: one cancelled caller must not cancel the scan others are waiting on
        return await asyncio.shield(self._inflight)

    async def get(self, *, fresh: bool = False) -> Any:
        age = self.age
        if not fresh and age is not None and age < self.max_age:
            return self._value
        try:
            return await self._shared_refresh()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if self._value is None:
                raise
            logger.warning("Overview refresh failed; serving snapshot aged %.0fs error=%s",
                           self.age or 0, type(exc).__name__)
            return self._value

    async def run(self, sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep) -> None:
        """Background warmer. Never raises except on cancellation."""
        while True:
            try:
                age = self.age
                # Skip if something else (an alert check, a 🔄 tap) refreshed recently.
                if age is None or age >= self.interval * 0.9:
                    await self._shared_refresh()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Background overview refresh failed error=%s", type(exc).__name__)
            await sleep(self.interval)
