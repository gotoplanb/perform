"""The asset catalog, the warm-cache endpoint, and the media route guard."""

from __future__ import annotations


async def test_the_catalog_lists_the_built_in_tones(client):
    """Something works before a single asset is sourced (SPEC §5)."""
    body = (await client.get("/assets")).json()
    names = {a["name"] for a in body["assets"]}
    assert {"tone.ok", "tone.error", "tone.warn", "tone.ping", "tone.tick"} <= names
    assert all(a["media"] == "audio" for a in body["assets"])


async def test_the_catalog_filters_by_pack(client):
    body = (await client.get("/assets?pack=tone")).json()
    assert body["count"] == 5
    assert all(a["pack"] == "tone" for a in body["assets"])


async def test_an_unknown_pack_filter_returns_nothing_rather_than_erroring(client):
    assert (await client.get("/assets?pack=nope")).json()["count"] == 0


async def test_warming_reports_per_name(client):
    r = await client.post("/assets/warm", json={"names": ["tone.ok", "tone.error"]})
    assert r.status_code == 200
    body = r.json()
    assert body["requested"] == 2
    assert body["failed"] == 0
    assert body["warmed"] + body["already_warm"] == 2


async def test_warming_a_bad_name_does_not_abandon_the_good_ones(client):
    """A warm is best-effort by construction: one typo should not abandon the
    other forty. Every failure is still reported."""
    r = await client.post(
        "/assets/warm", json={"names": ["tone.ok", "tone.typo", "tone.ping"]}
    )
    body = r.json()

    assert body["failed"] == 1
    assert body["results"]["tone.typo"].startswith("failed")
    assert body["results"]["tone.ok"] in ("warmed", "hit")
    assert body["results"]["tone.ping"] in ("warmed", "hit")


async def test_a_failed_warm_shows_up_in_status(client):
    """SPEC §8 has no room for a warm that quietly skipped half a theme."""
    await client.post("/assets/warm", json={"names": ["tone.typo"]})
    body = (await client.get("/status")).json()
    assert any("tone.typo" in w for w in body["library_warnings"])


async def test_warming_then_playing_is_a_cache_hit(client):
    await client.post("/assets/warm", json={"names": ["tone.ok"]})
    body = (await client.get("/assets")).json()
    entry = next(a for a in body["assets"] if a["name"] == "tone.ok")
    assert entry["warm"] is True


async def test_the_media_route_refuses_traversal(client):
    """The one route that turns a caller-supplied string into a file read."""
    for attempt in ("..%2F..%2Fetc%2Fpasswd", "....//etc/passwd", "nope.png"):
        r = await client.get(f"/media/{attempt}")
        assert r.status_code == 404
