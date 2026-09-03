"""The SSE endpoint, driven directly.

I previously wrote that this route could not be tested. That was too broad: it
cannot be driven through httpx's `ASGITransport`, which buffers a response body
to completion and therefore hangs on an endless generator. Calling the route
function and consuming `response.body_iterator` has neither problem.

What is still only checked by hand against a real uvicorn is the HTTP framing
around it — status line, headers reaching a browser, `EventSource` reconnecting.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

from core.item import AssetRef, Duration, DurationKind, Item, Media
from library.library import ResolvedVisual
from routes import surface
from sinks.registry import SinkRegistry
from sinks.visual import VisualSink


class FakeLibrary:
    warnings: list[str] = []

    def resolve_visual(self, ref) -> ResolvedVisual:
        return ResolvedVisual(
            key="deadbeef",
            name=str(ref),
            media=Media.IMAGE,
            path=__import__("pathlib").Path("/dev/null"),
            url_path="/media/deadbeef.png",
            audio_stripped=False,
            warnings=(),
        )


class FakeEngine:
    def __init__(self, sinks: SinkRegistry) -> None:
        self.sinks = sinks
        self.warnings: list = []

    def warn(self, bus, kind, detail):
        self.warnings.append((bus, kind, detail))


@pytest.fixture
def sink() -> VisualSink:
    return VisualSink(FakeLibrary())


@pytest.fixture
def engine(sink) -> FakeEngine:
    registry = SinkRegistry()
    registry.register(sink)
    return FakeEngine(registry)


def visual_item(label: str = "the bridge") -> Item:
    return Item(
        bus="backdrop",
        asset=AssetRef(key="scene.bridge"),
        media=Media.IMAGE,
        duration=Duration(kind=DurationKind.UNTIL_REPLACED),
        label=label,
    )


async def take(iterator, timeout: float = 2.0) -> str:
    return await asyncio.wait_for(iterator.__anext__(), timeout=timeout)


# --- the stream ---------------------------------------------------------


async def test_the_stream_opens_with_a_retry_hint(engine):
    """The browser's default retry is aggressive enough to hammer a server that
    is restarting."""
    response = await surface.events(engine)
    iterator = response.body_iterator
    try:
        assert await take(iterator) == "retry: 2000\n\n"
    finally:
        await iterator.aclose()


async def test_it_sets_the_event_stream_headers(engine):
    response = await surface.events(engine)
    try:
        assert response.media_type == "text/event-stream"
        assert response.headers["cache-control"] == "no-cache"
        # nginx and friends buffer by default, which turns an event stream into
        # a very slow batch job.
        assert response.headers["x-accel-buffering"] == "no"
    finally:
        await response.body_iterator.aclose()


async def test_a_placed_item_arrives_as_an_sse_frame(engine, sink):
    response = await surface.events(engine)
    iterator = response.body_iterator
    try:
        await take(iterator)  # the retry hint
        await sink.start(visual_item("the bridge"))

        frame = await take(iterator)
        assert frame.startswith("event: show\ndata: ")
        payload = json.loads(frame.split("data: ", 1)[1])
        assert payload["label"] == "the bridge"
        assert payload["muted"] is True
        assert frame.endswith("\n\n")
    finally:
        await iterator.aclose()


async def test_a_clear_arrives_too(engine, sink):
    response = await surface.events(engine)
    iterator = response.body_iterator
    try:
        await take(iterator)
        await sink.start(visual_item())
        await take(iterator)

        await sink.stop_bus("backdrop")
        frame = await take(iterator)
        assert frame.startswith("event: clear\n")
    finally:
        await iterator.aclose()


async def test_an_idle_stream_emits_a_keepalive(engine, monkeypatch):
    """Keeps proxies and laptop power management from dropping an idle
    connection, which would otherwise look like a surface that mysteriously
    stopped updating."""
    monkeypatch.setattr(surface, "HEARTBEAT_S", 0.01)

    response = await surface.events(engine)
    iterator = response.body_iterator
    try:
        await take(iterator)
        assert await take(iterator) == ": keepalive\n\n"
    finally:
        await iterator.aclose()


async def test_closing_the_stream_unsubscribes(engine, sink):
    """A browser that goes away must not leave its queue attached forever —
    every event would then be copied into a queue nobody reads."""
    response = await surface.events(engine)
    iterator = response.body_iterator
    await take(iterator)
    assert sink.attached == 1

    await iterator.aclose()
    assert sink.attached == 0


async def test_two_browsers_each_get_the_event(engine, sink):
    first = (await surface.events(engine)).body_iterator
    second = (await surface.events(engine)).body_iterator
    try:
        await take(first)
        await take(second)
        await sink.start(visual_item("shared"))

        for iterator in (first, second):
            payload = json.loads((await take(iterator)).split("data: ", 1)[1])
            assert payload["label"] == "shared"
    finally:
        await first.aclose()
        await second.aclose()


# --- guards -------------------------------------------------------------


def test_no_visual_sink_is_a_404():
    """A deployment whose buses all route to audio has no surface, and asking
    for its event stream should say so rather than 500."""
    engine = FakeEngine(SinkRegistry())
    with pytest.raises(HTTPException) as excinfo:
        surface._sink(engine)
    assert excinfo.value.status_code == 404


# --- completion callback ------------------------------------------------


async def test_complete_routes_to_the_sink(engine, sink):
    item = visual_item()
    await sink.start(item)

    result = await surface.complete(
        surface.CompleteIn(item_id=item.id), engine
    )
    assert result == {"accepted": True, "item_id": item.id}


async def test_a_stale_completion_is_accepted_and_ignored(engine):
    result = await surface.complete(surface.CompleteIn(item_id="deadbeef"), engine)
    assert result["accepted"] is False


async def test_a_detail_is_recorded_as_a_warning(engine, sink):
    """The page reporting 'video failed to decode' has to reach /status, or the
    bus advances and nobody learns why the screen was blank."""
    item = visual_item()
    await sink.start(item)

    await surface.complete(
        surface.CompleteIn(item_id=item.id, natural=False, detail="decode failed"),
        engine,
    )
    assert any("decode failed" in w[2] for w in engine.warnings)
