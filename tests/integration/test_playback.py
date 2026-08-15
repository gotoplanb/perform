"""End-to-end through the real ASGI app, on a NullDevice.

These go through the actual routers and the actual mixer — a placed item is
decoded, resampled, mixed, and rendered. The only difference from a real run is
that the frames go into a numpy array instead of into CoreAudio.
"""

from __future__ import annotations

import numpy as np

from tests.integration.conftest import drain


async def test_health_reports_the_device(client):
    r = await client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["device"] is True
    assert set(body["buses"]) == {"voice", "ambience", "backdrop"}


async def test_placing_a_tone_actually_makes_sound(client, engine, device):
    """The full path: name -> warm cache -> PCM -> mixer -> frames."""
    r = await client.post(
        "/buses/voice/items", json={"asset": "tone.ok", "label": "success"}
    )
    assert r.status_code == 201

    import asyncio

    for _ in range(6):
        await asyncio.sleep(0)

    out = device.pull_seconds(0.2)
    assert float(np.max(np.abs(out))) > 0.05


async def test_an_unknown_bus_is_a_404(client):
    r = await client.post("/buses/nope/items", json={"asset": "tone.ok"})
    assert r.status_code == 404
    assert "nope" in r.json()["detail"]


async def test_an_unknown_asset_is_reported_not_swallowed(client, engine, device):
    r = await client.post("/buses/voice/items", json={"asset": "tone.nonexistent"})
    assert r.status_code == 201  # placed; resolution happens when it plays

    await drain(engine, device)

    status = (await client.get("/status")).json()
    assert any("nonexistent" in w["detail"] for w in status["warnings"])
    assert any(
        r["state"] == "dropped" for r in status["buses"]["voice"]["recent"]
    )


async def test_supplying_both_asset_and_path_is_refused(client):
    r = await client.post(
        "/buses/voice/items", json={"asset": "tone.ok", "path": "/tmp/x.wav"}
    )
    assert r.status_code == 422
    assert "exactly one" in r.json()["detail"]


async def test_supplying_neither_is_refused(client):
    r = await client.post("/buses/voice/items", json={})
    assert r.status_code == 422


async def test_video_on_an_audio_bus_is_refused_at_placement(client):
    """The capability check fires at the point of the mistake, not forty seconds
    later as silence."""
    r = await client.post(
        "/buses/voice/items", json={"asset": "tone.ok", "media": "video"}
    )
    assert r.status_code == 409
    assert "does not accept" in r.json()["detail"]


# --- addressability ---------------------------------------------------------


async def test_a_pending_item_can_be_replaced_over_http(client, engine, device):
    """Wander's placeholder swap, through the API it will actually use."""
    first = (await client.post("/buses/voice/items", json={"asset": "tone.ping"})).json()
    placeholder = (
        await client.post(
            "/buses/voice/items", json={"asset": "tone.tick", "label": "placeholder"}
        )
    ).json()

    r = await client.put(
        f"/items/{placeholder['item_id']}",
        json={"asset": "tone.ok", "label": "the real line"},
    )
    assert r.status_code == 200
    assert r.json()["item_id"] == placeholder["item_id"]

    detail = (await client.get(f"/items/{placeholder['item_id']}")).json()
    assert detail["label"] == "the real line"
    assert detail["asset"] == "tone.ok"
    assert first["item_id"] != placeholder["item_id"]


async def test_replacing_a_playing_item_is_a_409(client, engine, device):
    placed = (
        await client.post(
            "/buses/voice/items",
            json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
        )
    ).json()
    await drain(engine, device, 0.05)

    r = await client.put(f"/items/{placed['item_id']}", json={"asset": "tone.ping"})
    assert r.status_code == 409
    assert "already playing" in r.json()["detail"]


async def test_a_removed_item_is_skipped_and_still_visible(client, engine, device):
    # A long declared duration rather than `until_replaced` for the holder:
    # placing a second item on a bus holding an open-ended item *is* the
    # replacement, so the second one would start playing and could no longer
    # be removed.
    await client.post(
        "/buses/voice/items",
        json={
            "asset": "tone.ok",
            "duration": {"kind": "declared", "seconds": 10},
            "label": "held",
        },
    )
    doomed = (
        await client.post("/buses/voice/items", json={"asset": "tone.error", "label": "doomed"})
    ).json()
    await drain(engine, device, 0.05)

    r = await client.delete(f"/items/{doomed['item_id']}")
    assert r.status_code == 200
    assert r.json()["state"] == "dropped"

    detail = (await client.get(f"/items/{doomed['item_id']}")).json()
    assert detail["state"] == "dropped"
    assert detail["note"]


async def test_removing_an_unknown_item_is_a_404(client):
    assert (await client.delete("/items/deadbeef")).status_code == 404


# --- interruption -----------------------------------------------------------


async def test_stop_silences_everything(client, engine, device):
    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
    )
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.1)
    assert float(np.max(np.abs(device.pull_seconds(0.05)))) > 0.01

    r = await client.post("/stop")
    assert r.status_code == 200
    assert set(r.json()["stopped"]) == {"voice", "ambience", "backdrop"}

    await drain(engine, device, 0.1)
    assert float(np.max(np.abs(device.pull_seconds(0.05)))) < 1e-4


async def test_stop_takes_no_arguments_and_cannot_fail(client):
    """If a caller is panicking, the panic button must not ask questions."""
    for _ in range(3):
        assert (await client.post("/stop")).status_code == 200


async def test_per_bus_stop_leaves_the_others_alone(client, engine, device):
    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
    )
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.1)

    await client.post("/buses/voice/stop")
    await drain(engine, device, 0.1)

    status = (await client.get("/status")).json()
    assert status["buses"]["voice"]["state"] == "idle"
    assert status["buses"]["ambience"]["state"] == "resting"


async def test_stop_clears_the_queue_too(client, engine, device):
    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "declared", "seconds": 10}},
    )
    for i in range(3):
        await client.post("/buses/voice/items", json={"asset": "tone.tick", "label": f"q{i}"})
    await drain(engine, device, 0.05)

    r = await client.post("/buses/voice/stop")
    assert r.json()["dropped"] == 3


async def test_stopping_an_unknown_bus_is_a_404(client):
    assert (await client.post("/buses/nope/stop")).status_code == 404


# --- the relative mix -------------------------------------------------------


async def test_setting_bus_gain_changes_the_output(client, engine, device):
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.1)
    loud = float(np.max(np.abs(device.pull_seconds(0.1))))

    await client.put("/buses/ambience/gain", json={"gain_db": -40.0})
    await drain(engine, device, 0.2)
    quiet = float(np.max(np.abs(device.pull_seconds(0.1))))

    assert quiet < loud / 4


async def test_gain_on_an_unknown_bus_is_a_404(client):
    r = await client.put("/buses/nope/gain", json={"gain_db": 0.0})
    assert r.status_code == 404


async def test_voice_ducks_ambience_end_to_end(client, engine, device):
    """The one audio control SPEC §3 keeps, exercised through the real API."""
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.3)
    unducked = float(np.sqrt(np.mean(device.pull_seconds(0.1) ** 2)))

    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.3)

    status = (await client.get("/status")).json()
    assert status["buses"]["ambience"]["duck_db"] < -6.0
    assert unducked > 0
