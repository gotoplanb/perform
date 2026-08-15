"""The block arithmetic, on its own.

Small, pure, and worth testing directly because every mixer assertion depends on
these being exactly right rather than approximately right.
"""

from __future__ import annotations

import numpy as np
import pytest

from audio.dsp import (
    CLIP_KNEE,
    approach_db,
    db_to_linear,
    gain_ramp,
    linear_to_db,
    ramp_step_db,
    soft_clip,
    to_stereo,
)


def test_db_is_amplitude_not_power():
    """6dB is a doubling. Getting this wrong makes every duck half as deep as
    the config says."""
    assert db_to_linear(0.0) == pytest.approx(1.0)
    assert db_to_linear(-6.0) == pytest.approx(0.5, abs=0.01)
    assert db_to_linear(6.0) == pytest.approx(2.0, abs=0.02)


def test_db_round_trip():
    for db in (-40.0, -12.0, -6.0, 0.0):
        assert linear_to_db(db_to_linear(db)) == pytest.approx(db, abs=1e-6)


def test_ramp_step_reaches_depth_in_exactly_the_ramp_time():
    """The contract the duck attack test relies on: N steps of `ramp_step_db`
    cover `depth_db` in `ramp_ms`, exactly."""
    block_ms = 10.0
    step = ramp_step_db(-12.0, ramp_ms=80.0, block_ms=block_ms)
    assert step == pytest.approx(1.5)

    current = 0.0
    for _ in range(8):  # 8 blocks x 10ms = 80ms
        current = approach_db(current, -12.0, step)
    assert current == pytest.approx(-12.0)


def test_approach_never_overshoots():
    assert approach_db(0.0, -12.0, 100.0) == -12.0
    assert approach_db(-12.0, 0.0, 100.0) == 0.0


def test_gain_ramp_interpolates_across_the_block():
    """A single gain per block is a step discontinuity, and a step
    discontinuity is a click."""
    ramp = gain_ramp(64, 0.0, -6.0, channels=2)
    assert ramp.shape == (64, 2)
    assert ramp[0, 0] == pytest.approx(1.0, abs=1e-3)
    assert ramp[-1, 0] == pytest.approx(db_to_linear(-6.0), abs=1e-3)
    assert np.all(np.diff(ramp[:, 0]) < 0)


def test_gain_ramp_is_flat_when_unchanged():
    ramp = gain_ramp(32, -6.0, -6.0, channels=2)
    assert np.allclose(ramp, db_to_linear(-6.0), atol=1e-6)


# --- the limiter ------------------------------------------------------------


def test_soft_clip_is_transparent_below_the_knee():
    """The bug this replaced: a plain tanh took a full-scale item down 2.4dB for
    nothing, and every gain assertion downstream inherited the error."""
    block = np.full((16, 2), CLIP_KNEE - 0.01, dtype=np.float32)
    assert np.array_equal(soft_clip(block), block)


def test_soft_clip_leaves_quiet_material_untouched():
    block = np.full((16, 2), 0.5, dtype=np.float32)
    assert np.allclose(soft_clip(block), 0.5, atol=1e-7)


def test_soft_clip_bounds_a_hot_sum():
    """Three legal items summing to 2.7 must not come back as 2.7."""
    block = np.full((16, 2), 2.7, dtype=np.float32)
    out = soft_clip(block)
    # Full scale is legal; anything past it is not. The curve asymptotes to 1.0
    # and float32 rounds the last fraction of a dB away, so <= is the honest
    # bound here.
    assert float(np.max(out)) <= 1.0
    assert float(np.max(out)) > CLIP_KNEE


def test_soft_clip_preserves_sign_and_monotonicity():
    block = np.linspace(-3.0, 3.0, 128, dtype=np.float32)[:, None]
    out = soft_clip(np.repeat(block, 2, axis=1))
    assert np.all(np.sign(out) == np.sign(block))
    assert np.all(np.diff(out[:, 0]) >= 0)
    assert float(np.max(np.abs(out))) <= 1.0


def test_to_stereo_fans_out_mono():
    """Piper's output is mono and the device is stereo, so this runs on nearly
    every asset conduct produces."""
    mono = np.full((8, 1), 0.5, dtype=np.float32)
    out = to_stereo(mono, 2)
    assert out.shape == (8, 2)
    assert np.allclose(out, 0.5)


def test_to_stereo_passes_through_matching_width():
    stereo = np.zeros((8, 2), dtype=np.float32)
    assert to_stereo(stereo, 2).shape == (8, 2)


def test_to_stereo_handles_a_bare_1d_buffer():
    out = to_stereo(np.zeros(8, dtype=np.float32), 2)
    assert out.shape == (8, 2)
