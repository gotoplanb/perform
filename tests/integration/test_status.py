"""`GET /status` and `/ui` — the "why is it quiet" surface.

SPEC §8 treats this as a feature, not a debug endpoint, so it gets tested like
one: every state that could make Perform quiet should be legible from the
payload without inference.
"""

from __future__ import annotations

from tests.integration.conftest import drain


async def test_idle_says_so_plainly(client):
    body = (await client.get("/status")).json()
    assert "idle" in body["quiet_because"]
    assert body["device"]["running"] is True


async def test_playing_has_nothing_to_explain(client, engine, device):
    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.05)
    assert (await client.get("/status")).json()["quiet_because"] is None


async def test_a_stalled_bus_is_named_and_explained(client, engine, device, clock):
    """A voice bus resting on an open-ended item is a hung scene, and the
    payload should say which bus and why."""
    await client.post(
        "/buses/voice/items",
        json={
            "asset": "tone.ok",
            "duration": {"kind": "until_replaced"},
            "label": "hung line",
        },
    )
    await drain(engine, device, 0.05)
    await clock.advance(31.0)

    body = (await client.get("/status")).json()
    assert body["buses"]["voice"]["state"] == "stalled"
    assert "voice" in body["quiet_because"]
    assert "until_replaced" in body["quiet_because"]


async def test_a_backdrop_resting_is_not_a_problem(client, engine, device, clock):
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.05)
    await clock.advance(600.0)

    body = (await client.get("/status")).json()
    assert body["buses"]["ambience"]["state"] == "resting"
    assert "ambience" not in (body["quiet_because"] or "")


async def test_status_reports_effective_gain_including_the_duck(client, engine, device):
    await client.post(
        "/buses/ambience/items",
        json={"asset": "tone.ping", "duration": {"kind": "until_replaced"}},
    )
    await client.post(
        "/buses/voice/items",
        json={"asset": "tone.ok", "duration": {"kind": "until_replaced"}},
    )
    await drain(engine, device, 0.4)

    ambience = (await client.get("/status")).json()["buses"]["ambience"]
    assert ambience["gain_db"] == -6.0
    assert ambience["duck_db"] < -6.0
    assert ambience["effective_db"] < ambience["gain_db"]


async def test_status_lists_the_declared_duck_matrix(client):
    voice = (await client.get("/status")).json()["buses"]["voice"]
    assert voice["ducks"] == [{"target": "ambience", "depth_db": -12.0}]


async def test_status_carries_sink_detail(client):
    body = (await client.get("/status")).json()
    assert body["sinks"]["local_audio"]["device_running"] is True
    # An SSE stream with no subscriber is a laptop with the tab closed, and it
    # is the fastest answer to "why is the screen blank".
    assert body["sinks"]["visual"]["attached"] == 0


async def test_buses_endpoint_mirrors_status(client):
    buses = (await client.get("/buses")).json()["buses"]
    assert set(buses) == {"voice", "ambience", "backdrop"}


async def test_the_ui_serves_a_shell(client):
    """The page renders client-side from /status, so this is a static shell —
    it should not need the engine and should carry no data."""
    r = await client.get("/ui")
    assert r.status_code == 200
    assert "status.js" in r.text
    assert "status.css" in r.text


async def test_the_ui_shell_leaks_no_status_data(client, engine, device):
    """The reason the page became a shell rather than staying server-rendered:
    this router carries no auth while /status does, so rendering the payload
    here served it unauthenticated whenever PERFORM_TOKEN was set."""
    await client.post(
        "/buses/voice/items", json={"asset": "tone.ok", "label": "a secret label"}
    )
    await drain(engine, device, 0.05)

    body = (await client.get("/ui")).text
    assert "a secret label" not in body
    assert "quiet_because" not in body
    assert "tone.ok" not in body


async def test_the_ui_works_without_the_engine(app, client):
    """A shell has nothing to fail on. Useful precisely when perform is
    unhealthy — the page loads and reports the failure rather than 503ing."""
    engine, app.state.engine = app.state.engine, None
    try:
        assert (await client.get("/ui")).status_code == 200
    finally:
        app.state.engine = engine


async def test_the_root_redirects_to_the_ui(client):
    r = await client.get("/", follow_redirects=False)
    assert r.status_code in (307, 302)
    assert r.headers["location"] == "/ui"
