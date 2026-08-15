"""The scheduler: one task per bus, advancing on completion.

Buses run concurrently and items on one bus play serially (SPEC §3), which maps
onto one asyncio task per bus, each walking its own list. The engine coordinates
nothing beyond that — it does not decide what to play when results arrive out of
order, it does not retry, it does not stall on Wander's behalf. Those are
Wander's problems, and Wander is the only component with the context to solve
them well (SPEC §2).

## How a bus advances

Three cases, and the third is the one that makes the model media-agnostic:

1. **The sink reports completion** — audio when a buffer runs out, the browser
   when a video ends or an animation hits its loop count. The bus waits for the
   signal and then takes the next item.
2. **The sink cannot report completion** — the Claude client, where media
   arrives only as the result of a tool call and nothing comes back. The bus
   advances on dispatch; duration and loop metadata ride along as advisory
   values for the client to honor. This is not a gap to be closed later, it is
   a property of the medium.
3. **The item is open-ended** (`until_replaced`) — nothing will ever signal.
   The bus holds it and wakes when something is placed or stopped. Whether that
   hold is *rest* or a *hang* is the bus's call, not the item's: a backdrop bus
   is expected to end here, a voice bus doing the same is a hung scene, and
   `/status` says which.

## What stop means

Stop clears the lane: the current item is interrupted *and* everything pending
behind it is dropped. A stop that only killed the current line would let the
next one start a few milliseconds later, which is not "stopping" to anyone
holding the remote. SPEC §3 puts it plainly — being unable to shut it up is the
most common reason tools like this get abandoned, and it matters more here
because Wander drives Perform faster than a person can react.

Dropped items are marked DROPPED and stay in the list. They are reported, never
silently discarded (SPEC §8).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from audio.mixer import Configure, Mixer, StopAll
from core.bus import Bus, BusState, PlacedItem
from core.clock import Clock, SystemClock
from core.errors import SinkRejected, UnknownBus
from core.item import DurationKind, Item, ItemState
from observability import tracing
from sinks.registry import SinkRegistry

log = logging.getLogger(__name__)

# How often the engine drains the mixer's completion deque. The mixer cannot
# signal us — see the threading note in audio/mixer.py — so we poll. One block
# is ~10.7ms at the defaults, which is the granularity at which "the line
# finished" becomes "start the next one". Inaudible, and it buys a render
# callback that never touches a lock.
COMPLETION_POLL_S = 0.01


@dataclass
class Warning_:
    """Something an operator should see in `/status`.

    SPEC §8: *"Failures visible, never silent. A dropped item, a missing asset
    name, a stalled bus, a muted-on-arrival video — each obvious from status
    output. 'Why is it quiet' should take seconds to answer."* This is that
    list, and everything that could make Perform quiet writes to it.
    """

    at: float
    bus: str
    kind: str
    detail: str


class Engine:
    def __init__(
        self,
        *,
        buses: list[Bus],
        sinks: SinkRegistry,
        mixer: Mixer,
        clock: Clock | None = None,
        stall_after_s: float = 30.0,
        warning_limit: int = 200,
    ) -> None:
        self.buses: dict[str, Bus] = {b.name: b for b in buses}
        self.sinks = sinks
        self.mixer = mixer
        self.clock = clock or SystemClock()
        self.stall_after_s = stall_after_s
        self.warnings: list[Warning_] = []
        self._warning_limit = warning_limit

        self._tasks: list[asyncio.Task] = []
        self._wake: dict[str, asyncio.Event] = {b.name: asyncio.Event() for b in buses}
        self._stopping: set[str] = set()
        self._waiters: dict[str, asyncio.Future[bool]] = {}
        self._closing = False
        self.device = None

    # --- lifecycle ------------------------------------------------------

    def configure_mixer(self) -> None:
        """Push the bus set into the mixer. Called at startup and on reload."""
        self.mixer.submit(
            Configure(
                gains={b.name: b.gain_db for b in self.buses.values()},
                ducks={b.name: b.ducks for b in self.buses.values()},
            )
        )

    def attach_device(self, device) -> None:
        self.device = device

    @property
    def device_running(self) -> bool:
        return bool(self.device and self.device.running)

    async def start(self) -> None:
        self.configure_mixer()
        for sink in self.sinks:
            # Sinks that signal completion out of band (the browser posting
            # back, a test calling complete()) route through the same channel
            # the mixer's completions take.
            if hasattr(sink, "on_complete"):
                sink.on_complete = self._on_sink_complete
        self._tasks = [
            asyncio.create_task(self._run_bus(bus), name=f"bus:{name}")
            for name, bus in self.buses.items()
        ]
        self._tasks.append(asyncio.create_task(self._pump(), name="mixer-pump"))
        log.info("engine started: %d bus(es) — %s", len(self.buses), ", ".join(self.buses))

    async def close(self) -> None:
        self._closing = True
        self.mixer.submit(StopAll())
        for event in self._wake.values():
            event.set()
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        for sink in self.sinks:
            await sink.close()
        self._tasks.clear()

    # --- placement ------------------------------------------------------

    def bus(self, name: str) -> Bus:
        try:
            return self.buses[name]
        except KeyError:
            raise UnknownBus(
                f"no bus named {name!r} — buses are fixed at startup; "
                f"configured buses are {sorted(self.buses)}"
            ) from None

    def place(self, item: Item) -> PlacedItem:
        """Append an item to its bus. The capability check happens here, at the
        point of the mistake, rather than at play time where it would surface as
        silence."""
        bus = self.bus(item.bus)
        try:
            self.sinks.check_item(bus.sink, item.media)
        except SinkRejected as e:
            self.warn(bus.name, "rejected", str(e))
            raise
        placed = bus.place(item)
        self._wake[bus.name].set()
        return placed

    def replace(self, item_id: str, replacement: Item) -> PlacedItem:
        """Swap a pending item in place, keeping its slot and id.

        The placeholder case from SPEC §3: Wander drops a stand-in, then swaps
        in the real asset when generation lands.
        """
        bus = self.bus(replacement.bus)
        self.sinks.check_item(bus.sink, replacement.media)
        placed = bus.replace(item_id, replacement)
        self._wake[bus.name].set()
        return placed

    def remove(self, bus_name: str, item_id: str) -> PlacedItem:
        bus = self.bus(bus_name)
        placed = bus.remove(item_id)
        self.warn(bus.name, "dropped", f"item {item_id} ({placed.item.label!r}) removed by caller")
        self._wake[bus.name].set()
        return placed

    def find_item(self, item_id: str) -> tuple[Bus, PlacedItem]:
        for bus in self.buses.values():
            for placed in bus.placed:
                if placed.item.id == item_id:
                    return bus, placed
        from core.errors import UnknownItem  # noqa: PLC0415 - avoid an import cycle

        raise UnknownItem(f"no item {item_id!r} on any bus")

    # --- interruption ---------------------------------------------------

    async def stop_bus(self, name: str) -> int:
        """Interrupt one bus and clear its lane. Returns how many were dropped."""
        bus = self.bus(name)
        self._stopping.add(name)

        dropped = 0
        for placed in bus.pending:
            placed.state = ItemState.DROPPED
            placed.note = "bus stopped"
            dropped += 1

        current = bus.current
        if current is not None:
            current.state = ItemState.STOPPED
            current.ended_at = self.clock.now()

        sink = self.sinks.get(bus.sink)
        if hasattr(sink, "stop_bus"):
            await sink.stop_bus(name)
        else:
            await sink.stop()

        bus.state = BusState.IDLE
        bus.resting_since = None
        self._wake[name].set()
        log.info("bus %s stopped (%d pending dropped)", name, dropped)
        return dropped

    async def stop_all(self) -> int:
        """The panic button. Every bus, every sink, immediately."""
        total = 0
        for name in list(self.buses):
            total += await self.stop_bus(name)
        self.mixer.submit(StopAll())
        return total

    # --- the bus loop ---------------------------------------------------

    async def _run_bus(self, bus: Bus) -> None:
        tracer = tracing.get_tracer()
        while not self._closing:
            placed = bus.next_pending()
            if placed is None:
                bus.recompute_state(self.clock.now(), self.stall_after_s)
                await self._sleep_until_work(bus)
                continue

            self._stopping.discard(bus.name)
            item = placed.item
            placed.state = ItemState.PLAYING
            placed.started_at = self.clock.now()
            bus.resting_since = None

            with tracer.start_as_current_span("perform.item") as span:
                span.set_attribute(tracing.BUS, bus.name)
                span.set_attribute(tracing.ITEM_ID, item.id)
                span.set_attribute(tracing.ITEM_LABEL, item.label)
                span.set_attribute(tracing.SINK, bus.sink)
                span.set_attribute(tracing.MEDIA, item.media.value)
                span.set_attribute(tracing.TREATMENT, item.treatment.value)
                span.set_attribute(tracing.DURATION_KIND, item.duration.kind.value)
                span.set_attribute(tracing.ASSET_KEY, str(item.asset))

                try:
                    await self._play(bus, placed, span)
                except asyncio.CancelledError:
                    raise
                except Exception as e:  # noqa: BLE001 - one bad item must not kill the lane
                    # A bus task that dies takes every future item on that bus
                    # with it, silently. Catching here costs one dropped item
                    # and keeps the lane alive.
                    log.exception("item %s on bus %s failed", item.id, bus.name)
                    placed.state = ItemState.DROPPED
                    placed.note = f"{type(e).__name__}: {e}"
                    placed.ended_at = self.clock.now()
                    span.record_exception(e)
                    span.set_attribute(tracing.OUTCOME, "dropped")
                    self.warn(bus.name, "dropped", f"{item.label or item.id}: {e}")

            bus.prune()

    async def _play(self, bus: Bus, placed: PlacedItem, span) -> None:
        item = placed.item
        sink = self.sinks.get(bus.sink)
        caps = sink.capabilities

        waiter: asyncio.Future[bool] = asyncio.get_running_loop().create_future()
        self._waiters[item.id] = waiter
        try:
            dispatch = await sink.start(item)

            if not dispatch.accepted:
                placed.state = ItemState.DROPPED
                placed.note = dispatch.detail or "sink refused the item"
                placed.ended_at = self.clock.now()
                span.set_attribute(tracing.OUTCOME, "dropped")
                self.warn(bus.name, "dropped", f"{item.label or item.id}: {placed.note}")
                return

            for w in dispatch.warnings:
                self.warn(bus.name, "sink", w)

            if not caps.reports_completion:
                # The Claude client. Nothing will ever come back, so the bus
                # advances now and the duration metadata rides along advisory.
                placed.state = ItemState.PLAYED
                placed.ended_at = self.clock.now()
                placed.note = "sink does not report completion; duration advisory"
                span.set_attribute(tracing.OUTCOME, "dispatched")
                return

            if item.duration.kind is DurationKind.UNTIL_REPLACED:
                await self._hold_until_replaced(bus, placed, waiter, span)
                return

            natural = await self._await_completion(bus, placed, waiter, dispatch)
            placed.ended_at = self.clock.now()
            placed.state = ItemState.PLAYED if natural else ItemState.STOPPED
            span.set_attribute(tracing.OUTCOME, "played" if natural else "stopped")
        finally:
            self._waiters.pop(item.id, None)

    async def _await_completion(
        self, bus: Bus, placed: PlacedItem, waiter: asyncio.Future[bool], dispatch
    ) -> bool:
        """Wait for the sink's completion signal, or for a stop.

        A `declared` duration also arms a clock backstop. If the backstop wins,
        the sink failed to report and that gets said out loud rather than
        papered over — a sink that silently stops reporting would otherwise
        present as buses that gradually all go quiet.
        """
        wake = self._wake[bus.name]
        wake.clear()

        waits = [asyncio.ensure_future(asyncio.shield(waiter)), asyncio.ensure_future(wake.wait())]
        backstop: asyncio.Task | None = None
        if placed.item.duration.kind is DurationKind.DECLARED and dispatch.expected_duration_s:
            backstop = asyncio.ensure_future(
                self.clock.sleep(dispatch.expected_duration_s + 1.0)
            )
            waits.append(backstop)

        try:
            done, pending = await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in waits:
                if not task.done():
                    task.cancel()

        if waiter.done():
            return waiter.result()

        if backstop is not None and backstop in done:
            self.warn(
                bus.name,
                "no-completion",
                f"{placed.item.label or placed.item.id}: sink {bus.sink!r} did not report "
                f"completion for a declared {dispatch.expected_duration_s}s item — advanced anyway",
            )
            return True

        # Woken by a stop or by a placement. A stop is an interruption; a
        # placement while something is playing is just a queued item, so we
        # go back to waiting.
        if bus.name in self._stopping:
            return False
        return await self._await_completion(bus, placed, waiter, dispatch)

    async def _hold_until_replaced(
        self, bus: Bus, placed: PlacedItem, waiter: asyncio.Future[bool], span
    ) -> None:
        """Hold an open-ended item until something displaces it.

        The bus is either at rest or hung here, and which one it is depends on
        the bus rather than the item — see core/bus.py. `/status` computes it;
        this loop just waits and lets the stall detector do the judging.

        Three things can end the hold, and the third is easy to miss:

        1. a stop,
        2. something placed behind it — that is the "replaced" in until_replaced,
        3. **the sink reporting completion anyway.**

        Nothing ends an open-ended item *naturally*, so (3) always means
        something went wrong: the page failed to decode the video, or the tab
        went away. Not listening for it leaves the lane wedged forever on a
        signal the sink already sent, which is precisely the silent hang SPEC §8
        exists to prevent.
        """
        wake = self._wake[bus.name]
        bus.resting_since = self.clock.now()
        span.set_attribute(tracing.OUTCOME, "holding")

        while not self._closing:
            wake.clear()
            bus.recompute_state(self.clock.now(), self.stall_after_s)
            if bus.state is BusState.STALLED:
                self.warn(
                    bus.name,
                    "stalled",
                    f"holding open-ended item {placed.item.label or placed.item.id!r} for "
                    f"{self.clock.now() - (bus.resting_since or 0):.0f}s — bus {bus.name!r} is "
                    f"not declared resting_open_ended, so this is a hung lane, not a steady state",
                )

            waits = [
                asyncio.ensure_future(asyncio.shield(waiter)),
                asyncio.ensure_future(wake.wait()),
            ]
            try:
                await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in waits:
                    if not task.done():
                        task.cancel()

            if waiter.done():
                natural = waiter.result()
                placed.state = ItemState.PLAYED if natural else ItemState.STOPPED
                placed.ended_at = self.clock.now()
                bus.resting_since = None
                span.set_attribute(tracing.OUTCOME, "played" if natural else "stopped")
                if not natural:
                    self.warn(
                        bus.name,
                        "sink",
                        f"{placed.item.label or placed.item.id}: sink reported a failure on an "
                        f"open-ended item — advancing the bus rather than holding it",
                    )
                return

            if bus.name in self._stopping:
                placed.state = ItemState.STOPPED
                placed.ended_at = self.clock.now()
                span.set_attribute(tracing.OUTCOME, "stopped")
                return
            if bus.next_pending() is not None:
                placed.state = ItemState.PLAYED
                placed.ended_at = self.clock.now()
                bus.resting_since = None
                span.set_attribute(tracing.OUTCOME, "replaced")
                return

    async def _sleep_until_work(self, bus: Bus) -> None:
        event = self._wake[bus.name]
        event.clear()
        await event.wait()

    # --- completion plumbing --------------------------------------------

    async def _pump(self) -> None:
        """Move completions from the mixer's deque onto the asyncio side.

        Polling rather than being signalled — see COMPLETION_POLL_S and the
        threading note in audio/mixer.py.
        """
        while not self._closing:
            for completion in self.mixer.drain_completions():
                self._resolve(completion.item_id, completion.natural)
            await asyncio.sleep(COMPLETION_POLL_S)

    async def _on_sink_complete(self, bus: str, item_id: str, natural: bool) -> None:
        """Completion from a sink that signals out of band — the browser posting
        back, or a test."""
        self._resolve(item_id, natural)

    def _resolve(self, item_id: str, natural: bool) -> None:
        waiter = self._waiters.get(item_id)
        if waiter is not None and not waiter.done():
            waiter.set_result(natural)

    def pump_once(self) -> int:
        """Drain the mixer synchronously. The test-facing counterpart to `_pump`:
        a test that pulled blocks from NullDevice calls this instead of waiting
        out a poll interval."""
        completions = self.mixer.drain_completions()
        for completion in completions:
            self._resolve(completion.item_id, completion.natural)
        return len(completions)

    # --- reporting ------------------------------------------------------

    def warn(self, bus: str, kind: str, detail: str) -> None:
        self.warnings.append(Warning_(at=self.clock.now(), bus=bus, kind=kind, detail=detail))
        del self.warnings[: max(0, len(self.warnings) - self._warning_limit)]

    def status(self) -> dict:
        """The 'why is it quiet' answer (SPEC §8).

        Deliberately not a debug endpoint. It carries every bus's state, what is
        playing and how far in, what is queued, the effective gain including any
        live duck, and the warning list — so the question takes seconds rather
        than a log trawl.
        """
        now = self.clock.now()
        snap = self.mixer.snapshot.get("buses", {})

        buses = {}
        for name, bus in self.buses.items():
            state = bus.recompute_state(now, self.stall_after_s)
            current = bus.current
            mix = snap.get(name, {})
            buses[name] = {
                "state": state.value,
                "sink": bus.sink,
                "gain_db": bus.gain_db,
                "duck_db": mix.get("duck_db", 0.0),
                "effective_db": mix.get("effective_db", bus.gain_db),
                "resting_open_ended": bus.resting_open_ended,
                "ducks": [
                    {"target": d.target, "depth_db": d.depth_db} for d in bus.ducks
                ],
                "current": _item_view(current, now, mix) if current else None,
                "pending": [_item_view(p, now, {}) for p in bus.pending],
                "recent": [
                    _item_view(p, now, {}) for p in bus.placed if p.terminal
                ][-5:],
            }

        return {
            "device": {
                "running": self.device_running,
                "xruns": getattr(self.device, "xruns", 0),
                "frames_rendered": self.mixer.snapshot.get("frames_rendered", 0),
            },
            "buses": buses,
            "sinks": {sink.name: sink.status() for sink in self.sinks},
            "warnings": [
                {
                    "at": round(w.at, 2),
                    "age_s": round(now - w.at, 1),
                    "bus": w.bus,
                    "kind": w.kind,
                    "detail": w.detail,
                }
                for w in reversed(self.warnings[-25:])
            ],
            "quiet_because": self._quiet_because(buses),
        }

    def _quiet_because(self, buses: dict) -> str | None:
        """One sentence answering the actual question, when there is one.

        SPEC §8 asks that "why is it quiet" take seconds. Making a human read a
        bus table and infer it is not seconds; saying it is.
        """
        if not self.device_running:
            return "the audio device is not running — check the startup log for the device open"
        stalled = [n for n, b in buses.items() if b["state"] == "stalled"]
        if stalled:
            return (
                f"bus(es) {', '.join(stalled)} are holding an open-ended item and are not "
                f"declared resting_open_ended — Wander placed something with duration "
                f"'until_replaced' and never replaced it"
            )
        playing = [n for n, b in buses.items() if b["state"] == "playing"]
        if playing:
            return None
        pending = sum(len(b["pending"]) for b in buses.values())
        if pending:
            return f"{pending} item(s) are queued but no bus has started one — check the warnings"
        recent_drop = next(
            (w for w in reversed(self.warnings[-25:]) if w.kind in ("dropped", "rejected")), None
        )
        if recent_drop:
            return f"nothing is playing; the last event was a dropped item — {recent_drop.detail}"
        return "nothing is playing and nothing is queued — Perform is idle"


def _item_view(placed: PlacedItem | None, now: float, mix: dict) -> dict | None:
    if placed is None:
        return None
    item = placed.item
    return {
        "id": item.id,
        "label": item.label,
        "asset": str(item.asset),
        "media": item.media.value,
        "treatment": item.treatment.value,
        "transition": item.transition.value,
        "duration": {
            "kind": item.duration.kind.value,
            "repeat": item.duration.repeat,
            "seconds": item.duration.seconds,
        },
        "state": placed.state.value,
        "elapsed_s": mix.get("elapsed_s")
        or (round(now - placed.started_at, 2) if placed.started_at else None),
        "total_s": mix.get("total_s"),
        "cycles": mix.get("cycles", 0),
        "note": placed.note,
    }
