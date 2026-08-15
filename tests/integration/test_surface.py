"""The visual surface, through the real app — everything except the SSE stream.

## What is and is not covered here

`GET /surface/events` is deliberately absent. httpx's `ASGITransport` buffers a
response body to completion before returning it, so an endless event generator
yields nothing and the request hangs — a limitation of in-process ASGI testing,
not of the route. The sink's pub/sub, event shape, replay, backpressure, and
completion routing are all covered in `tests/unit/test_visual_sink.py`, and
`routes/surface.py` is a thin serializer over exactly those calls. The wire
framing and the browser's own behaviour are checked by hand — see the surface
steps in the README.

Everything else about the surface *is* exercised here against real media,
generated with the ffmpeg that is already an ingest dependency. No window is
opened and nothing is displayed.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from library.ingest import ffmpeg_available
from tests.integration.conftest import drain

pytestmark = pytest.mark.skipif(
    not ffmpeg_available(), reason="ffmpeg is needed to generate the test media"
)


def make_png(path: Path, color: str = "red") -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", f"color=c={color}:s=64x64:d=1", "-frames:v", "1", str(path)],
        check=True, capture_output=True,
    )
    return path


def make_silent_video(path: Path) -> Path:
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=blue:s=64x64:d=2", "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return path


def make_video_with_audio(path: Path) -> Path:
    """The case SPEC §4 says 'will fail silently rather than error'."""
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "color=c=green:s=64x64:d=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
         "-pix_fmt", "yuv420p", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return path


async def place_still(client, path: Path, **extra) -> dict:
    body = {
        "path": str(path),
        "media": "image",
        "duration": {"kind": "until_replaced"},
        **extra,
    }
    r = await client.post("/buses/backdrop/items", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def test_the_surface_page_renders(client):
    r = await client.get("/surface")
    assert r.status_code == 200
    assert "surface.js" in r.text
    assert "surface.css" in r.text


async def test_an_image_is_ingested_and_served(client, engine, device, tmp_path):
    png = make_png(tmp_path / "still.png")
    await place_still(client, png, label="the bridge")
    await drain(engine, device, 0.05)

    showing = (await client.get("/status")).json()["sinks"]["visual"]["showing"]
    assert "backdrop" in showing

    r = await client.get("/media/" + _key_of(client, engine))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content == png.read_bytes()


def _key_of(client, engine) -> str:
    """The served filename for whatever the visual sink last resolved."""
    sink = engine.sinks.get("visual")
    return sink._last_by_bus["backdrop"]["url"].removeprefix("/media/")


async def test_a_video_with_an_audio_track_is_muted_and_said_out_loud(
    client, engine, device, tmp_path
):
    """SPEC §4: this 'will fail silently rather than error — strip or mute at
    the sink, and log when you do'. The muting was never the missing part;
    anyone being told was."""
    video = make_video_with_audio(tmp_path / "noisy.mp4")

    r = await client.post(
        "/buses/backdrop/items",
        json={"path": str(video), "media": "video", "label": "noisy clip"},
    )
    assert r.status_code == 201
    await drain(engine, device, 0.05)

    status = (await client.get("/status")).json()
    assert any("audio track" in w for w in status["library_warnings"])


async def test_a_silent_video_raises_no_warning(client, engine, device, tmp_path):
    """The check has to be specific, or the warning becomes noise and stops
    being read."""
    video = make_silent_video(tmp_path / "clip.mp4")
    await client.post(
        "/buses/backdrop/items", json={"path": str(video), "media": "video"}
    )
    await drain(engine, device, 0.05)

    status = (await client.get("/status")).json()
    assert not any("audio track" in w for w in status["library_warnings"])


async def test_the_page_reporting_completion_advances_the_bus(
    client, engine, device, tmp_path
):
    """The report that keeps visuals sequencing exactly like audio."""
    first = await place_still(client, make_png(tmp_path / "one.png"), label="first")
    await drain(engine, device, 0.05)

    r = await client.post("/surface/complete", json={"item_id": first["item_id"]})
    assert r.status_code == 200
    assert r.json()["accepted"] is True

    await drain(engine, device, 0.05)
    assert (await client.get(f"/items/{first['item_id']}")).json()["state"] == "played"


async def test_a_stale_completion_is_accepted_and_ignored(client):
    """A tab that reconnected mid-scene has no way to know its id is stale, and
    a 404 would not help it."""
    r = await client.post("/surface/complete", json={"item_id": "deadbeef"})
    assert r.status_code == 200
    assert r.json()["accepted"] is False


async def test_a_page_error_report_unblocks_without_claiming_success(
    client, engine, device, tmp_path
):
    placed = await place_still(client, make_png(tmp_path / "broken.png"), label="broken")
    await drain(engine, device, 0.05)

    await client.post(
        "/surface/complete",
        json={
            "item_id": placed["item_id"],
            "natural": False,
            "detail": "image failed to load",
        },
    )
    await drain(engine, device, 0.05)

    assert (await client.get(f"/items/{placed['item_id']}")).json()["state"] == "stopped"
    status = (await client.get("/status")).json()
    assert any("failed to load" in w["detail"] for w in status["warnings"])


async def test_no_browser_attached_is_warned_about(client, engine, device, tmp_path):
    """The fastest answer to "why is the screen blank". A bus rendering to
    nobody looks perfectly healthy otherwise."""
    await place_still(client, make_png(tmp_path / "lonely.png"), label="unseen")
    await drain(engine, device, 0.05)

    status = (await client.get("/status")).json()
    assert any("no browser attached" in w["detail"] for w in status["warnings"])
    assert status["sinks"]["visual"]["attached"] == 0


async def test_stopping_the_visual_bus_clears_it(client, engine, device, tmp_path):
    await place_still(client, make_png(tmp_path / "up.png"))
    await drain(engine, device, 0.05)

    await client.post("/buses/backdrop/stop")
    await drain(engine, device, 0.05)

    assert (await client.get("/status")).json()["sinks"]["visual"]["showing"] == {}


async def test_audio_on_the_visual_bus_is_refused(client):
    """The capability check, in the other direction."""
    r = await client.post(
        "/buses/backdrop/items", json={"asset": "tone.ok", "media": "audio"}
    )
    assert r.status_code == 409
    assert "does not accept" in r.json()["detail"]


async def test_a_visual_asset_on_an_audio_bus_is_refused(client, tmp_path):
    r = await client.post(
        "/buses/voice/items",
        json={"path": str(make_png(tmp_path / "wrong.png")), "media": "image"},
    )
    assert r.status_code == 409
