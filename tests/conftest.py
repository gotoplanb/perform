"""Shared fixtures.

The whole suite honors SPEC §8: *"Fully testable with no audio device and no
display. A test run should never make noise or open a window."*

Two seams make that true, and they are the same two the production code uses:

- **`NullDevice`** renders through the real mix graph and discards the output,
  so mixer tests assert on actual sample values with nothing plugged in.
- **`NullSink` + `ManualClock`** let engine tests drive sequencing by hand:
  place three items, complete them in an order the test chooses, assert what
  happened. No sleeping, no waiting, no flakiness under load.

Nothing here monkeypatches around a device. There is no device.

The DB is a per-test tmp_path sqlite file rather than conduct's
transaction-with-savepoints pattern — Perform's only durable state is a cache,
so a fresh file per test is simpler and just as isolated.
"""

from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import pytest
import pytest_asyncio

from audio.device import NullDevice
from audio.mixer import Mixer
from core.bus import Bus, DuckRule
from core.clock import ManualClock
from core.engine import Engine
from core.item import AssetRef, Duration, DurationKind, Item, Media
from library.ingest import ffmpeg_available
from library.library import Library
from library.store import Store
from sinks.null import NullSink
from sinks.registry import SinkRegistry

SAMPLE_RATE = 48_000
CHANNELS = 2
BLOCK = 512


# Skipping locally is a convenience; skipping in CI is a hole — without ffmpeg
# the tests that need it would quietly disappear and the run would still report
# green, claiming coverage of paths nothing exercised.
#
# So the skip is conditional on *not* being in CI. On a contributor's machine
# with no ffmpeg the rest of the suite still runs; on a runner, a missing
# dependency fails loudly instead of silently shrinking the suite.
requires_ffmpeg = pytest.mark.skipif(
    not ffmpeg_available() and not os.environ.get("CI"),
    reason="ffmpeg is needed here (install it, or set CI=1 to fail instead of skip)",
)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def mixer() -> Mixer:
    return Mixer(sample_rate=SAMPLE_RATE, channels=CHANNELS, block_size=BLOCK)


@pytest.fixture
def device(mixer: Mixer) -> NullDevice:
    dev = NullDevice(
        mixer.render, sample_rate=SAMPLE_RATE, channels=CHANNELS, block_size=BLOCK
    )
    dev.start()
    return dev


@pytest.fixture
def store(tmp_path: Path) -> Store:
    s = Store(tmp_path / "library.sqlite")
    yield s
    s.close()


@pytest.fixture
def library(store: Store, tmp_path: Path) -> Library:
    """A library with the built-in tones registered but no packs.

    `bootstrap` synthesizes the tones numerically — no media files, no ffmpeg,
    no network — which is exactly why SPEC §5 says to ship them that way.
    """
    lib = Library(
        store=store,
        blobs_dir=tmp_path / "blobs",
        packs_dir=tmp_path / "packs",
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
    )
    lib.bootstrap()
    return lib


@pytest.fixture
def sinks() -> SinkRegistry:
    reg = SinkRegistry()
    reg.register(NullSink("null"))
    return reg


@pytest.fixture
def buses() -> list[Bus]:
    """A voice bus that must not rest open-ended, and an ambience bus that may.

    That asymmetry is the point of the fixture: it is the difference between a
    steady state and a hung scene, and several tests turn on it.
    """
    return [
        Bus(
            name="voice",
            sink="null",
            ducks=[DuckRule(target="ambience", depth_db=-12.0, attack_ms=80, release_ms=400)],
        ),
        Bus(name="ambience", sink="null", gain_db=-6.0, resting_open_ended=True),
    ]


@pytest_asyncio.fixture
async def engine(buses, sinks, mixer, clock, device):
    eng = Engine(
        buses=buses, sinks=sinks, mixer=mixer, clock=clock, stall_after_s=30.0
    )
    eng.attach_device(device)
    await eng.start()
    yield eng
    await eng.close()


@pytest.fixture
def null_sink(sinks: SinkRegistry) -> NullSink:
    return sinks.get("null")


# --- helpers ----------------------------------------------------------------


def item(
    bus: str = "voice",
    label: str = "",
    *,
    asset: str = "tone.ok",
    kind: DurationKind = DurationKind.INTRINSIC,
    repeat: int | None = None,
    seconds: float | None = None,
    media: Media = Media.AUDIO,
) -> Item:
    return Item(
        bus=bus,
        asset=AssetRef(key=asset),
        media=media,
        duration=Duration(kind=kind, repeat=repeat, seconds=seconds),
        label=label,
    )


def tone(frequency: float, seconds: float, amplitude: float = 1.0) -> np.ndarray:
    """A constant-amplitude test signal. Constant so gain assertions are exact —
    a decaying tone would make "did the duck reach -12dB" a question about the
    envelope rather than about the duck."""
    n = int(seconds * SAMPLE_RATE)
    t = np.arange(n, dtype=np.float64) / SAMPLE_RATE
    wave = np.sin(2 * np.pi * frequency * t) * amplitude
    return np.repeat(wave[:, None], CHANNELS, axis=1).astype(np.float32)


def dc(value: float, seconds: float) -> np.ndarray:
    """A constant. The clearest possible signal for asserting a gain envelope:
    the output *is* the gain."""
    n = int(seconds * SAMPLE_RATE)
    return np.full((n, CHANNELS), value, dtype=np.float32)
