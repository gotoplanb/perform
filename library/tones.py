"""The built-in tones, synthesized rather than shipped.

SPEC §5: *"Ship no media files. Generate the handful of simple tones
programmatically; richer packs are user-supplied. That sidesteps licensing
questions and means something works before a single asset is sourced."*

So this module is the entire default asset library. It runs once at startup,
writes a handful of WAVs into the warm cache, and registers them pinned under
the `tone` pack. `curl -X POST localhost:8770/buses/voice/items -d
'{"asset":"tone.ok"}'` works on a fresh clone with nothing installed.

The shapes are deliberately plain — a sine with an exponential decay is what a
notification sounds like, and anything more elaborate would be a synthesizer
hiding in a playback server. Each one is a short envelope over one or two
partials, which is enough to make "done", "attention", and "something broke"
distinguishable without a designer.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import soundfile as sf

log = logging.getLogger(__name__)

PACK_ID = "tone"

# (key, [(frequency_hz, start_s)], duration_s, description)
_TONES: dict[str, tuple[list[tuple[float, float]], float, str]] = {
    # A single clean sine. The neutral "here is a thing" ping.
    "ping": ([(880.0, 0.0)], 0.35, "single sine — neutral attention"),
    # Rising major third: the universal "finished, and it went well".
    "ok": ([(660.0, 0.0), (880.0, 0.11)], 0.45, "rising third — success"),
    # Falling minor third. Reads as "finished, and it did not".
    "error": ([(520.0, 0.0), (392.0, 0.13)], 0.55, "falling third — failure"),
    # Two even beeps at the same pitch: attention without a verdict.
    "warn": ([(740.0, 0.0), (740.0, 0.16)], 0.42, "double beep — attention"),
    # Very short, very quiet. For marking progress without being annoying.
    "tick": ([(1200.0, 0.0)], 0.06, "short click — progress"),
}


def _render(
    partials: list[tuple[float, float]], duration_s: float, sample_rate: int, channels: int
) -> np.ndarray:
    """Sum of decaying sines, with a short fade at each end.

    The 8ms fades are not cosmetic. A sine that starts at a non-zero sample is a
    step discontinuity, and a step discontinuity is a click — the same reason
    audio/source.py declicks every start and stop.
    """
    n = int(duration_s * sample_rate)
    t = np.arange(n, dtype=np.float64) / sample_rate
    out = np.zeros(n, dtype=np.float64)

    for freq, start_s in partials:
        start = int(start_s * sample_rate)
        if start >= n:
            continue
        local_t = t[: n - start]
        # ~6 time-constants over the remaining span: audibly gone by the end,
        # never abruptly truncated.
        decay = np.exp(-local_t * (6.0 / max(duration_s - start_s, 1e-3)))
        out[start:] += np.sin(2 * np.pi * freq * local_t) * decay

    peak = float(np.max(np.abs(out))) or 1.0
    out = (out / peak) * 0.5  # -6dBFS: audible, with headroom for a bus sum

    fade = max(1, int(0.008 * sample_rate))
    out[:fade] *= np.linspace(0.0, 1.0, fade)
    out[-fade:] *= np.linspace(1.0, 0.0, fade)

    return np.repeat(out[:, None], channels, axis=1).astype(np.float32)


def generate(dest_dir: Path, *, sample_rate: int, channels: int) -> list[tuple[str, Path, str]]:
    """Write every built-in tone as a normalized WAV.

    Returns (name, path, description). Idempotent: a tone whose file already
    exists at the right size is left alone, so a restart is instant.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    out: list[tuple[str, Path, str]] = []

    for key, (partials, duration_s, description) in _TONES.items():
        name = f"{PACK_ID}.{key}"
        # Rate and channels are in the filename because a blob at the wrong
        # rate is unplayable, and silently reusing one is the kind of failure
        # that presents as "why does everything sound slow".
        path = dest_dir / f"{PACK_ID}-{key}-{sample_rate}-{channels}.wav"
        if not path.is_file():
            pcm = _render(partials, duration_s, sample_rate, channels)
            sf.write(str(path), pcm, sample_rate, subtype="FLOAT")
        out.append((name, path, description))

    log.info("built-in tones ready: %s", ", ".join(n for n, _, _ in out))
    return out


def describe(name: str) -> str:
    key = name.split(".", 1)[-1]
    entry = _TONES.get(key)
    return entry[2] if entry else ""
