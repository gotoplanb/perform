"""The local visual surface: a browser page, driven over SSE.

SPEC §4: *"A small local web server rendering to a browser page. Push. Reports
completion — the page can report that a video ended or an animation hit its loop
count, which is what keeps visuals sequencing exactly like audio. Server-to-client
push with automatic reconnect is the right shape; validate the specific mechanism
against how Wander and Conduct already handle streaming."*

That validation came back negative, and it is worth writing down: **neither
sibling streams anything.** Conduct is `202 + poll_url` end to end — no
StreamingResponse, no SSE, no WebSocket — and Wander is HTMX fragment swaps over
plain POST. So this is a new mechanism in the system rather than conformance to
an existing one.

SSE anyway, for three reasons: the browser cannot poll (it has to show item N
the instant the audio bus reaches it, and a poll interval is exactly the drift
we are trying to avoid), `EventSource` reconnects on its own with no client code,
and it stays inside the all-HTTP posture both siblings share rather than
introducing a socket protocol.

## Audio never comes out of here

Audio and video stay separate (SPEC §4). Video is served with `muted` set, which
is correct architecturally *and* happens to sidestep browser autoplay policy
entirely, since muted media autoplays unconditionally. A video that arrives
carrying a live audio track is played silently — and said out loud in the
warnings, because "fails silently" is the one outcome SPEC §8 refuses.

## Nobody attached is a first-class state

An SSE stream with no subscriber is not an error, it is a laptop with the tab
closed. `status()` reports the subscriber count for exactly that reason: it is
the fastest answer to "why is the screen blank", and without it the bus looks
perfectly healthy while rendering to nothing.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.item import DurationKind, Item, Media
from library.library import Library
from sinks.base import BaseSink, Dispatch, SinkCapabilities

log = logging.getLogger(__name__)

# Bounded so a browser that stops reading cannot grow the queue without limit.
# Overflow drops the oldest event and warns — a surface that fell behind is
# showing the wrong thing, and that needs saying.
QUEUE_DEPTH = 32

# Sent when nothing else has been for a while. Keeps proxies and laptop power
# management from quietly dropping an idle connection, which would otherwise
# present as a surface that stops updating for no visible reason.
HEARTBEAT_S = 15.0


class VisualSink(BaseSink):
    name = "visual"
    capabilities = SinkCapabilities(
        accepts=frozenset({Media.VIDEO, Media.IMAGE}),
        push=True,
        reports_completion=True,  # the page posts back to /surface/complete
    )

    def __init__(self, library: Library) -> None:
        self._library = library
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._live: dict[str, str] = {}  # bus -> item_id currently shown
        self.on_complete = None
        self.dropped_events = 0
        # Replayed to a browser that connects (or reconnects) mid-scene, so a
        # refresh does not blank the screen until the next item.
        self._last_by_bus: dict[str, dict[str, Any]] = {}

    # --- subscription ---------------------------------------------------

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=QUEUE_DEPTH)
        self._subscribers.add(queue)
        for event in self._last_by_bus.values():
            queue.put_nowait(event)
        log.info("surface attached (%d now)", len(self._subscribers))
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        log.info("surface detached (%d left)", len(self._subscribers))

    @property
    def attached(self) -> int:
        return len(self._subscribers)

    def _emit(self, event: dict[str, Any]) -> None:
        # No copy needed: this is synchronous end to end, so nothing can
        # subscribe or unsubscribe midway through.
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                # The browser stopped reading. Drop the oldest rather than the
                # newest: what it needs most is the current state.
                self.dropped_events += 1
                try:
                    queue.get_nowait()
                    queue.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    # --- sink interface -------------------------------------------------

    async def start(self, item: Item) -> Dispatch:
        resolved = self._library.resolve_visual(item.asset)

        warnings = list(resolved.warnings)
        if not self._subscribers:
            warnings.append(
                f"no browser attached to the visual surface — {item.label or item.id!r} is "
                f"rendering to nobody. Open /surface."
            )

        event = {
            "type": "show",
            "bus": item.bus,
            "item_id": item.id,
            "label": item.label,
            "media": resolved.media.value,
            "url": resolved.url_path,
            "transition": item.transition.value,
            # Duration travels to the page rather than being enforced here: the
            # page is the only thing that knows when a video actually ended or
            # an animation actually hit its loop count, and taking its word for
            # it is what keeps visuals sequencing like audio instead of drifting.
            "duration": {
                "kind": item.duration.kind.value,
                "repeat": item.duration.repeat,
                "seconds": item.duration.seconds,
            },
            "muted": True,
            "audio_stripped": resolved.audio_stripped,
        }
        self._live[item.bus] = item.id
        self._last_by_bus[item.bus] = event
        self._emit(event)

        expected = (
            item.duration.seconds if item.duration.kind is DurationKind.DECLARED else None
        )
        return Dispatch(
            item_id=item.id,
            accepted=True,
            expected_duration_s=expected,
            detail=resolved.url_path,
            warnings=tuple(warnings),
        )

    async def stop(self, item_id: str | None = None) -> None:
        # list() is load-bearing: the loop pops from _live as it goes.
        for bus in list(self._live):
            if item_id is None or self._live.get(bus) == item_id:
                self._live.pop(bus, None)
                self._last_by_bus.pop(bus, None)
                self._emit({"type": "clear", "bus": bus})

    async def stop_bus(self, bus: str) -> None:
        self._live.pop(bus, None)
        self._last_by_bus.pop(bus, None)
        self._emit({"type": "clear", "bus": bus})

    def report_complete(self, item_id: str, *, natural: bool = True) -> bool:
        """Called by `POST /surface/complete` when the page says an item ended.

        Returns whether the id was one we were waiting on — a stale report from
        a reconnected tab is normal and must not advance the wrong item.

        Synchronous, like the callback it invokes: the whole out-of-band
        completion path is bookkeeping over in-memory state, and marking it
        async would suggest it might block somewhere it must not.
        """
        bus = next((b for b, i in self._live.items() if i == item_id), None)
        if bus is None:
            return False
        self._live.pop(bus, None)
        if self.on_complete is not None:
            self.on_complete(bus, item_id, natural)
        return True

    def status(self) -> dict:
        return {
            "attached": self.attached,
            "showing": dict(self._live),
            "dropped_events": self.dropped_events,
        }
