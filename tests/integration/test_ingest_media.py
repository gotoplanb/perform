"""Ingest against real media, with a real ffmpeg.

These live under tests/integration/ rather than tests/unit/ for one concrete
reason: the `checks` CI job runs `pytest tests/unit` with no system packages
installed, on purpose, so a lint slip fails in seconds instead of waiting behind
an apt install. A unit test that shells out to ffmpeg breaks that contract — and
because `requires_ffmpeg` *fails* rather than skips under CI, it broke it
loudly. Which is the marker working as designed, in the wrong directory.

The rule: if it needs a binary, it is an integration test here.
"""

from __future__ import annotations

import subprocess

import pytest

from core.item import Treatment
from library import ingest
from tests.conftest import requires_ffmpeg

pytestmark = requires_ffmpeg

def test_normalize_converts_rate_and_channels(tmp_path):
    """The property the whole design leans on: whatever arrives, the mixer sees
    48k stereo float32 and never resamples in the callback."""
    source = tmp_path / "in.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.2", "-ar", "22050", "-ac", "1", str(source)],
        check=True, capture_output=True,
    )

    stats = ingest.normalize_audio(
        source, tmp_path / "out.wav", sample_rate=48_000, channels=2
    )

    assert stats["sample_rate"] == 48_000
    assert stats["channels"] == 2
    assert stats["duration_s"] == pytest.approx(0.2, abs=0.02)
    assert stats["bytes"] > 0


def test_a_treatment_changes_the_output(tmp_path):
    """Treatments are baked in at ingest and keyed into the cache, which is what
    makes them free at play time."""
    source = tmp_path / "in.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=8000:duration=0.2", str(source)],
        check=True, capture_output=True,
    )

    clean = tmp_path / "clean.wav"
    phone = tmp_path / "phone.wav"
    ingest.normalize_audio(source, clean, sample_rate=48_000, channels=2)
    ingest.normalize_audio(
        source, phone, sample_rate=48_000, channels=2, treatment=Treatment.PHONE
    )

    # An 8kHz tone is well outside the phone band, so the treated file is
    # audibly (and measurably) quieter.
    assert clean.read_bytes() != phone.read_bytes()


def test_load_pcm_returns_the_device_shape(tmp_path):
    source = tmp_path / "in.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "sine=frequency=440:duration=0.1", "-ac", "1", str(source)],
        check=True, capture_output=True,
    )
    out = tmp_path / "out.wav"
    ingest.normalize_audio(source, out, sample_rate=48_000, channels=2)

    pcm = ingest.load_pcm(out, channels=2)
    assert pcm.shape[1] == 2
    assert pcm.dtype.name == "float32"
