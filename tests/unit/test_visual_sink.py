"""The visual sink's pub/sub and event shape, tested directly.

## Why these are unit tests and not integration tests

The SSE *route* cannot be driven through httpx's `ASGITransport`: that transport
buffers the entire response body before returning it, so an endless event
generator produces nothing and the call hangs — not a bug in the route, a
documented limitation of testing ASGI apps in-process. Verified empirically
before writing this file.

So the contract is tested where the logic actually lives. `routes/surface.py` is
a thin serializer over exactly these calls: it subscribes, formats each event as
`event: <type>\\ndata: <json>`, and unsubscribes in a `finally`. What is *not*
covered here is that wire framing and the browser's own behaviour; those are in
the manual verification steps in the README, and a real uvicorn server is the
only honest way to check them.

Following conduct's precedent in `tests/unit/test_tts.py` — where the real
dependency cannot be faked, test the surface that can be and say plainly what
was left out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from core.item import AssetRef, Duration, DurationKind, Item, Media, Transition
from library.library import ResolvedVisual
from sinks.visual import QUEUE_DEPTH, VisualSink


class FakeLibrary:
    """Stands in for the library so these tests need no media and no ffmpeg."""

    def __init__(self, *, media=Media.IMAGE, audio_stripped=False) -> None:
        self.media = media
        self.audio_stripped = audio_stripped
        self.warnings: list[str] = []

    def resolve_visual(self, ref) -> ResolvedVisual:
        warnings = ("carried an audio track",) if self.audio_stripped else ()
        return ResolvedVisual(
            key="deadbeef",
            name=str(ref),
            media=self.media,
            path=Path("/dev/null"),
            url_path="/media/deadbeef.png",
            audio_stripped=self.audio_stripped,
            warnings=warnings,
        )


def visual_item(
    *, kind: DurationKind = DurationKind.UNTIL_REPLACED, repeat=None, seconds=None, label=""
) -> Item:
    return Item(
        bus="backdrop",
        asset=AssetRef(key="scene.bridge"),
        media=Media.IMAGE,
        duration=Duration(kind=kind, repeat=repeat, seconds=seconds),
        transition=Transition.FADE,
        label=label,
    )


@pytest.fixture
def sink() -> VisualSink:
    return VisualSink(FakeLibrary())


async def test_a_subscriber_receives_a_show_event(sink):
    queue = sink.subscribe()
    await sink.start(visual_item(label="the bridge"))

    event = queue.get_nowait()
    assert event["type"] == "show"
    assert event["bus"] == "backdrop"
    assert event["label"] == "the bridge"
    assert event["media"] == "image"
    assert event["url"] == "/media/deadbeef.png"
    assert event["transition"] == "fade"


async def test_video_is_always_muted(sink):
    """Audio and video stay separate (SPEC §4). Not negotiable — and it also
    makes autoplay unconditional, which is a happy side effect rather than the
    reason."""
    queue = sink.subscribe()
    await sink.start(visual_item())
    assert queue.get_nowait()["muted"] is True


async def test_the_duration_policy_travels_to_the_page(sink):
    """Duration goes to the page rather than being enforced server-side: only
    the page knows when a video actually ended or an animation actually hit its
    loop count, and taking its word for it is what stops visuals drifting away
    from audio."""
    queue = sink.subscribe()
    await sink.start(visual_item(kind=DurationKind.REPEAT, repeat=3))

    event = queue.get_nowait()
    assert event["duration"] == {"kind": "repeat", "repeat": 3, "seconds": None}


async def test_a_declared_duration_is_reported_as_expected(sink):
    dispatch = await sink.start(visual_item(kind=DurationKind.DECLARED, seconds=4.0))
    assert dispatch.expected_duration_s == 4.0


async def test_an_open_ended_item_has_no_expected_duration(sink):
    """Nothing will ever signal, and pretending otherwise would arm a backstop
    timer that fires on a bus doing exactly what it should."""
    dispatch = await sink.start(visual_item())
    assert dispatch.expected_duration_s is None


async def test_every_subscriber_gets_the_event(sink):
    queues = [sink.subscribe() for _ in range(3)]
    await sink.start(visual_item())
    assert all(q.get_nowait()["type"] == "show" for q in queues)


async def test_a_late_subscriber_is_caught_up(sink):
    """A refresh mid-scene should not blank the screen until the next item."""
    await sink.start(visual_item(label="already showing"))
    queue = sink.subscribe()

    event = queue.get_nowait()
    assert event["label"] == "already showing"


async def test_unsubscribe_stops_delivery(sink):
    queue = sink.subscribe()
    sink.unsubscribe(queue)
    await sink.start(visual_item())

    assert sink.attached == 0
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


async def test_no_browser_attached_is_warned_about(sink):
    """The fastest answer to "why is the screen blank". A bus rendering to
    nobody looks perfectly healthy otherwise."""
    dispatch = await sink.start(visual_item(label="unseen"))
    assert any("no browser attached" in w for w in dispatch.warnings)


async def test_no_warning_once_a_browser_is_attached(sink):
    sink.subscribe()
    dispatch = await sink.start(visual_item())
    assert not any("no browser" in w for w in dispatch.warnings)


async def test_a_video_with_an_audio_track_is_flagged(sink):
    """SPEC §4: this case 'will fail silently rather than error'. The muting was
    never the missing part; anyone being told was."""
    sink = VisualSink(FakeLibrary(media=Media.VIDEO, audio_stripped=True))
    queue = sink.subscribe()

    dispatch = await sink.start(visual_item())

    assert queue.get_nowait()["audio_stripped"] is True
    assert any("audio track" in w for w in dispatch.warnings)


async def test_a_slow_subscriber_drops_the_oldest_not_the_newest(sink):
    """A browser that stopped reading needs the *current* state most. Growing
    the queue without limit would be the other failure."""
    queue = sink.subscribe()
    for i in range(QUEUE_DEPTH + 5):
        await sink.start(visual_item(label=f"item-{i}"))

    assert sink.dropped_events > 0
    assert queue.qsize() <= QUEUE_DEPTH

    labels = []
    while not queue.empty():
        labels.append(queue.get_nowait()["label"])
    assert labels[-1] == f"item-{QUEUE_DEPTH + 4}"


async def test_completion_from_the_page_is_routed_to_the_engine(sink):
    seen: list[tuple[str, str, bool]] = []

    async def on_complete(bus, item_id, natural):
        seen.append((bus, item_id, natural))

    sink.on_complete = on_complete
    it = visual_item()
    await sink.start(it)

    accepted = await sink.report_complete(it.id)
    assert accepted is True
    assert seen == [("backdrop", it.id, True)]


async def test_a_stale_completion_is_ignored_not_misrouted(sink):
    """A tab that reconnected mid-scene has no way to know its id is stale, and
    advancing the wrong item would be worse than doing nothing."""
    seen = []

    async def on_complete(bus, item_id, natural):
        seen.append(item_id)

    sink.on_complete = on_complete
    await sink.start(visual_item())

    assert await sink.report_complete("some-old-id") is False
    assert seen == []


async def test_an_error_report_is_not_recorded_as_played(sink):
    seen: list[bool] = []

    async def on_complete(bus, item_id, natural):
        seen.append(natural)

    sink.on_complete = on_complete
    it = visual_item()
    await sink.start(it)

    await sink.report_complete(it.id, natural=False)
    assert seen == [False]


async def test_stop_clears_the_surface(sink):
    queue = sink.subscribe()
    await sink.start(visual_item())
    queue.get_nowait()

    await sink.stop_bus("backdrop")

    event = queue.get_nowait()
    assert event["type"] == "clear"
    assert event["bus"] == "backdrop"


async def test_stop_forgets_the_replay_state(sink):
    """Otherwise a browser reconnecting after a stop would be handed the thing
    that was just stopped."""
    await sink.start(visual_item(label="stopped item"))
    await sink.stop()

    queue = sink.subscribe()
    with pytest.raises(asyncio.QueueEmpty):
        queue.get_nowait()


async def test_status_reports_what_is_showing(sink):
    sink.subscribe()
    it = visual_item(label="the bridge")
    await sink.start(it)

    status = sink.status()
    assert status["attached"] == 1
    assert status["showing"] == {"backdrop": it.id}


def test_capabilities_are_declared_not_assumed(sink):
    caps = sink.capabilities
    assert caps.accepts_media(Media.IMAGE)
    assert caps.accepts_media(Media.VIDEO)
    assert not caps.accepts_media(Media.AUDIO)
    assert caps.push is True
    assert caps.reports_completion is True
