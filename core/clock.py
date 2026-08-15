"""Time, as an injectable dependency.

SPEC §8 requires the whole suite to run with no audio device and no display, and
names bus interaction and completion signalling as the behaviors most worth
testing. Both are *timing* behaviors: a declared duration elapses, a duck ramp
reaches its depth, a bus sits open-ended long enough to count as stalled.

Testing those against the wall clock means either sleeping for real (slow, and
flaky under load) or asserting nothing. So the engine takes a `Clock`, and the
tests hand it a `ManualClock` they drive by hand. This is the same move conduct
makes with `stub_registry` in tests/conftest.py — the seam exists so the test
can be exact rather than approximate.

Note the asymmetry with the *audio* clock: the mixer's sense of time is the
sound card's, counted in frames rendered, and it is deliberately not this. See
audio/mixer.py.
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol


class Clock(Protocol):
    """Monotonic time plus a cancellable sleep."""

    def now(self) -> float:
        """Seconds, monotonic. Only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real one. `time.monotonic` because playback spans must survive an
    NTP step or a DST change without a bus deciding it has been stalled for an
    hour."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """A clock that only moves when a test tells it to.

    `sleep` registers a future against a deadline; `advance` resolves every
    future whose deadline has passed and then yields, so the tasks that were
    waiting actually get to run before `advance` returns. That yield is the
    whole reason this is worth writing: without it a test would have to
    interleave `advance` and `asyncio.sleep(0)` by hand and would eventually
    get the ordering subtly wrong.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    async def advance(self, seconds: float) -> None:
        """Move time forward and let everything that was waiting run."""
        self._now += seconds
        due = [(d, f) for d, f in self._waiters if d <= self._now]
        self._waiters = [(d, f) for d, f in self._waiters if d > self._now]
        for _, fut in due:
            if not fut.done():
                fut.set_result(None)
        # Two yields: one to resolve the awaiting coroutines, one to let
        # whatever they scheduled in turn (a sink dispatch, a completion
        # callback) actually run before the caller asserts.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    def pending_sleeps(self) -> int:
        """How many coroutines are parked. Handy for asserting that a bus is
        genuinely waiting on a timer rather than wedged on something else."""
        return len(self._waiters)
