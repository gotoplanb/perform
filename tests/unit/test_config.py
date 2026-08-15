"""Bus configuration and startup route validation.

SPEC §8: *"Validate at startup and fail loudly rather than falling back."* Every
test here is a config mistake that must stop the boot rather than surface later
as silence.
"""

from __future__ import annotations

import pytest

from config.buses import load_buses
from core.bus import Bus, DuckRule
from core.errors import ConfigError
from core.item import Media
from sinks.base import SinkCapabilities
from sinks.null import NullSink
from sinks.registry import SinkRegistry

GOOD = """
buses:
  - name: voice
    sink: local_audio
    gain_db: 0.0
    ducks:
      - target: ambience
        depth_db: -12.0
        attack_ms: 80
        release_ms: 500
  - name: ambience
    sink: local_audio
    gain_db: -6.0
    resting_open_ended: true
"""


def write(tmp_path, text, name="buses.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_a_good_config_loads(tmp_path):
    buses = load_buses(write(tmp_path, GOOD))
    assert [b.name for b in buses] == ["voice", "ambience"]
    assert buses[0].ducks[0].target == "ambience"
    assert buses[0].ducks[0].depth_db == -12.0
    assert buses[1].resting_open_ended is True


def test_a_missing_file_falls_back_to_the_built_in_pair(tmp_path):
    """Speaking has to work on a fresh clone with nothing configured — that is
    SPEC §6 step 1, and it has to work before anything else is worth building."""
    buses = load_buses(tmp_path / "absent.yaml")
    assert {b.name for b in buses} == {"voice", "ambience"}
    assert all(b.sink == "local_audio" for b in buses)


def test_invalid_yaml_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="invalid YAML"):
        load_buses(write(tmp_path, "buses: [unclosed"))


def test_an_empty_bus_list_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="non-empty list"):
        load_buses(write(tmp_path, "buses: []"))


def test_a_bus_without_a_sink_is_fatal(tmp_path):
    with pytest.raises(ConfigError, match="missing"):
        load_buses(write(tmp_path, "buses:\n  - name: voice\n"))


def test_duplicate_bus_names_are_fatal(tmp_path):
    text = (
        "buses:\n"
        "  - name: voice\n    sink: local_audio\n"
        "  - name: voice\n    sink: local_audio\n"
    )
    with pytest.raises(ConfigError, match="duplicate bus name"):
        load_buses(write(tmp_path, text))


def test_a_positive_duck_depth_is_fatal(tmp_path):
    """A "duck" that makes the target louder is a typo, every time."""
    text = GOOD.replace("depth_db: -12.0", "depth_db: 12.0")
    with pytest.raises(ConfigError, match="depth_db"):
        load_buses(write(tmp_path, text))


def test_a_zero_attack_is_fatal(tmp_path):
    text = GOOD.replace("attack_ms: 80", "attack_ms: 0")
    with pytest.raises(ConfigError, match="attack_ms"):
        load_buses(write(tmp_path, text))


# --- route validation -------------------------------------------------------


def registry():
    reg = SinkRegistry()
    reg.register(NullSink("local_audio"))
    return reg


def test_routes_validate_when_everything_exists():
    reg = registry()
    reg.validate_routes([Bus(name="voice", sink="local_audio")])


def test_a_bus_pointing_at_a_missing_sink_is_fatal():
    """Finding this out at play time means the failure surfaces as silence
    during a scene."""
    with pytest.raises(ConfigError, match="does not exist"):
        registry().validate_routes([Bus(name="voice", sink="speakers")])


def test_ducking_a_bus_that_does_not_exist_is_fatal():
    reg = registry()
    buses = [
        Bus(
            name="voice",
            sink="local_audio",
            ducks=[DuckRule(target="typo", depth_db=-12.0)],
        )
    ]
    with pytest.raises(ConfigError, match="not a bus"):
        reg.validate_routes(buses)


def test_a_bus_ducking_itself_is_fatal():
    reg = registry()
    buses = [
        Bus(
            name="voice",
            sink="local_audio",
            ducks=[DuckRule(target="voice", depth_db=-12.0)],
        )
    ]
    with pytest.raises(ConfigError, match="ducks itself"):
        reg.validate_routes(buses)


def test_every_problem_is_listed_at_once():
    """One boot, one list. Fixing them one restart at a time is miserable."""
    reg = registry()
    buses = [
        Bus(name="a", sink="nope"),
        Bus(name="b", sink="local_audio", ducks=[DuckRule(target="ghost", depth_db=-6.0)]),
    ]
    with pytest.raises(ConfigError) as excinfo:
        reg.validate_routes(buses)
    message = str(excinfo.value)
    assert "nope" in message
    assert "ghost" in message


def test_check_item_refuses_media_the_sink_does_not_accept():
    from core.errors import SinkRejected

    reg = SinkRegistry()
    sink = NullSink("audio_only")
    sink.capabilities = SinkCapabilities(
        accepts=frozenset({Media.AUDIO}), push=True, reports_completion=True
    )
    reg.register(sink)

    reg.check_item("audio_only", Media.AUDIO)
    with pytest.raises(SinkRejected, match="does not accept"):
        reg.check_item("audio_only", Media.VIDEO)


def test_an_unknown_sink_lists_what_is_registered():
    with pytest.raises(ConfigError, match="local_audio"):
        registry().get("typo")
