"""Integration fixtures: the real ASGI app, on a NullDevice.

The app under test is `main.app` — the actual routers, the actual dependency
graph, the actual engine. What is swapped is the two things SPEC §8 says a test
must never touch: the audio device and the display.

The lifespan is bypassed (`ASGITransport` never runs it) and the objects it
would have built are constructed here instead, exactly as `lifespan.build_engine`
does. Same move conduct makes in its conftest, and for the same reason: a fast
boot with no OTel instrumentation side effects.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from audio.device import NullDevice
from audio.mixer import Mixer
from core.bus import Bus, DuckRule
from core.clock import ManualClock
from core.engine import Engine
from library.library import Library
from library.store import Store
from sinks.claude import ClaudeSink
from sinks.local_audio import LocalAudioSink
from sinks.registry import SinkRegistry
from sinks.visual import VisualSink
from tests.conftest import BLOCK, CHANNELS, SAMPLE_RATE


@pytest_asyncio.fixture
async def app(tmp_path: Path):
    from config.settings import get_settings
    from main import app as fastapi_app

    store = Store(tmp_path / "library.sqlite")
    library = Library(
        store=store,
        blobs_dir=tmp_path / "blobs",
        packs_dir=tmp_path / "packs",
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    library.bootstrap()

    mixer = Mixer(sample_rate=SAMPLE_RATE, channels=CHANNELS, block_size=BLOCK)

    sinks = SinkRegistry()
    audio_sink = LocalAudioSink(mixer, library)
    sinks.register(audio_sink)
    sinks.register(VisualSink(library))
    sinks.register(ClaudeSink(library))

    buses = [
        Bus(
            name="voice",
            sink="local_audio",
            ducks=[DuckRule(target="ambience", depth_db=-12.0)],
        ),
        Bus(name="ambience", sink="local_audio", gain_db=-6.0, resting_open_ended=True),
        Bus(name="backdrop", sink="visual", resting_open_ended=True),
    ]
    sinks.validate_routes(buses)

    clock = ManualClock()
    engine = Engine(buses=buses, sinks=sinks, mixer=mixer, clock=clock, stall_after_s=30.0)

    device = NullDevice(
        mixer.render, sample_rate=SAMPLE_RATE, channels=CHANNELS, block_size=BLOCK
    )
    device.start()
    engine.attach_device(device)
    audio_sink.attach_device(device)
    await engine.start()

    fastapi_app.state.engine = engine
    fastapi_app.state.library = library
    fastapi_app.state.settings = get_settings()
    fastapi_app.state.device = device
    fastapi_app.state.clock = clock

    try:
        yield fastapi_app
    finally:
        await engine.close()
        device.stop()
        store.close()
        fastapi_app.dependency_overrides.clear()


@pytest_asyncio.fixture
async def client(app) -> AsyncClient:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def device(app) -> NullDevice:
    return app.state.device


@pytest.fixture
def clock(app) -> ManualClock:
    return app.state.clock


@pytest.fixture
def engine(app):
    return app.state.engine


async def drain(engine, device, seconds: float = 0.3) -> None:
    """Let placements dispatch, render `seconds` of audio, then let completions
    advance the buses.

    The order matters and is easy to get backwards. An item placed over HTTP is
    only appended to its bus list; the bus *task* is what hands it to the sink,
    and the sink is what gives the mixer a source. Pulling frames before those
    yields renders a mix graph that has not heard about the new item yet — which
    presents as a duck that never engaged, or a bus that never made a sound.
    """
    import asyncio

    for _ in range(6):
        await asyncio.sleep(0)
    device.pull_seconds(seconds)
    engine.pump_once()
    for _ in range(6):
        await asyncio.sleep(0)
