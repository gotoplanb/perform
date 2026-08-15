"""Load and validate the bus configuration.

YAML rather than TOML because this is a tabular, hand-edited operational file
and it hot-reloads — the same reasoning that put conduct's pricing in
`config/pricing.yaml` while its algorithm specs stayed TOML. Packs, which are
authored content shipped alongside media, are TOML like wander's specs.

Every failure raises `ConfigError` and aborts the boot. There is no partial
load and no default-on-error: a bus set that half-applied would mean a scene
where some lanes route and some don't, and the symptom is silence.
"""

from __future__ import annotations

import logging
from pathlib import Path

import yaml

from core.bus import Bus, DuckRule
from core.errors import ConfigError

log = logging.getLogger(__name__)

# The shipped set, used when no config file exists. Enough to make `POST /speak`
# work on a fresh clone with nothing configured — which is SPEC §6 step 1, and
# the thing that has to work before anything else is worth building.
DEFAULT_BUSES = [
    {"name": "voice", "sink": "local_audio", "gain_db": 0.0, "resting_open_ended": False},
    {"name": "ambience", "sink": "local_audio", "gain_db": -6.0, "resting_open_ended": True},
]


def load_buses(path: Path | str) -> list[Bus]:
    path = Path(path)
    if not path.is_file():
        log.warning(
            "no bus config at %s — falling back to the built-in voice/ambience pair. "
            "Copy config/buses.yaml to customize.",
            path,
        )
        return [_build(entry, "<default>") for entry in DEFAULT_BUSES]

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path.name}: invalid YAML: {e}") from e

    entries = data.get("buses")
    if not isinstance(entries, list) or not entries:
        raise ConfigError(f"{path.name}: 'buses' must be a non-empty list")

    buses = [_build(entry, path.name) for entry in entries]

    names = [b.name for b in buses]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise ConfigError(f"{path.name}: duplicate bus name(s): {sorted(dupes)}")

    log.info(
        "loaded %d bus(es) from %s: %s",
        len(buses),
        path.name,
        ", ".join(f"{b.name}->{b.sink}" for b in buses),
    )
    return buses


def _build(entry: dict, source: str) -> Bus:
    if not isinstance(entry, dict):
        raise ConfigError(f"{source}: each bus must be a mapping, got {type(entry).__name__}")

    missing = {"name", "sink"} - entry.keys()
    if missing:
        raise ConfigError(f"{source}: bus entry missing {sorted(missing)}")

    ducks = []
    for rule in entry.get("ducks", []) or []:
        rule_missing = {"target", "depth_db"} - rule.keys()
        if rule_missing:
            raise ConfigError(
                f"{source}: duck rule on bus {entry['name']!r} missing {sorted(rule_missing)}"
            )
        try:
            ducks.append(
                DuckRule(
                    target=rule["target"],
                    depth_db=float(rule["depth_db"]),
                    attack_ms=float(rule.get("attack_ms", 80.0)),
                    release_ms=float(rule.get("release_ms", 400.0)),
                )
            )
        except ValueError as e:
            raise ConfigError(f"{source}: bus {entry['name']!r}: {e}") from e

    return Bus(
        name=str(entry["name"]),
        sink=str(entry["sink"]),
        gain_db=float(entry.get("gain_db", 0.0)),
        resting_open_ended=bool(entry.get("resting_open_ended", False)),
        ducks=ducks,
    )
