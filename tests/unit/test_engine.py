"""Sequencing: ordering, addressability, interruption, stall detection.

SPEC §8 names bus interaction and completion signalling as *"the behaviors most
worth testing and the most likely to break silently."* This file is that, driven
entirely through `NullSink` and `ManualClock` — no device, no display, no
sleeping, and therefore nothing timing-dependent to go flaky under load.
"""

from __future__ import annotations

import pytest

from core.bus import Bus, BusState
from core.errors import ItemAlreadyPlaying, SinkRejected, UnknownBus, UnknownItem
from core.item import DurationKind, ItemState, Media
from sinks.null import NullSink
from sinks.registry import SinkRegistry
from tests.conftest import item


async def settle():
    """Let the bus tasks run. The engine is cooperative, so a couple of yields
    is all that separates 'placed' from 'dispatched'."""
    import asyncio

    for _ in range(6):
        await asyncio.sleep(0)


# --- ordering ---------------------------------------------------------------


async def test_items_play_in_order(engine, null_sink):
    for label in ("first", "second", "third"):
        engine.place(item(label=label))
    await settle()

    # Serial: only the first has been dispatched.
    assert null_sink.order == ["first"]

    await null_sink.complete()
    await settle()
    assert null_sink.order == ["first", "second"]

    await null_sink.complete()
    await settle()
    assert null_sink.order == ["first", "second", "third"]


async def test_buses_run_concurrently(engine, null_sink):
    """Items on one bus play serially; buses run concurrently (SPEC §3)."""
    engine.place(item(bus="voice", label="line"))
    engine.place(item(bus="ambience", label="bed"))
    await settle()

    assert set(null_sink.order) == {"line", "bed"}


async def test_a_finished_item_is_marked_played(engine, null_sink):
    placed = engine.place(item(label="line"))
    await settle()
    await null_sink.complete()
    await settle()

    assert placed.state is ItemState.PLAYED
    assert placed.ended_at is not None


# --- addressability (SPEC §3) ----------------------------------------------


async def test_a_pending_item_can_be_replaced_in_place(engine, null_sink):
    """The placeholder case: Wander drops a stand-in, then swaps in the real
    asset when generation lands."""
    engine.place(item(label="playing"))
    placeholder = engine.place(item(label="placeholder"))
    await settle()

    engine.replace(placeholder.item.id, item(label="the real line"))
    await null_sink.complete()
    await settle()

    assert null_sink.order == ["playing", "the real line"]


async def test_a_replacement_keeps_the_slot_id(engine):
    """So a caller holding the id can keep addressing the slot after a swap."""
    engine.place(item(label="playing"))
    placeholder = engine.place(item(label="placeholder"))
    original_id = placeholder.item.id
    await settle()

    replaced = engine.replace(original_id, item(label="real"))
    assert replaced.item.id == original_id
    assert replaced.item.label == "real"


async def test_replacing_a_playing_item_is_refused(engine, null_sink):
    """Once a bus has started an item the only lever is interruption."""
    playing = engine.place(item(label="playing"))
    await settle()

    with pytest.raises(ItemAlreadyPlaying):
        engine.replace(playing.item.id, item(label="too late"))


async def test_a_pending_item_can_be_removed(engine, null_sink):
    engine.place(item(label="playing"))
    doomed = engine.place(item(label="doomed"))
    engine.place(item(label="after"))
    await settle()

    engine.remove("voice", doomed.item.id)
    await null_sink.complete()
    await settle()

    assert null_sink.order == ["playing", "after"]


async def test_a_removed_item_is_reported_not_vanished(engine):
    """SPEC §8: a dropped item must be obvious from status output. An item that
    simply disappeared is the definition of a silent failure."""
    engine.place(item(label="playing"))
    doomed = engine.place(item(label="doomed"))
    await settle()

    engine.remove("voice", doomed.item.id)

    assert doomed.state is ItemState.DROPPED
    assert doomed.note
    kinds = [w.kind for w in engine.warnings]
    assert "dropped" in kinds

    recent = engine.status()["buses"]["voice"]["recent"]
    assert any(r["label"] == "doomed" and r["state"] == "dropped" for r in recent)


