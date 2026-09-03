"""Media ingest — classification, probing, and the failure paths.

The happy paths run constantly through the surface and speak suites. What was
uncovered is everything that goes wrong: no ffmpeg, a timeout, a non-zero exit,
a file that is not media at all. Each of those has a message a human has to act
on, so each is worth asserting.
"""

from __future__ import annotations

import subprocess

import pytest

from core.errors import IngestError
from core.item import Media, Treatment
from library import ingest
from tests.conftest import requires_ffmpeg

# --- availability probes ------------------------------------------------


def test_availability_reflects_the_path(monkeypatch):
    monkeypatch.setattr(ingest.shutil, "which", lambda name: "/usr/bin/" + name)
    assert ingest.ffmpeg_available() is True
    assert ingest.ffprobe_available() is True

    monkeypatch.setattr(ingest.shutil, "which", lambda name: None)
    assert ingest.ffmpeg_available() is False
    assert ingest.ffprobe_available() is False


def test_normalize_without_ffmpeg_says_how_to_fix_it(monkeypatch, tmp_path):
    """The message has to name the remedy — this is the first thing a new
    contributor hits."""
    monkeypatch.setattr(ingest, "ffmpeg_available", lambda: False)
    with pytest.raises(IngestError, match="brew install ffmpeg"):
        ingest.normalize_audio(
            tmp_path / "in.wav", tmp_path / "out.wav", sample_rate=48_000, channels=2
        )


# --- probe --------------------------------------------------------------


def test_probe_returns_nothing_without_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "ffprobe_available", lambda: False)
    assert ingest.probe(tmp_path / "x.wav") == {}


def test_probe_survives_a_failing_ffprobe(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "ffprobe_available", lambda: True)
    monkeypatch.setattr(
        ingest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "nope"),
    )
    assert ingest.probe(tmp_path / "x.wav") == {}


def test_probe_survives_unparseable_output(monkeypatch, tmp_path, caplog):
    monkeypatch.setattr(ingest, "ffprobe_available", lambda: True)
    monkeypatch.setattr(
        ingest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, "not json", ""),
    )
    assert ingest.probe(tmp_path / "x.wav") == {}


def test_probe_survives_a_subprocess_error(monkeypatch, tmp_path):
    def boom(*a, **k):
        raise subprocess.SubprocessError("no")

    monkeypatch.setattr(ingest, "ffprobe_available", lambda: True)
    monkeypatch.setattr(ingest.subprocess, "run", boom)
    assert ingest.probe(tmp_path / "x.wav") == {}


# --- classification -----------------------------------------------------


def test_media_of_classifies_by_streams_not_extension(monkeypatch, tmp_path):
    """Extension-sniffing gets `.mp4` wrong when it holds only audio, and that
    mistake routes an item to the wrong sink."""
    monkeypatch.setattr(
        ingest, "probe", lambda p: {"streams": [{"codec_type": "audio"}]}
    )
    assert ingest.media_of(tmp_path / "misleading.mp4") is Media.AUDIO


def test_a_video_stream_means_video(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ingest,
        "probe",
        lambda p: {"streams": [{"codec_type": "video", "codec_name": "h264"}]},
    )
    assert ingest.media_of(tmp_path / "clip.mp4") is Media.VIDEO


def test_a_single_frame_codec_with_no_audio_is_an_image(monkeypatch, tmp_path):
    """ffprobe reports a still as a one-frame video stream."""
    monkeypatch.setattr(
        ingest,
        "probe",
        lambda p: {"streams": [{"codec_type": "video", "codec_name": "png"}]},
    )
    assert ingest.media_of(tmp_path / "still.png") is Media.IMAGE


def test_a_still_codec_alongside_audio_is_video(monkeypatch, tmp_path):
    """Album art in an mp3 is not an image asset."""
    monkeypatch.setattr(
        ingest,
        "probe",
        lambda p: {
            "streams": [
                {"codec_type": "video", "codec_name": "mjpeg"},
                {"codec_type": "audio", "codec_name": "mp3"},
            ]
        },
    )
    assert ingest.media_of(tmp_path / "tagged.mp3") is Media.VIDEO


def test_something_that_is_not_media_at_all_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "probe", lambda p: {"streams": []})
    with pytest.raises(IngestError, match="actually media"):
        ingest.media_of(tmp_path / "notes.txt")


def test_has_audio_track(monkeypatch, tmp_path):
    monkeypatch.setattr(
        ingest,
        "probe",
        lambda p: {"streams": [{"codec_type": "video"}, {"codec_type": "audio"}]},
    )
    assert ingest.has_audio_track(tmp_path / "noisy.mp4") is True

    monkeypatch.setattr(ingest, "probe", lambda p: {"streams": [{"codec_type": "video"}]})
    assert ingest.has_audio_track(tmp_path / "silent.mp4") is False


# --- normalize failures -------------------------------------------------


def test_a_timeout_names_the_file_and_the_limit(monkeypatch, tmp_path):
    monkeypatch.setattr(ingest, "ffmpeg_available", lambda: True)

    def timing_out(*a, **k):
        raise subprocess.TimeoutExpired(a[0], ingest.FFMPEG_TIMEOUT_S)

    monkeypatch.setattr(ingest.subprocess, "run", timing_out)

    with pytest.raises(IngestError, match="timed out"):
        ingest.normalize_audio(
            tmp_path / "in.wav", tmp_path / "out.wav", sample_rate=48_000, channels=2
        )


def test_a_nonzero_exit_includes_ffmpeg_stderr(monkeypatch, tmp_path):
    """Without the stderr, 'ffmpeg failed' is unactionable."""
    monkeypatch.setattr(ingest, "ffmpeg_available", lambda: True)
    monkeypatch.setattr(
        ingest.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0], 1, "", "Invalid data found when processing input"
        ),
    )

    with pytest.raises(IngestError, match="Invalid data found"):
        ingest.normalize_audio(
            tmp_path / "in.wav", tmp_path / "out.wav", sample_rate=48_000, channels=2
        )


# --- the real thing -----------------------------------------------------


@requires_ffmpeg
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


@requires_ffmpeg
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


@requires_ffmpeg
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
