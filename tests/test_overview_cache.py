"""Shared, background-refreshed overview cache (app/overview_cache.py)."""
from __future__ import annotations

import asyncio

import pytest

from app.overview_cache import OverviewCache


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Builder:
    def __init__(self, delay: float = 0.02, fail: bool = False) -> None:
        self.calls, self.delay, self.fail, self.concurrent, self.peak = 0, delay, fail, 0, 0

    async def __call__(self):
        self.calls += 1
        self.concurrent += 1
        self.peak = max(self.peak, self.concurrent)
        try:
            await asyncio.sleep(self.delay)
            if self.fail:
                raise RuntimeError("database is locked")
            return {"snapshot": self.calls}
        finally:
            self.concurrent -= 1


async def test_hit_within_max_age_then_rebuild():
    clock, build = Clock(), Builder()
    cache = OverviewCache(build, max_age=30, interval=30, clock=clock)
    first = await cache.get()
    clock.t += 10
    assert await cache.get() is first and build.calls == 1
    clock.t += 25
    assert (await cache.get())["snapshot"] == 2


async def test_concurrent_callers_share_one_scan():
    build = Builder(delay=0.05)
    cache = OverviewCache(build, clock=Clock())
    values = await asyncio.gather(*(cache.get() for _ in range(20)))
    assert build.calls == 1 and build.peak == 1
    assert all(v is values[0] for v in values)
    fresh = await asyncio.gather(*(cache.get(fresh=True) for _ in range(10)))
    assert build.calls == 2 and all(v is fresh[0] for v in fresh)


async def test_failed_refresh_serves_last_good_snapshot():
    clock, build = Clock(), Builder()
    cache = OverviewCache(build, max_age=30, interval=30, clock=clock)
    good = await cache.get()
    build.fail = True
    clock.t += 60
    assert await cache.get() is good
    assert cache.failures == 1 and 59 <= cache.age <= 61


async def test_failure_with_no_snapshot_raises():
    with pytest.raises(RuntimeError):
        await OverviewCache(Builder(fail=True), clock=Clock()).get()


async def test_invalidate():
    build = Builder()
    cache = OverviewCache(build, clock=Clock())
    await cache.get()
    cache.invalidate()
    await cache.get()
    assert build.calls == 2
    empty = OverviewCache(Builder(), clock=Clock())
    empty.invalidate()
    assert empty.age is None


async def test_cancelled_waiter_does_not_abort_shared_scan():
    build = Builder(delay=0.1)
    cache = OverviewCache(build, clock=Clock())
    first = asyncio.ensure_future(cache.get())
    second = asyncio.ensure_future(cache.get())
    await asyncio.sleep(0.02)
    first.cancel()
    assert await second == {"snapshot": 1} and build.calls == 1


async def test_on_refresh_export_failure_is_isolated():
    async def build():
        return "overview"

    async def broken_export(_value):
        raise OSError("disk full")

    cache = OverviewCache(build, on_refresh=broken_export)
    assert await cache.get() == "overview"
    assert cache.failures == 0


async def test_background_warmer_skips_redundant_scans_and_survives_errors():
    clock, build = Clock(), Builder(delay=0)
    cache = OverviewCache(build, max_age=30, interval=30, clock=clock)
    sleeps = {"n": 0}

    async def fake_sleep(_seconds):
        sleeps["n"] += 1
        if sleeps["n"] == 1:
            clock.t += 5
            await cache.get(fresh=True)  # an alert check refreshed just now
            clock.t += 1
        elif sleeps["n"] == 2:
            clock.t += 40
            build.fail = True
        elif sleeps["n"] == 3:
            build.fail = False
            clock.t += 40
        else:
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await cache.run(sleep=fake_sleep)
    assert cache.refreshes == 3 and cache.failures == 1


def test_rejects_non_positive_intervals():
    with pytest.raises(ValueError):
        OverviewCache(Builder(), max_age=0)
