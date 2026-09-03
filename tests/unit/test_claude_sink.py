"""The Claude client sink — pull, and the odd one out.

SPEC §4: media can only arrive as the result of a tool call and nothing reports
back, so there is no completion signal and there cannot be one. Most of what is
worth testing here is that the constraints are honored rather than quietly
worked around: the payload ceiling, the buffer policy, and the permanent
`reports_completion = False`.

Like the visual sink, this is tested directly rather than through the app: it
has no HTTP surface at all — the MCP `next_visual` tool is its only reader.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from core.item import AssetRef, Duration, DurationKind, Item, Media
from library.library import ResolvedVisual
from sinks.claude import BUFFER_DEPTH, MAX_PAYLOAD_BYTES, ClaudeSink


class FakeLibrary:
    """Stands in for the library so these need no media and no ffmpeg."""

    def __init__(self, path: Path, media: Media = Media.IMAGE) -> None:
        self.path = path
        self.media = media
        self.warnings: list[str] = []

    def resolve_visual(self, ref) -> ResolvedVisual:
        return ResolvedVisual(
            key="deadbeef",
            name=str(ref),
            media=self.media,
            path=self.path,
            url_path=f"/media/deadbeef{self.path.suffix}",
            audio_stripped=False,
            warnings=(),
        )


@pytest.fixture
def png(tmp_path) -> Path:
    path = tmp_path / "still.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"pixels")
    return path


@pytest.fixture
def sink(png) -> ClaudeSink:
    return ClaudeSink(FakeLibrary(png))


def item(label: str = "", media: Media = Media.IMAGE, **duration) -> Item:
    kind = duration.pop("kind", DurationKind.UNTIL_REPLACED)
    return Item(
        bus="aside",
        asset=AssetRef(key="scene.bridge"),
        media=media,
        duration=Duration(kind=kind, **duration),
        label=label,
    )


# --- capabilities -------------------------------------------------------


def test_it_never_reports_completion_and_that_is_permanent(sink):
    """Not a gap to be closed later. There is no mechanism by which a chat
    client could tell us a picture finished being looked at."""
    assert sink.capabilities.reports_completion is False


def test_it_is_a_pull_sink(sink):
    assert sink.capabilities.push is False


def test_it_accepts_visual_media_only(sink):
    assert sink.capabilities.accepts_media(Media.IMAGE)
    assert not sink.capabilities.accepts_media(Media.AUDIO)


# --- buffering ----------------------------------------------------------


async def test_a_started_item_is_buffered_not_sent(sink):
    dispatch = await sink.start(item(label="the bridge"))
    assert dispatch.accepted
    assert sink.status()["buffered"] == 1


async def test_take_drains_and_base64_encodes(sink, png):
    await sink.start(item(label="the bridge"))
    taken = sink.take()

    assert len(taken) == 1
    assert taken[0]["label"] == "the bridge"
    assert base64.b64decode(taken[0]["data"]) == png.read_bytes()
    assert taken[0]["mime"] == "image/png"
    # Drained, not copied.
    assert sink.status()["buffered"] == 0


async def test_take_respects_its_limit(sink):
    for i in range(3):
        await sink.start(item(label=f"item-{i}"))
    assert len(sink.take(limit=2)) == 2
    assert sink.status()["buffered"] == 1


async def test_take_on_an_empty_buffer_returns_nothing(sink):
    assert sink.take() == []


async def test_duration_rides_along_as_advisory(sink):
    """SPEC §4: passed through for the client to honor, never enforced here,
    because the sequencing unit is the conversational turn and not the clock."""
    await sink.start(item(kind=DurationKind.REPEAT, repeat=3))
    assert sink.take()[0]["duration"] == {"kind": "repeat", "repeat": 3, "seconds": None}


async def test_the_buffer_drops_the_oldest_when_full(sink):
    """In a conversation the newest image is almost always the relevant one, and
    a backlog from four turns ago is noise."""
    for i in range(BUFFER_DEPTH + 3):
        await sink.start(item(label=f"item-{i}"))

    assert sink.status()["buffered"] == BUFFER_DEPTH
    assert sink.dropped == 3
    labels = [entry["label"] for entry in sink.take(limit=BUFFER_DEPTH)]
    assert labels[-1] == f"item-{BUFFER_DEPTH + 2}"
    assert "item-0" not in labels


async def test_a_full_buffer_warns_about_why(sink):
    for _ in range(BUFFER_DEPTH):
        await sink.start(item())
    dispatch = await sink.start(item())
    assert any("buffer full" in w for w in dispatch.warnings)


# --- the payload ceiling ------------------------------------------------


async def test_an_oversized_payload_is_refused_rather_than_truncated(tmp_path):
    """A silently-truncated image is worse than a refused one."""
    big = tmp_path / "huge.png"
    big.write_bytes(b"\x00" * (MAX_PAYLOAD_BYTES + 1))
    sink = ClaudeSink(FakeLibrary(big))

    dispatch = await sink.start(item(label="too big"))

    assert dispatch.accepted is False
    assert "payload limit" in dispatch.detail
    assert sink.status()["buffered"] == 0


async def test_a_payload_at_the_limit_is_accepted(tmp_path):
    """An off-by-one at the boundary would refuse legitimate images."""
    exact = tmp_path / "exact.png"
    exact.write_bytes(b"\x00" * MAX_PAYLOAD_BYTES)
    sink = ClaudeSink(FakeLibrary(exact))
    assert (await sink.start(item())).accepted is True


async def test_video_is_accepted_but_warned_about(tmp_path):
    """SPEC §4: images work far better than video. Accepted so a caller is not
    blocked, warned so nobody wonders why it did not render."""
    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"\x00" * 64)
    sink = ClaudeSink(FakeLibrary(clip, media=Media.VIDEO))

    dispatch = await sink.start(item(label="a clip", media=Media.VIDEO))

    assert dispatch.accepted is True
    assert any("images work far better" in w for w in dispatch.warnings)


# --- stop ---------------------------------------------------------------


async def test_stop_clears_the_whole_buffer(sink):
    for _ in range(3):
        await sink.start(item())
    await sink.stop()
    assert sink.status()["buffered"] == 0


async def test_stop_with_an_id_removes_only_that_item(sink):
    first = item(label="keep-me")
    second = item(label="drop-me")
    await sink.start(first)
    await sink.start(second)

    await sink.stop(second.id)

    assert [e["label"] for e in sink.take(limit=5)] == ["keep-me"]


async def test_stop_bus_clears_it(sink):
    await sink.start(item())
    await sink.stop_bus("aside")
    assert sink.status()["buffered"] == 0


# --- reporting ----------------------------------------------------------


async def test_status_says_it_is_a_pull_sink(sink):
    """`/status` is where someone asks why nothing appeared. 'Nothing pulled
    yet' and 'nothing was placed' look identical without this."""
    assert "pull sink" in sink.status()["note"]


async def test_an_unreadable_file_is_skipped_rather_than_raising(sink, png):
    """The file is content-addressed under blobs/, so it going missing means the
    cache was cleared mid-conversation — worth surviving, not worth crashing a
    tool call over."""
    await sink.start(item(label="vanishing"))
    png.unlink()
    assert sink.take() == []
