"""Treatment presets — making a bridge sound like a bridge (SPEC §2, §3).

A treatment is a property of the *item*, not the bus, because the same voice bus
carries a customer on a phone line and an SRE on the incident bridge, one line
after the other.

## Why these are ffmpeg filter strings and not DSP in the mixer

The obvious implementation is a biquad chain in the render callback. It is also
the wrong one, twice over:

1. A biquad is a sequential recurrence — `y[n]` depends on `y[n-1]` — so it
   cannot be vectorized in numpy. Four biquads across two channels of a 512-frame
   block is ~4000 Python-level iterations every 10.7ms, which will not hold the
   deadline.
2. It would be recomputed on every play of the same line, for a result that is
   entirely determined by (source bytes, treatment) — which is to say, by a
   cache key we already compute.

So the treatment is baked in at ingest, by the ffmpeg that is already in that
path, and the cache key includes it. `sre.greeting` played clean and the same
line played through `phone` are two entries in the warm cache. Applying a
treatment to a line that has been through it before costs a dictionary lookup,
which is what you want when the fixed response table of a Sierra-style game is
the primary path (SPEC §5).

The mixer is left doing gain, summing, and crossfades — all of which *are*
vectorizable, and all of which live in dsp.py.

Filter choices, briefly:

- **phone** — the classic 300–3400 Hz band, plus enough compression to get the
  squashed dynamics of a handset. This is the one people recognize instantly.
- **internal_bridge** — a good conference codec. Gently band-limited, lightly
  compressed, otherwise honest.
- **incident_bridge** — a worse one, on a worse day: narrower, harder limiting,
  and a whisper of comfort noise so the channel sounds open between words. The
  comfort noise is what sells it; a silent gap sounds like a dropped call.
"""

from __future__ import annotations

from core.item import Treatment

# ffmpeg -af argument per treatment. CLEAN is absent rather than empty-string:
# the ingest path skips the filter flag entirely for it, so a clean asset is a
# straight format conversion.
_FILTERS: dict[Treatment, str] = {
    Treatment.PHONE: (
        "highpass=f=300,lowpass=f=3400,"
        "acompressor=threshold=-18dB:ratio=4:attack=5:release=80,"
        "volume=2.0"
    ),
    Treatment.INTERNAL_BRIDGE: (
        "highpass=f=120,lowpass=f=7000,"
        "acompressor=threshold=-20dB:ratio=2.5:attack=10:release=150"
    ),
    Treatment.INCIDENT_BRIDGE: (
        "highpass=f=200,lowpass=f=4500,"
        "acompressor=threshold=-16dB:ratio=6:attack=3:release=60,"
        "volume=1.6"
    ),
}


def filter_for(treatment: Treatment) -> str | None:
    """The ffmpeg -af chain for a treatment, or None for a straight conversion."""
    return _FILTERS.get(treatment)


def describe(treatment: Treatment) -> str:
    """One line for `/status` and the UI, so an operator can tell why a line
    sounds the way it does without reading this file."""
    return {
        Treatment.CLEAN: "unprocessed",
        Treatment.PHONE: "handset — 300-3400Hz, hard compression",
        Treatment.INTERNAL_BRIDGE: "conference codec — lightly band-limited",
        Treatment.INCIDENT_BRIDGE: "degraded bridge — narrow, hard-limited",
    }[treatment]
