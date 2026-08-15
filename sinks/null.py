"""A sink that records what it was told and completes when asked.

The second half of SPEC §8's constraint — *"fully testable with no audio device
and no display"* — and the counterpart to `NullDevice`. Where NullDevice proves
the mixing arithmetic, this proves the *sequencing*: that items start in order,
that a replacement plays instead of the thing it replaced, that a stop reaches
every bus, that a dropped item is skipped and reported.

Those are the behaviors SPEC calls *"the most worth testing and the most likely
to break silently"*, and they are exactly the ones that need no sound at all to
verify. A test places three items, calls `complete()` on each in turn, and
asserts the order — no device, no clock, no waiting.

This is conduct's `_StubProvider` for a different interface.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.item import Item, Media
from sinks.base import BaseSink, Dispatch, SinkCapabilities


@dataclass
class Recorded:
    item: Item
    stopped: bool = False
    completed: bool = False


class NullSink(BaseSink):
    name = "null"
    # Accepts everything: a test routing a video bus at this sink is testing
    # the engine, not the capability check, and the capability check has its
    # own tests.
    capabilities = SinkCapabilities(
        accepts=frozenset(Media),
        push=True,
        reports_completion=True,
    )

    def __init__(self, name: str = "null", *, reports_completion: bool = True) -> None:
        self.name = name
        self.capabilities = SinkCapabilities(
            accepts=frozenset(Media), push=True, reports_completion=reports_completion
        )
        self.dispatched: list[Recorded] = []
        self.stops: list[str | None] = []
        # Set by the engine so `complete` can push a completion back through
        # the same channel the mixer uses.
        self.on_complete = None
        self.fail_next: str | None = None

    async def start(self, item: Item) -> Dispatch:
        if self.fail_next:
            detail, self.fail_next = self.fail_next, None
            return Dispatch(item_id=item.id, accepted=False, detail=detail)
        self.dispatched.append(Recorded(item=item))
        return Dispatch(item_id=item.id, accepted=True, expected_duration_s=None)

    async def stop(self, item_id: str | None = None) -> None:
        self.stops.append(item_id)
        for rec in self.dispatched:
            if not rec.completed and (item_id is None or rec.item.id == item_id):
                rec.stopped = True

    # --- test-facing ----------------------------------------------------

    @property
    def order(self) -> list[str]:
        """Labels in dispatch order — the assertion most tests actually want."""
        return [r.item.label or r.item.id for r in self.dispatched]

    @property
    def live(self) -> Recorded | None:
        for rec in self.dispatched:
            if not rec.completed and not rec.stopped:
                return rec
        return None

    async def complete(self, item_id: str | None = None, *, natural: bool = True) -> None:
        """Signal that an item finished. `None` completes the oldest live one.

        This is the seam: real sinks report completion when a buffer runs out or
        a browser posts back, and a test reports it when it feels like it.
        """
        rec = self.live if item_id is None else next(
            (r for r in self.dispatched if r.item.id == item_id), None
        )
        if rec is None:
            return
        rec.completed = True
        if self.on_complete is not None:
            await self.on_complete(rec.item.bus, rec.item.id, natural)

    def status(self) -> dict:
        return {
            "dispatched": len(self.dispatched),
            "stops": len(self.stops),
            "order": self.order,
        }
