"""Time as an injectable dependency.

`ManualClock` is used by nearly every engine test, but its own edges — the
zero-length sleep, `pending_sleeps`, an already-resolved future — were never
asserted directly. `SystemClock` was not covered at all, which matters because
it is what actually runs in production.
"""

from __future__ import annotations

import asyncio
import time

from core.clock import ManualClock, SystemClock

# --- SystemClock --------------------------------------------------------


def test_system_clock_is_monotonic():
    """`time.monotonic` because playback spans must survive an NTP step or a
    DST change without a bus deciding it has been stalled for an hour."""
    clock = SystemClock()
    first = clock.now()
    second = clock.now()
    assert second >= first
    # And it is the monotonic clock, not the wall clock.
    assert abs(first - time.monotonic()) < 1.0


async def test_system_clock_sleeps():
    clock = SystemClock()
    before = clock.now()
    await clock.sleep(0.01)
    assert clock.now() - before >= 0.005


def test_both_clocks_satisfy_the_protocol():
    """Structurally rather than via isinstance: `Clock` is a plain Protocol, and
    making it @runtime_checkable purely so a test can call isinstance would be
    changing production for the test's convenience."""
    for clock in (SystemClock(), ManualClock()):
        assert callable(clock.now)
        assert asyncio.iscoroutinefunction(clock.sleep)


# --- ManualClock --------------------------------------------------------


def test_it_starts_where_it_is_told():
    assert ManualClock().now() == 0.0
    assert ManualClock(start=100.0).now() == 100.0


def test_time_only_moves_when_told():
    clock = ManualClock()
    assert clock.now() == 0.0
    time.sleep(0.01)
    assert clock.now() == 0.0


async def test_advance_moves_time():
    clock = ManualClock()
    await clock.advance(5.0)
    assert clock.now() == 5.0


async def test_a_sleeper_wakes_when_its_deadline_passes():
    clock = ManualClock()
    woke = []

    async def sleeper():
        await clock.sleep(10.0)
        woke.append(clock.now())

    task = asyncio.create_task(sleeper())
    await asyncio.sleep(0)

    await clock.advance(5.0)
    assert woke == []

    await clock.advance(6.0)
    assert woke == [11.0]
    await task


async def test_a_zero_or_negative_sleep_yields_without_parking():
    """Otherwise a `declared` duration of 0 would hang the bus forever waiting
    for a deadline that already passed."""
    clock = ManualClock()
    await clock.sleep(0)
    await clock.sleep(-1)
    assert clock.pending_sleeps() == 0


async def test_pending_sleeps_reports_parked_coroutines():
    """Handy for asserting a bus is genuinely waiting on a timer rather than
    wedged on something else."""
    clock = ManualClock()
    assert clock.pending_sleeps() == 0

    tasks = [asyncio.create_task(clock.sleep(10.0)) for _ in range(3)]
    await asyncio.sleep(0)
    assert clock.pending_sleeps() == 3

    await clock.advance(11.0)
    assert clock.pending_sleeps() == 0
    await asyncio.gather(*tasks)


async def test_advancing_past_several_deadlines_wakes_them_all():
    clock = ManualClock()
    order = []

    async def sleeper(seconds, label):
        await clock.sleep(seconds)
        order.append(label)

    tasks = [
        asyncio.create_task(sleeper(1.0, "a")),
        asyncio.create_task(sleeper(2.0, "b")),
        asyncio.create_task(sleeper(30.0, "late")),
    ]
    await asyncio.sleep(0)

    await clock.advance(5.0)
    assert sorted(order) == ["a", "b"]
    assert clock.pending_sleeps() == 1

    await clock.advance(30.0)
    await asyncio.gather(*tasks)
    assert "late" in order


async def test_advance_tolerates_an_already_cancelled_sleeper():
    """A bus task cancelled during shutdown leaves a resolved-or-cancelled
    future behind; advancing must not blow up on it."""
    clock = ManualClock()
    task = asyncio.create_task(clock.sleep(10.0))
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)

    await clock.advance(20.0)  # must not raise
    assert clock.now() == 20.0
