"""Mixer arithmetic, asserted sample-for-sample against NullDevice.

No audio device is involved and no sound is made. The mix graph is the real one
— same render callback, same command deque, same gain ramps — with the output
going into a numpy array instead of into CoreAudio.
"""

from __future__ import annotations

import numpy as np

from audio.dsp import db_to_linear
from audio.mixer import Configure, SetGain, Start, Stop, StopAll
from core.bus import DuckRule
from core.item import Transition
from tests.conftest import BLOCK, CHANNELS, SAMPLE_RATE, dc, item


def configure(mixer, *, gains=None, ducks=None):
    mixer.submit(
        Configure(gains=gains or {"voice": 0.0, "ambience": 0.0}, ducks=ducks or {})
    )


def test_silence_when_nothing_is_playing(mixer, device):
    configure(mixer)
    out = device.pull(4)
    assert out.shape == (4 * BLOCK, CHANNELS)
    assert np.all(out == 0.0)


def test_two_buses_sum(mixer, device):
    configure(mixer)
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.2, 1.0))))
    mixer.submit(
        Start(bus="ambience", source=mixer.make_source(item(bus="ambience"), dc(0.3, 1.0)))
    )

    # Skip the first block: the 5ms declick fade-in is still running there, and
    # asserting through a ramp would be asserting the ramp, not the sum.
    device.pull(1)
    out = device.pull(1)
    assert np.allclose(out, 0.5, atol=1e-4)


def test_gain_is_applied_in_db(mixer, device):
    configure(mixer, gains={"voice": -6.0})
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(1.0, 1.0))))
    device.pull(1)
    out = device.pull(1)
    assert np.allclose(out, db_to_linear(-6.0), atol=1e-3)


def test_set_gain_takes_effect(mixer, device):
    configure(mixer, gains={"voice": 0.0})
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.5, 2.0))))
    device.pull(2)
    mixer.submit(SetGain(bus="voice", gain_db=-12.0))
    device.pull(2)
    out = device.pull(1)
    assert np.allclose(out, 0.5 * db_to_linear(-12.0), atol=1e-3)


def test_source_finishes_and_reports_completion(mixer, device):
    configure(mixer)
    it = item(label="one line")
    mixer.submit(Start(bus="voice", source=mixer.make_source(it, dc(0.5, 0.05))))

    device.pull_seconds(0.2)
    completions = mixer.drain_completions()

    assert len(completions) == 1
    assert completions[0].item_id == it.id
    assert completions[0].natural is True
    assert completions[0].cycles == 1


def test_repeat_plays_exactly_n_cycles(mixer, device):
    """SPEC §3's 'one cycle times N'. Exactly N — not N-ish, and not forever."""
    configure(mixer)
    from core.item import DurationKind

    it = item(kind=DurationKind.REPEAT, repeat=3)
    mixer.submit(Start(bus="voice", source=mixer.make_source(it, dc(0.5, 0.05))))

    device.pull_seconds(0.5)
    completions = mixer.drain_completions()

    assert len(completions) == 1
    assert completions[0].cycles == 3
    assert completions[0].natural is True
    # Three 50ms cycles, give or take the block granularity.
    assert 0.14 < completions[0].elapsed_s < 0.16


def test_until_replaced_loops_forever(mixer, device):
    configure(mixer)
    from core.item import DurationKind

    it = item(kind=DurationKind.UNTIL_REPLACED)
    mixer.submit(Start(bus="voice", source=mixer.make_source(it, dc(0.5, 0.05))))

    # Twenty times the buffer length. Nothing should have ended.
    device.pull_seconds(1.0)
    assert mixer.drain_completions() == []

    out = device.pull(1)
    assert np.allclose(out, 0.5, atol=1e-3)


def test_declared_duration_stops_on_budget(mixer, device):
    """A still held for a stated interval — the media-agnostic case. Here the
    'still' is a 50ms loop held for 300ms."""
    configure(mixer)
    from core.item import DurationKind

    it = item(kind=DurationKind.DECLARED, seconds=0.3)
    mixer.submit(Start(bus="voice", source=mixer.make_source(it, dc(0.5, 0.05))))

    device.pull_seconds(0.6)
    completions = mixer.drain_completions()

    assert len(completions) == 1
    assert 0.29 < completions[0].elapsed_s < 0.32


