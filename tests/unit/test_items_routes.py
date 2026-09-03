"""The item routes' validation and error mapping.

The happy paths are covered end to end in tests/integration/test_playback.py.
What was missing is the mapping layer: which domain error becomes which status,
and the request-model validation that fires before the engine is ever touched.
Getting that wrong turns a caller's typo into a 500.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException, status

from core.errors import (
    AssetError,
    ItemAlreadyPlaying,
    PerformError,
    SinkRejected,
    UnknownAsset,
    UnknownBus,
    UnknownItem,
)
from core.item import DurationKind, Media
from routes.items import DurationIn, ItemIn, as_http

# --- request validation -------------------------------------------------


def test_a_repeat_without_a_count_is_422_not_500():
    """Duration's invariants are enforced in the dataclass; the route has to
    turn that ValueError into a caller-facing status."""
    with pytest.raises(HTTPException) as excinfo:
        DurationIn(kind=DurationKind.REPEAT).build()
    assert excinfo.value.status_code == status.HTTP_422_UNPROCESSABLE_CONTENT
    assert "repeat" in excinfo.value.detail


def test_a_declared_duration_without_seconds_is_422():
    with pytest.raises(HTTPException) as excinfo:
        DurationIn(kind=DurationKind.DECLARED).build()
    assert excinfo.value.status_code == 422
    assert "seconds" in excinfo.value.detail


def test_a_valid_duration_builds():
    duration = DurationIn(kind=DurationKind.REPEAT, repeat=3).build()
    assert duration.repeat == 3
    assert DurationIn().build().kind is DurationKind.INTRINSIC


def test_supplying_both_asset_and_path_is_refused():
    with pytest.raises(HTTPException) as excinfo:
        ItemIn(asset="tone.ok", path="/tmp/x.wav").build("voice")
    assert excinfo.value.status_code == 422
    assert "exactly one" in excinfo.value.detail


def test_supplying_neither_is_refused():
    with pytest.raises(HTTPException) as excinfo:
        ItemIn().build("voice")
    assert excinfo.value.status_code == 422


def test_an_asset_name_builds_a_keyed_ref():
    item = ItemIn(asset="tone.ok", label="a line").build("voice")
    assert item.asset.key == "tone.ok"
    assert item.asset.path is None
    assert item.bus == "voice"
    assert item.label == "a line"


def test_a_path_builds_a_path_ref():
    """How Wander hands over something conduct just generated."""
    item = ItemIn(path="/Users/dave/conduct/output/abc.mp3").build("voice")
    assert item.asset.path is not None
    assert item.asset.key is None


def test_defaults_are_the_conservative_ones():
    item = ItemIn(asset="tone.ok").build("voice")
    assert item.media is Media.AUDIO
    assert item.treatment.value == "clean"
    assert item.transition.value == "cut"
    assert item.duration.kind is DurationKind.INTRINSIC


# --- error mapping ------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (UnknownBus("no such bus"), 404),
        (UnknownItem("no such item"), 404),
        (ItemAlreadyPlaying("too late"), 409),
        (SinkRejected("wrong media"), 409),
        (AssetError("cannot read"), 422),
        (UnknownAsset("no such name"), 422),  # an AssetError subclass
        (PerformError("something else"), 500),
    ],
)
def test_domain_errors_map_to_the_right_status(error, expected):
    """One place that knows the mapping, the way conduct's route layer converts
    ProviderError -> 502 and SensitivityViolation -> 400."""
    assert as_http(error).status_code == expected


def test_the_mapped_error_keeps_the_original_message():
    """The message is the remediation — dropping it for a bare status turns a
    fixable mistake into a guess."""
    detail = as_http(UnknownBus("no bus named 'nope' — configured buses are ['voice']"))
    assert "configured buses" in detail.detail
