"""The item vocabulary, and the invariants that keep the model honest."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from core.item import AssetRef, Duration, DurationKind, Item


def test_asset_ref_needs_exactly_one_of_key_or_path():
    """Both would be ambiguous; neither is nothing to play."""
    AssetRef(key="tone.ok")
    AssetRef(path=Path("/tmp/x.wav"))

    with pytest.raises(ValueError, match="exactly one"):
        AssetRef()
    with pytest.raises(ValueError, match="exactly one"):
        AssetRef(key="tone.ok", path=Path("/tmp/x.wav"))


def test_repeat_needs_a_count():
    with pytest.raises(ValueError, match="repeat"):
        Duration(kind=DurationKind.REPEAT)
    with pytest.raises(ValueError, match="repeat"):
        Duration(kind=DurationKind.REPEAT, repeat=0)
    Duration(kind=DurationKind.REPEAT, repeat=1)


def test_declared_needs_a_positive_interval():
    with pytest.raises(ValueError, match="seconds"):
        Duration(kind=DurationKind.DECLARED)
    with pytest.raises(ValueError, match="seconds"):
        Duration(kind=DurationKind.DECLARED, seconds=0)
    Duration(kind=DurationKind.DECLARED, seconds=2.5)


def test_only_until_replaced_is_open_ended():
    """The property the stall detector turns on."""
    assert Duration(kind=DurationKind.UNTIL_REPLACED).open_ended is True
    assert Duration(kind=DurationKind.INTRINSIC).open_ended is False
    assert Duration(kind=DurationKind.REPEAT, repeat=3).open_ended is False
    assert Duration(kind=DurationKind.DECLARED, seconds=1).open_ended is False


def test_item_ids_are_unique():
    ids = {Item(bus="voice", asset=AssetRef(key="tone.ok")).id for _ in range(200)}
    assert len(ids) == 200


def test_replacement_keeps_the_id_and_takes_everything_else():
    original = Item(bus="voice", asset=AssetRef(key="placeholder"), label="slot 3")
    replacement = Item(
        bus="voice",
        asset=AssetRef(key="tone.ok"),
        label="the real line",
        duration=Duration(kind=DurationKind.REPEAT, repeat=2),
    )

    merged = original.replaced_by(replacement)

    assert merged.id == original.id
    assert str(merged.asset) == "tone.ok"
    assert merged.label == "the real line"
    assert merged.duration.repeat == 2


def test_a_replacement_without_a_label_inherits_the_old_one():
    """So swapping a placeholder for the real asset does not lose the trace
    correlation Wander set up when it placed the slot."""
    original = Item(bus="voice", asset=AssetRef(key="placeholder"), label="sre line 3")
    merged = original.replaced_by(Item(bus="voice", asset=AssetRef(key="tone.ok")))
    assert merged.label == "sre line 3"


def test_items_are_frozen():
    """The engine holds one, the mixer holds a reference to the same one, and
    /status serializes it — nobody gets to mutate it out from under the others."""
    it = Item(bus="voice", asset=AssetRef(key="tone.ok"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        it.label = "mutated"  # type: ignore[misc]