def test_stop_silences_the_bus_quickly(mixer, device):
    """SPEC §3: being unable to shut it up is the most common reason tools like
    this get abandoned. 'Quickly' here means within the declick fade."""
    configure(mixer)
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.5, 5.0))))
    device.pull(4)

    mixer.submit(Stop(bus="voice"))
    device.pull_seconds(0.05)  # well past the 5ms declick

    out = device.pull(2)
    assert np.allclose(out, 0.0, atol=1e-6)


def test_stop_is_a_fade_not_a_cliff(mixer, device):
    """A hard mute mid-waveform is a click, and a click on every stop trains
    people not to use stop."""
    configure(mixer)
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(1.0, 5.0))))
    device.pull(4)

    mixer.submit(StopAll())
    block = device.pull(1)[:, 0]

    # Monotonically down, and it actually gets there.
    assert block[0] > 0.5
    assert block[-1] < 0.05
    assert np.all(np.diff(block) <= 1e-6)


def test_stop_all_reaches_every_bus(mixer, device):
    configure(mixer)
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.4, 5.0))))
    mixer.submit(
        Start(bus="ambience", source=mixer.make_source(item(bus="ambience"), dc(0.4, 5.0)))
    )
    device.pull(4)

    mixer.submit(StopAll())
    device.pull_seconds(0.05)

    assert np.allclose(device.pull(2), 0.0, atol=1e-6)


# --- ducking ----------------------------------------------------------------


DUCK = DuckRule(target="ambience", depth_db=-12.0, attack_ms=80.0, release_ms=400.0)

# Ducking tests run the bed at -6dBFS rather than full scale, so the assertions
# are about the duck and not about the limiter. A bed sitting at 1.0 would be
# inside the soft-clip knee, and then every gain number here would be measuring
# two things at once.
BED = 0.5


def start_ambience(mixer, level=BED, seconds=10.0):
    mixer.submit(
        Start(bus="ambience", source=mixer.make_source(item(bus="ambience"), dc(level, seconds)))
    )


def test_duck_reaches_exactly_its_depth(mixer, device):
    """The one audio control SPEC §3 keeps. It should land on the configured
    depth, not near it."""
    configure(mixer, gains={"voice": 0.0, "ambience": 0.0}, ducks={"voice": [DUCK]})
    start_ambience(mixer)
    device.pull_seconds(0.2)  # let ambience settle at unity

    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.0, 10.0))))
    # Attack is 80ms; pull well past it.
    device.pull_seconds(0.4)

    out = device.pull(1)
    assert np.allclose(out, BED * db_to_linear(-12.0), atol=1e-3)


def test_duck_attack_takes_the_configured_time(mixer, device):
    """Exact by construction — ramp_step_db is defined so that `depth` is
    reached in `attack_ms`, and this asserts the definition holds end to end."""
    configure(mixer, gains={"voice": 0.0, "ambience": 0.0}, ducks={"voice": [DUCK]})
    start_ambience(mixer)
    device.pull_seconds(0.2)

    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.0, 10.0))))

    block_ms = 1000.0 * BLOCK / SAMPLE_RATE
    blocks_to_attack = int(DUCK.attack_ms / block_ms)

    # Halfway through the attack it should be roughly halfway down in dB, not
    # already at the bottom (which a linear-amplitude ramp would give).
    device.pull(blocks_to_attack // 2)
    mid = float(np.mean(device.pull(1)))
    assert BED * db_to_linear(-9.0) < mid < BED * db_to_linear(-3.0)

    device.pull(blocks_to_attack)
    assert np.allclose(device.pull(1), BED * db_to_linear(-12.0), atol=1e-3)


def test_duck_releases_when_the_voice_stops(mixer, device):
    configure(mixer, gains={"voice": 0.0, "ambience": 0.0}, ducks={"voice": [DUCK]})
    start_ambience(mixer)

    # A 300ms line: long enough to outlast the 80ms attack, so the ducked state
    # is observable before the item ends.
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.0, 0.3))))
    device.pull_seconds(0.15)
    assert np.allclose(device.pull(1), BED * db_to_linear(-12.0), atol=1e-3)

    # The line has now ended and the 400ms release has run out.
    device.pull_seconds(1.0)
    assert np.allclose(device.pull(1), BED, atol=1e-3)


