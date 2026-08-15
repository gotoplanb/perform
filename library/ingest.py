"""Turn anything playable into the one format the mixer accepts.

Everything crossing into Perform — a pack asset, a path Wander handed over, a
`say` recording — passes through here exactly once and comes out as 48 kHz
stereo float32 WAV with its treatment already baked in. That single decision
buys three things:

- **The mixer never resamples or decodes.** Conduct's Piper output is 22.05 kHz
  mono MP3; the device wants 48 kHz stereo float32. Doing that conversion in the
  render callback would be the most expensive thing on the audio thread. Doing
  it once, here, makes the callback a slice and a multiply.
- **Treatments are free at play time.** See treatments.py — the filter is an
  ffmpeg `-af` chain applied right here, and the cache key includes it.
- **One code path for everything**, which is what SPEC §5 asks for: "pre-generating
  a theme" is warming this cache, and it is the same function a just-in-time
  play calls. An unfinished warm degrades to a few slow items, not a failure.

ffmpeg rather than a Python decoder because it is already a hard dependency of
this system — conduct's Dockerfile apt-installs it for the TTS path — and
because "decode anything, resample, filter, convert" is one invocation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
from pathlib import Path

import numpy as np
import soundfile as sf

from audio.treatments import filter_for
from core.errors import IngestError
from core.item import Media, Treatment

log = logging.getLogger(__name__)

# Long enough for a chapter of audiobook, short enough that a wedged ffmpeg
# doesn't hang a scene forever. Conduct uses 120 for the same call.
FFMPEG_TIMEOUT_S = 120


def ffmpeg_available() -> bool:
    return shutil.which("ffmpeg") is not None


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def content_key(
    source: Path, treatment: Treatment, *, sample_rate: int, channels: int, voice: str = ""
) -> str:
    """The cache key: content, plus everything that changes the rendered result.

    SPEC §5 says "keyed by content plus voice". Sample rate and channel count
    join it because changing either makes every stored blob unplayable, and a
    key that silently returns the wrong-rate blob is worse than a cache miss.
    Treatment joins it because the same line through `phone` is a different
    sound.

    Hashing bytes rather than path+mtime is what makes the cache survive
    conduct handing over the same audio under a fresh job-id filename — which
    it does on every re-synthesis, since it has no cache of its own.
    """
    h = hashlib.sha256()
    with source.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    h.update(f"|{treatment}|{sample_rate}|{channels}|{voice}".encode())
    return h.hexdigest()


def probe(source: Path) -> dict:
    """What ffprobe knows about a file: streams, duration, and — the one that
    matters for SPEC §4 — whether a video arrived carrying a live audio track."""
    if not ffprobe_available():
        return {}
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "stream=codec_type,codec_name,sample_rate,channels:format=duration",
                "-of",
                "json",
                str(source),
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return {}
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, json.JSONDecodeError) as e:
        log.warning("ffprobe failed on %s: %s", source, e)
        return {}


def media_of(source: Path) -> Media:
    """Classify by what ffprobe finds, not by extension.

    Extension-sniffing gets `.mp4` wrong when it holds only audio, and that
    mistake routes an item to the wrong sink, where it fails as a capability
    mismatch rather than as the mislabel it actually is.
    """
    info = probe(source)
    kinds = {s.get("codec_type") for s in info.get("streams", [])}
    if "video" in kinds:
        # A single-frame "video" stream is how ffprobe reports a still.
        codecs = {
            s.get("codec_name")
            for s in info.get("streams", [])
            if s.get("codec_type") == "video"
        }
        if codecs & {"mjpeg", "png", "gif", "webp", "bmp"} and "audio" not in kinds:
            return Media.IMAGE
        return Media.VIDEO
    if "audio" in kinds:
        return Media.AUDIO
    raise IngestError(
        f"{source.name}: ffprobe found neither an audio nor a video stream — "
        f"is this actually media?"
    )


def has_audio_track(source: Path) -> bool:
    info = probe(source)
    return any(s.get("codec_type") == "audio" for s in info.get("streams", []))


def normalize_audio(
    source: Path,
    dest: Path,
    *,
    sample_rate: int,
    channels: int,
    treatment: Treatment = Treatment.CLEAN,
) -> dict:
    """Decode, resample, apply the treatment, write float32 WAV at `dest`.

    Returns a small stats dict the caller stores alongside the cache entry —
    same shape as conduct's `synthesize_to_mp3`, which also hands timing and
    sizes back for the caller to record rather than logging them itself.
    """
    if not ffmpeg_available():
        raise IngestError(
            "ffmpeg not found on PATH — Perform needs it to decode and resample "
            "assets (`brew install ffmpeg`). It is the same dependency conduct's "
            "TTS path already requires."
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(source)]
    af = filter_for(treatment)
    if af:
        cmd += ["-af", af]
    cmd += [
        "-ar", str(sample_rate),
        "-ac", str(channels),
        "-c:a", "pcm_f32le",
        "-f", "wav",
        # Drop any video stream: this is the audio path, and a still image
        # riding along would be decoded for nothing.
        "-vn",
        str(dest),
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=FFMPEG_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        raise IngestError(f"{source.name}: ffmpeg timed out after {FFMPEG_TIMEOUT_S}s") from e
    if result.returncode != 0:
        raise IngestError(f"{source.name}: ffmpeg failed: {result.stderr.strip()[:300]}")

    info = sf.info(str(dest))
    return {
        "frames": info.frames,
        "duration_s": info.duration,
        "sample_rate": info.samplerate,
        "channels": info.channels,
        "bytes": dest.stat().st_size,
        "treatment": str(treatment),
    }


def load_pcm(path: Path, *, channels: int) -> np.ndarray:
    """Read a normalized blob into the array the mixer slices.

    `always_2d` so a mono blob still arrives as (n, 1) and the channel handling
    downstream has one shape to think about rather than two.
    """
    pcm, _ = sf.read(str(path), dtype="float32", always_2d=True)
    if pcm.shape[1] != channels:
        from audio.dsp import to_stereo  # noqa: PLC0415 - avoid a cycle at import time

        pcm = to_stereo(pcm, channels)
    return np.ascontiguousarray(pcm, dtype=np.float32)