async def test_removing_a_playing_item_is_refused(engine):
    playing = engine.place(item(label="playing"))
    await settle()

    with pytest.raises(ItemAlreadyPlaying):
        engine.remove("voice", playing.item.id)


async def test_unknown_item_and_bus_are_distinct_errors(engine):
    with pytest.raises(UnknownItem):
        engine.find_item("nope")
    with pytest.raises(UnknownBus):
        engine.bus("nope")


# --- interruption (SPEC §3) -------------------------------------------------


async def test_stop_clears_the_lane_not_just_the_current_item(engine, null_sink):
    """A stop that let the next queued item start milliseconds later would not
    be a stop to anyone holding the remote."""
    playing = engine.place(item(label="playing"))
    queued = [engine.place(item(label=f"queued-{i}")) for i in range(3)]
    await settle()

    dropped = await engine.stop_bus("voice")
    await settle()

    assert dropped == 3
    assert playing.state is ItemState.STOPPED
    assert all(q.state is ItemState.DROPPED for q in queued)
    assert null_sink.order == ["playing"]


async def test_stop_all_reaches_every_bus(engine, null_sink):
    engine.place(item(bus="voice", label="line"))
    engine.place(item(bus="ambience", label="bed"))
    await settle()

    await engine.stop_all()
    await settle()

    status = engine.status()
    assert status["buses"]["voice"]["state"] == "idle"
    assert status["buses"]["ambience"]["state"] == "idle"


async def test_the_bus_keeps_working_after_a_stop(engine, null_sink):
    """A stop must not wedge the lane — the next thing placed has to play."""
    engine.place(item(label="doomed"))
    await settle()
    await engine.stop_bus("voice")
    await settle()

    engine.place(item(label="after the stop"))
    await settle()

    assert null_sink.order == ["doomed", "after the stop"]


# --- resting vs stalled (SPEC §3) -------------------------------------------


async def test_a_backdrop_bus_resting_open_ended_is_not_stalled(engine, clock):
    """A bus declared resting_open_ended is *expected* to end on an open-ended
    item. That is its steady state, not a hang."""
    engine.place(item(bus="ambience", label="room tone", kind=DurationKind.UNTIL_REPLACED))
    await settle()
    await clock.advance(600.0)

    assert engine.status()["buses"]["ambience"]["state"] == "resting"


async def test_a_voice_bus_resting_open_ended_is_stalled(engine, clock):
    """Same item, same duration policy, opposite meaning — which is exactly why
    the policy lives on the bus and not on the item."""
    engine.place(item(bus="voice", label="hung line", kind=DurationKind.UNTIL_REPLACED))
    await settle()

    assert engine.status()["buses"]["voice"]["state"] == "playing"

    await clock.advance(31.0)
    assert engine.status()["buses"]["voice"]["state"] == "stalled"


async def test_a_stalled_bus_says_why_it_is_quiet(engine, clock):
    """SPEC §8: 'why is it quiet' should take seconds to answer."""
    engine.place(item(bus="voice", label="hung", kind=DurationKind.UNTIL_REPLACED))
    await settle()
    await clock.advance(31.0)

    why = engine.status()["quiet_because"]
    assert why is not None
    assert "voice" in why
    assert "until_replaced" in why


async def test_an_open_ended_item_yields_when_the_sink_reports_a_failure(
    engine, null_sink, clock
):
    """Nothing ends an open-ended item naturally, so a completion signal on one
    always means something went wrong — the page failed to decode the video, or
    the tab went away. Not listening for it wedges the lane forever on a signal
    the sink already sent."""
    held = engine.place(item(bus="voice", label="held", kind=DurationKind.UNTIL_REPLACED))
    await settle()
    assert held.state is ItemState.PLAYING

    await null_sink.complete(natural=False)
    await settle()

    assert held.state is ItemState.STOPPED
    assert any(w.kind == "sink" for w in engine.warnings)


