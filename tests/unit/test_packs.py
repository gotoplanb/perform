"""Pack manifest validation.

Follows the shape of wander's spec loader tests in spirit: every rule the loader
enforces gets a malformed manifest proving it fires. These are cheap and they
are the difference between "a pack loaded" and "a pack loaded correctly", which
at play time is the difference between a scene and four fifths of a scene.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from library.packs import CURRENT_SCHEMA_VERSION, discover, load_pack


def write_pack(root: Path, pack_id: str, manifest: str, files=("greeting.wav",)) -> Path:
    folder = root / pack_id
    folder.mkdir(parents=True, exist_ok=True)
    for name in files:
        (folder / name).write_bytes(b"RIFF....WAVEfmt ")
    (folder / "pack.toml").write_text(manifest)
    return folder


GOOD = f"""
schema_version = {CURRENT_SCHEMA_VERSION}
id = "ops-bridge"

[assets."sre.greeting"]
file = "greeting.wav"
media = "audio"
"""


def test_a_good_pack_loads(tmp_path):
    pack = load_pack(write_pack(tmp_path, "ops-bridge", GOOD))
    assert pack.id == "ops-bridge"
    assert [a.name for a in pack.assets] == ["ops-bridge.sre.greeting"]
    assert pack.assets[0].media == "audio"


def test_names_are_namespaced_by_pack(tmp_path):
    """Namespaced to avoid collisions — two packs may both declare 'greeting'."""
    pack = load_pack(write_pack(tmp_path, "ops-bridge", GOOD))
    assert pack.assets[0].name.startswith("ops-bridge.")


def test_media_is_required_and_never_inferred(tmp_path):
    """SPEC §5 is blunt: retrofitting a media type into a key scheme that
    assumed audio is miserable. So it is required from schema_version 1."""
    manifest = GOOD.replace('media = "audio"\n', "")
    with pytest.raises(ValueError, match="media"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_unknown_media_is_refused(tmp_path):
    manifest = GOOD.replace('media = "audio"', 'media = "hologram"')
    with pytest.raises(ValueError, match="hologram"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_schema_version_must_match_exactly(tmp_path):
    """Equality, not compatibility — the same call wander's spec loader makes."""
    manifest = GOOD.replace(f"schema_version = {CURRENT_SCHEMA_VERSION}", "schema_version = 2")
    with pytest.raises(ValueError, match="schema_version"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_folder_name_must_match_id(tmp_path):
    with pytest.raises(ValueError, match="folder name must match id"):
        load_pack(write_pack(tmp_path, "something-else", GOOD))


def test_missing_file_is_a_startup_error(tmp_path):
    manifest = GOOD.replace('file = "greeting.wav"', 'file = "absent.wav"')
    with pytest.raises(ValueError, match="not found"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_a_file_outside_the_pack_is_refused(tmp_path):
    """A manifest is user-supplied content, and `file = "../../etc/passwd"`
    should be a startup error rather than a served file."""
    manifest = GOOD.replace('file = "greeting.wav"', 'file = "../../etc/passwd"')
    with pytest.raises(ValueError, match="escapes the pack directory"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_positional_keys_are_refused(tmp_path):
    """SPEC §5: keys are about the asset, not its position in a scene.
    `sre.greeting` survives a rename; `scene3.turn1.sre` orphans the asset and
    buries a scene format in a naming convention."""
    manifest = GOOD.replace('"sre.greeting"', '"scene3.turn1.sre"')
    with pytest.raises(ValueError, match="names a position in a scene"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


@pytest.mark.parametrize(
    "key", ["act2.line4", "step_7.intro", "chapter1.open", "beat3.hit"]
)
def test_other_positional_shapes_are_refused(tmp_path, key):
    manifest = GOOD.replace('"sre.greeting"', f'"{key}"')
    with pytest.raises(ValueError, match="names a position"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_a_key_that_merely_contains_a_number_is_fine(tmp_path):
    """`tier2.escalation` is about the asset. The rule must not be so eager that
    it rejects legitimate names."""
    manifest = GOOD.replace('"sre.greeting"', '"tier2.escalation"')
    pack = load_pack(write_pack(tmp_path, "ops-bridge", manifest))
    assert pack.assets[0].name == "ops-bridge.tier2.escalation"


def test_uppercase_and_spaces_are_refused(tmp_path):
    manifest = GOOD.replace('"sre.greeting"', '"SRE Greeting"')
    with pytest.raises(ValueError, match="lowercase dot-separated"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_aliases_resolve_to_the_same_file(tmp_path):
    """SPEC §7 flags aliasing as cheap now and annoying to retrofit: once scenes
    reference assets by name, renaming breaks saved scenes."""
    manifest = GOOD + '\naliases = ["sre.hello", "sre.hi"]\n'
    pack = load_pack(write_pack(tmp_path, "ops-bridge", manifest))

    names = {a.name: a for a in pack.assets}
    assert set(names) == {
        "ops-bridge.sre.greeting",
        "ops-bridge.sre.hello",
        "ops-bridge.sre.hi",
    }
    assert names["ops-bridge.sre.hello"].alias_of == "ops-bridge.sre.greeting"
    assert names["ops-bridge.sre.hello"].path == names["ops-bridge.sre.greeting"].path


def test_an_alias_colliding_with_a_real_name_is_refused(tmp_path):
    manifest = f"""
schema_version = {CURRENT_SCHEMA_VERSION}
id = "ops-bridge"

[assets."sre.greeting"]
file = "greeting.wav"
media = "audio"

[assets."sre.farewell"]
file = "greeting.wav"
media = "audio"
aliases = ["sre.greeting"]
"""
    with pytest.raises(ValueError, match="duplicate asset name"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


def test_invalid_toml_names_the_file(tmp_path):
    with pytest.raises(ValueError, match="invalid TOML"):
        load_pack(write_pack(tmp_path, "ops-bridge", "schema_version = [unclosed"))


def test_empty_assets_table_is_refused(tmp_path):
    manifest = f'schema_version = {CURRENT_SCHEMA_VERSION}\nid = "ops-bridge"\nassets = {{}}\n'
    with pytest.raises(ValueError, match="non-empty table"):
        load_pack(write_pack(tmp_path, "ops-bridge", manifest))


# --- discovery --------------------------------------------------------------


def test_discover_finds_every_pack(tmp_path):
    write_pack(tmp_path, "ops-bridge", GOOD)
    write_pack(tmp_path, "weather", GOOD.replace("ops-bridge", "weather"))
    packs = discover(tmp_path)
    assert [p.id for p in packs] == ["ops-bridge", "weather"]


def test_discover_on_a_missing_directory_is_not_an_error(tmp_path):
    """A machine with no packs installed still has the built-in tones, which is
    enough for the notification case."""
    assert discover(tmp_path / "nope") == []


def test_a_folder_without_a_manifest_is_skipped_not_fatal(tmp_path):
    write_pack(tmp_path, "ops-bridge", GOOD)
    (tmp_path / "just-a-folder").mkdir()
    assert [p.id for p in discover(tmp_path)] == ["ops-bridge"]


def test_a_broken_pack_aborts_the_scan(tmp_path):
    """Skipping would mean booting with a partially-loaded library, and the
    first symptom would be a scene that plays four of its five lines."""
    write_pack(tmp_path, "ops-bridge", GOOD)
    write_pack(tmp_path, "broken", GOOD.replace("ops-bridge", "broken").replace(
        'media = "audio"', 'media = "nope"'
    ))
    with pytest.raises(ValueError):
        discover(tmp_path)
