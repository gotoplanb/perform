"""The device boundary — both halves.

`NullDevice` is exercised everywhere already; `PortAudioDevice` was not, because
covering it looks like it needs a sound card. It does not. The native import is
lazy (`import sounddevice` lives inside `start()`), so a stub in `sys.modules`
covers the stream construction, the callback wiring, the xrun counter and the
teardown — all of which are *our* logic, not PortAudio's.

Nothing here opens a stream or makes a sound, which SPEC §8 requires.
"""

from __future__ import annotations

import sys
import types

import numpy as np
import pytest

from audio.device import NullDevice, PortAudioDevice, resolve_device


class FakeStream:
    """Stands in for `sounddevice.OutputStream`."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.started = False
        self.closed = False
        self.active = False
        self.device = kwargs.get("device", "default")

    def start(self):
        self.started = True
        self.active = True

    def stop(self):
        self.active = False

    def close(self):
        self.closed = True


@pytest.fixture
def fake_sd(monkeypatch):
    """Inject a stub `sounddevice` module, recording the constructed stream."""
    module = types.ModuleType("sounddevice")
    created: list[FakeStream] = []

    def OutputStream(**kwargs):  # noqa: N802 — mirrors the real API's name
        stream = FakeStream(**kwargs)
        created.append(stream)
        return stream

    module.OutputStream = OutputStream
    monkeypatch.setitem(sys.modules, "sounddevice", module)
    module.created = created
    return module


def render(frames: int) -> np.ndarray:
    return np.full((frames, 2), 0.25, dtype=np.float32)


def make(device=None) -> PortAudioDevice:
    return PortAudioDevice(
        render, sample_rate=48_000, channels=2, block_size=512, device=device
    )


# --- PortAudioDevice ----------------------------------------------------


def test_start_opens_a_stream_with_the_configured_format(fake_sd):
    dev = make()
    dev.start()

    kwargs = fake_sd.created[0].kwargs
    assert kwargs["samplerate"] == 48_000
    assert kwargs["channels"] == 2
    assert kwargs["blocksize"] == 512
    assert kwargs["dtype"] == "float32"
    assert dev.running is True


def test_an_empty_device_name_means_the_system_default(fake_sd):
    """`PERFORM_AUDIO_DEVICE=` is the unset form; passing "" through to
    PortAudio would be a request for a device literally named empty."""
    make(device="").start()
    assert fake_sd.created[0].kwargs["device"] is None


def test_a_named_device_is_passed_through(fake_sd):
    make(device="Scarlett 2i2").start()
    assert fake_sd.created[0].kwargs["device"] == "Scarlett 2i2"


def test_the_callback_renders_into_the_output_buffer(fake_sd):
    """The one line that actually makes sound. If this is wrong, everything
    upstream is irrelevant."""
    make().start()
    callback = fake_sd.created[0].kwargs["callback"]

    out = np.zeros((512, 2), dtype=np.float32)
    callback(out, 512, None, None)

    assert np.allclose(out, 0.25)


def test_the_callback_counts_xruns_without_logging(fake_sd):
    """Underruns are the audible failure mode, so they are counted and surfaced
    in /status — not logged, because the logging handler takes a lock and this
    is the real-time thread."""
    dev = make()
    dev.start()
    callback = fake_sd.created[0].kwargs["callback"]
    out = np.zeros((512, 2), dtype=np.float32)

    callback(out, 512, None, None)
    assert dev.xruns == 0

    callback(out, 512, None, "output underflow")
    callback(out, 512, None, "output underflow")
    assert dev.xruns == 2


def test_stop_closes_the_stream_and_is_idempotent(fake_sd):
    dev = make()
    dev.start()
    stream = fake_sd.created[0]

    dev.stop()
    assert stream.closed is True
    assert dev.running is False

    dev.stop()  # a second stop must not raise
    assert dev.running is False


def test_a_device_that_never_started_is_not_running(fake_sd):
    assert make().running is False


def test_restarting_opens_a_fresh_stream(fake_sd):
    """Reopening per item would put an audible gap in front of every line, but
    an explicit restart after a device change has to work."""
    dev = make()
    dev.start()
    dev.stop()
    dev.start()
    assert len(fake_sd.created) == 2
    assert dev.running is True


# --- resolve_device -----------------------------------------------------


@pytest.mark.parametrize(
    ("setting", "expected"),
    [
        ("", None),          # the unset form
        ("0", 0),            # an index
        ("3", 3),
        ("MacBook Pro Speakers", "MacBook Pro Speakers"),
        ("Scarlett 2i2 USB", "Scarlett 2i2 USB"),
    ],
)
def test_resolve_device(setting, expected):
    assert resolve_device(setting) == expected


def test_a_numeric_looking_name_becomes_an_index_not_a_name():
    """PortAudio treats an int as an index and a str as a name to match, so the
    distinction decides which speakers get used."""
    assert resolve_device("2") == 2
    assert isinstance(resolve_device("2"), int)


# --- NullDevice ---------------------------------------------------------


def test_null_device_renders_on_demand_only():
    """Nothing advances until `pull` is called — that is what lets a test say
    'render exactly 100 blocks, now assert' rather than sleeping and hoping."""
    dev = NullDevice(render, sample_rate=48_000, channels=2, block_size=512)
    dev.start()
    assert dev.blocks_rendered == 0

    dev.pull(3)
    assert dev.blocks_rendered == 3


def test_null_device_pull_returns_the_audio():
    dev = NullDevice(render, sample_rate=48_000, channels=2, block_size=512)
    dev.start()
    out = dev.pull(2)
    assert out.shape == (1024, 2)
    assert np.allclose(out, 0.25)


def test_null_device_pull_seconds_converts_to_blocks():
    dev = NullDevice(render, sample_rate=48_000, channels=2, block_size=512)
    dev.start()
    out = dev.pull_seconds(0.1)
    # 0.1s at 48k is 4800 frames, rounded to the nearest whole block.
    assert abs(out.shape[0] - 4800) < 512


def test_null_device_pull_of_zero_blocks_is_an_empty_buffer():
    dev = NullDevice(render, sample_rate=48_000, channels=2, block_size=512)
    dev.start()
    out = dev.pull(0)
    assert out.shape == (0, 2)


def test_null_device_reports_running_state():
    dev = NullDevice(render, sample_rate=48_000, channels=2, block_size=512)
    assert dev.running is False
    dev.start()
    assert dev.running is True
    dev.stop()
    assert dev.running is False