def test_overlapping_ducks_take_the_deepest_not_the_sum(mixer, device):
    """Two -12dB ducks meaning -24dB would be a surprise every time."""
    shallow = DuckRule(target="ambience", depth_db=-6.0, attack_ms=80, release_ms=400)
    deep = DuckRule(target="ambience", depth_db=-12.0, attack_ms=80, release_ms=400)
    mixer.submit(
        Configure(
            gains={"voice": 0.0, "music": 0.0, "ambience": 0.0},
            ducks={"voice": [deep], "music": [shallow]},
        )
    )
    start_ambience(mixer)
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(0.0, 10.0))))
    mixer.submit(
        Start(bus="music", source=mixer.make_source(item(bus="music"), dc(0.0, 10.0)))
    )

    device.pull_seconds(0.5)
    assert np.allclose(device.pull(1), BED * db_to_linear(-12.0), atol=1e-3)


def test_a_bus_never_ducks_itself(mixer, device):
    """Guarded in config validation too, but the mixer must not depend on that."""
    self_duck = DuckRule(target="voice", depth_db=-12.0)
    configure(mixer, gains={"voice": 0.0}, ducks={"voice": [self_duck]})
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(BED, 5.0))))

    device.pull_seconds(0.5)
    assert np.allclose(device.pull(1), BED, atol=1e-3)


# --- transitions ------------------------------------------------------------


def test_dissolve_overlaps_the_two_sources(mixer, device):
    """A crossfade: both are audible during the ramp, and the sum stays sane."""
    configure(mixer, gains={"voice": 0.0})
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(1.0, 5.0))))
    device.pull_seconds(0.1)

    mixer.submit(
        Start(
            bus="voice",
            source=mixer.make_source(item(label="second"), dc(1.0, 5.0)),
            transition=Transition.DISSOLVE,
            fade_ms=200.0,
        )
    )

    mid = device.pull_seconds(0.1)
    # Both playing at partial gain. Never silent, never far above either one
    # alone (soft_clip catches the peak of the crossfade).
    assert float(np.min(np.abs(mid))) > 0.5
    assert float(np.max(np.abs(mid))) <= 1.05


def test_fade_is_sequential_not_overlapping(mixer, device):
    """The difference from dissolve: fade lets the old one finish before the new
    one starts, which matters when overlapping two music-like items is muddy."""
    configure(mixer, gains={"voice": 0.0})
    mixer.submit(Start(bus="voice", source=mixer.make_source(item(), dc(BED, 5.0))))
    device.pull_seconds(0.1)

    mixer.submit(
        Start(
            bus="voice",
            source=mixer.make_source(item(label="second"), dc(BED, 5.0)),
            transition=Transition.FADE,
            fade_ms=100.0,
        )
    )

    # Through the fade-out the level falls toward zero...
    device.pull_seconds(0.1)
    trough = float(np.max(np.abs(device.pull(1))))
    assert trough < BED * 0.7

    # ...then the second item fades up on its own.
    device.pull_seconds(0.3)
    assert np.allclose(device.pull(1), BED, atol=1e-2)


def test_soft_clip_keeps_the_sum_in_range(mixer, device):
    """Buses sum, so three legal items can still exceed full scale together."""
    mixer.submit(Configure(gains={"a": 0.0, "b": 0.0, "c": 0.0}, ducks={}))
    for bus in ("a", "b", "c"):
        mixer.submit(
            Start(bus=bus, source=mixer.make_source(item(bus=bus), dc(0.9, 2.0)))
        )
    device.pull(2)
    out = device.pull(2)
    assert float(np.max(np.abs(out))) <= 1.0