async def test_an_open_ended_item_that_completes_naturally_is_played(engine, null_sink):
    held = engine.place(item(bus="ambience", label="bed", kind=DurationKind.UNTIL_REPLACED))
    await settle()

    await null_sink.complete(natural=True)
    await settle()

    assert held.state is ItemState.PLAYED


async def test_an_open_ended_item_yields_when_something_is_placed(engine, null_sink, clock):
    """'Held until the next item on that bus begins' — that is the whole of
    until_replaced, and it is what makes a backdrop swap work."""
    held = engine.place(item(bus="ambience", label="held", kind=DurationKind.UNTIL_REPLACED))
    await settle()
    await clock.advance(5.0)

    engine.place(item(bus="ambience", label="replacement"))
    await settle()

    assert held.state is ItemState.PLAYED
    assert null_sink.order == ["held", "replacement"]


# --- sinks ------------------------------------------------------------------


async def test_a_sink_that_never_reports_completion_advances_on_dispatch(
    buses, mixer, clock, device
):
    """The Claude client. Nothing will ever come back, so waiting for it would
    hang the lane forever."""
    from core.engine import Engine

    sinks = SinkRegistry()
    sinks.register(NullSink("null", reports_completion=False))
    eng = Engine(buses=buses, sinks=sinks, mixer=mixer, clock=clock)
    eng.attach_device(device)
    await eng.start()
    try:
        eng.place(item(label="one"))
        eng.place(item(label="two"))
        await settle()

        # Both went out without anyone signalling anything.
        assert sinks.get("null").order == ["one", "two"]
    finally:
        await eng.close()


async def test_a_refused_item_is_dropped_and_reported(engine, null_sink):
    null_sink.fail_next = "sink said no"
    engine.place(item(label="refused"))
    engine.place(item(label="after"))
    await settle()
    await null_sink.complete()
    await settle()

    _, refused = engine.find_item(
        next(p.item.id for p in engine.bus("voice").placed if p.item.label == "refused")
    )
    assert refused.state is ItemState.DROPPED
    assert "sink said no" in refused.note
    # The lane kept going.
    assert null_sink.order == ["after"]


async def test_capability_mismatch_is_refused_at_placement(mixer, clock, device):
    """At the point of the mistake, not forty seconds later as silence."""
    from core.engine import Engine
    from sinks.base import SinkCapabilities

    audio_only = NullSink("audio_only")
    audio_only.capabilities = SinkCapabilities(
        accepts=frozenset({Media.AUDIO}), push=True, reports_completion=True
    )
    sinks = SinkRegistry()
    sinks.register(audio_only)

    eng = Engine(
        buses=[Bus(name="voice", sink="audio_only")], sinks=sinks, mixer=mixer, clock=clock
    )
    eng.attach_device(device)
    await eng.start()
    try:
        with pytest.raises(SinkRejected):
            eng.place(item(media=Media.VIDEO, label="a video on the speakers"))
        assert any(w.kind == "rejected" for w in eng.warnings)
    finally:
        await eng.close()


# --- status -----------------------------------------------------------------


async def test_status_reports_idle_plainly(engine):
    assert "idle" in engine.status()["quiet_because"]


async def test_status_is_silent_about_quiet_when_something_is_playing(engine):
    engine.place(item(label="line"))
    await settle()
    assert engine.status()["quiet_because"] is None


async def test_status_carries_the_label_verbatim(engine):
    """Perform logs the label and reports it but never interprets it."""
    weird = "scene3::turn1 <SRE> {}"
    engine.place(item(label=weird))
    await settle()
    assert engine.status()["buses"]["voice"]["current"]["label"] == weird


async def test_status_lists_pending_items(engine):
    engine.place(item(label="playing"))
    engine.place(item(label="next"))
    engine.place(item(label="after that"))
    await settle()

    voice = engine.status()["buses"]["voice"]
    assert voice["current"]["label"] == "playing"
    assert [p["label"] for p in voice["pending"]] == ["next", "after that"]


async def test_bus_state_is_idle_with_nothing_placed(engine):
    assert engine.status()["buses"]["voice"]["state"] == BusState.IDLE.value
