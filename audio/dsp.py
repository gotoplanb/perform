"""The arithmetic the mixer does per block. All of it vectorized.

Deliberately small. Filtering happens at ingest (see treatments.py), so what is
left in the real-time path is gain, ramps, summing, and a limiter — every one of
which is a numpy expression over a block rather than a loop over samples.

Everything here is pure: same inputs, same outputs, no state beyond what the
caller passes in. That is what lets the mixer tests assert output blocks
sample-for-sample instead of listening to them.
"""

from __future__ import annotations

import numpy as np

# Below this, treat a gain as silence. -120dB is ~1e-6, comfortably under the
# noise floor of any real converter, and it stops a release ramp from
# asymptotically approaching zero forever.
SILENCE_DB = -120.0


def db_to_linear(db: float | np.ndarray) -> float | np.ndarray:
    """Amplitude ratio, not power — 6dB is a doubling."""
    return np.power(10.0, np.asarray(db, dtype=np.float64) / 20.0)


def linear_to_db(linear: float) -> float:
    if linear <= 0:
        return SILENCE_DB
    return float(20.0 * np.log10(linear))


def approach_db(current_db: float, target_db: float, step_db: float) -> float:
    """Move `current_db` toward `target_db` by at most `step_db`.

    Ramping in dB rather than in linear amplitude is what makes a duck sound
    like a duck: a linear ramp to -12dB spends most of its time in the last
    couple of dB, which reads as a lurch and then a crawl.
    """
    if current_db < target_db:
        return min(current_db + step_db, target_db)
    return max(current_db - step_db, target_db)


def ramp_step_db(depth_db: float, ramp_ms: float, block_ms: float) -> float:
    """dB of travel per block, such that the full `depth_db` takes `ramp_ms`.

    Used for duck attack/release. Exact by construction, which is the point —
    a test can assert that a -12dB duck with an 80ms attack lands on exactly
    -12.0 after 80ms of blocks and not a dB before.
    """
    if ramp_ms <= 0:
        return abs(depth_db)
    return abs(depth_db) * (block_ms / ramp_ms)


def gain_ramp(frames: int, start_db: float, end_db: float, channels: int) -> np.ndarray:
    """Per-sample gain for one block, interpolated between two dB values.

    Applying one gain to a whole block and a different one to the next block is
    a step discontinuity, and a step discontinuity is a click. Interpolating
    across the block costs one linspace and removes the entire class of bug.
    """
    if start_db == end_db:
        return np.full((frames, channels), db_to_linear(start_db), dtype=np.float32)
    curve = db_to_linear(np.linspace(start_db, end_db, frames, dtype=np.float64))
    return np.repeat(curve[:, None], channels, axis=1).astype(np.float32)


def linear_ramp(frames: int, start: float, end: float, channels: int) -> np.ndarray:
    """Per-sample linear-amplitude ramp. Used for de-click and crossfade, where
    the ear wants a straight amplitude line rather than a dB curve."""
    curve = np.linspace(start, end, frames, dtype=np.float32)
    return np.repeat(curve[:, None], channels, axis=1)


# Below the knee the limiter is exactly transparent; above it, the curve bends
# toward full scale and never reaches it. 0.8 leaves ~2dB of headroom to work
# in, which is enough to catch a bus sum without colouring anything normal.
CLIP_KNEE = 0.8


def soft_clip(block: np.ndarray) -> np.ndarray:
    """Keep the sum inside [-1, 1] without the buzz of hard clipping.

    Buses are summed, so three items that are each legal on their own can still
    exceed full scale together.

    A plain `tanh(x)` is the obvious implementation and it is wrong: tanh(1.0)
    is 0.76, so a single item sitting at full scale would come out 2.4dB down
    for no reason, and every gain assertion downstream would be off by that
    much. The knee below fixes it — identity under `CLIP_KNEE`, and a smooth
    bend above it that asymptotes to 1.0.
    """
    if not block.size:
        return block
    peak = float(np.max(np.abs(block)))
    if peak <= CLIP_KNEE:
        return block

    magnitude = np.abs(block)
    over = magnitude > CLIP_KNEE
    if not over.any():
        return block

    headroom = 1.0 - CLIP_KNEE
    shaped = magnitude.copy()
    shaped[over] = CLIP_KNEE + headroom * np.tanh(
        (magnitude[over] - CLIP_KNEE) / headroom
    )
    return (np.sign(block) * shaped).astype(np.float32)


def to_stereo(pcm: np.ndarray, channels: int) -> np.ndarray:
    """Fan a mono buffer out to `channels`, or pass through if it already fits.

    Piper's output is mono and the device is stereo, so this runs on nearly
    every asset conduct produces.
    """
    if pcm.ndim == 1:
        pcm = pcm[:, None]
    if pcm.shape[1] == channels:
        return pcm.astype(np.float32, copy=False)
    if pcm.shape[1] == 1:
        return np.repeat(pcm, channels, axis=1).astype(np.float32)
    # More channels than the device wants: fold down rather than dropping the
    # extras, so a stereo asset on a mono device keeps both sides.
    return pcm[:, :channels].astype(np.float32, copy=False)
