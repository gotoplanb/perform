"""The MCP surface — the direct-from-Claude-Code notification case.

This is a shipped entrypoint (`perform-mcp`) and it was at 0% coverage, which
the quality gate is exactly the sort of thing that should surface. It is also
the surface most likely to be used while nothing else is: a coding session
firing `speak` at a server that may or may not be running.

`httpx.MockTransport` stands in for the server, so these need no running
Perform and add no dependency — the transport seam is already there because
`_client()` builds the client in one place.

Note the `.fn`: FastMCP's `@mcp.tool` replaces the module-level name with a
`FunctionTool` wrapper, so `mcp_server.speak` is an object rather than the
function. `.fn` is the original callable. Worth knowing before writing any test
against a FastMCP surface — the failure is `TypeError: 'FunctionTool' object is
not callable`, which does not obviously point at the decorator.
"""

from __future__ import annotations

import json

import httpx
import pytest

import mcp_server


@pytest.fixture
def transport(monkeypatch):
    """Route every tool call into a recorded handler instead of the network."""
    calls: list[httpx.Request] = []
    responses: dict[tuple[str, str], httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        key = (request.method, request.url.path)
        if key in responses:
            return responses[key]
        return httpx.Response(200, json={"ok": True})

    def fake_client() -> httpx.Client:
        return httpx.Client(
            base_url=mcp_server.BASE_URL.rstrip("/"),
            transport=httpx.MockTransport(handler),
        )

    monkeypatch.setattr(mcp_server, "_client", fake_client)
    return type("T", (), {"calls": calls, "responses": responses})()


# --- the tools ----------------------------------------------------------


def test_speak_posts_the_text(transport):
    transport.responses[("POST", "/speak")] = httpx.Response(
        202, json={"item_id": "abc", "bus": "voice", "cache_hit": False}
    )
    result = mcp_server.speak.fn("the migration finished")

    assert result["item_id"] == "abc"
    request = transport.calls[0]
    assert request.method == "POST"
    assert request.url.path == "/speak"
    assert b"the migration finished" in request.content


def test_speak_omits_an_empty_voice_rather_than_sending_one(transport):
    """An empty string would override the server's configured default with
    'no voice', which is not the same as 'use the default'.

    Asserted on the parsed body rather than a substring: the default bus is
    *named* "voice", so a naive `b"voice" not in content` fails on a payload
    that is entirely correct."""
    mcp_server.speak.fn("hello")
    body = json.loads(transport.calls[0].content)
    assert "voice" not in body
    assert body["bus"] == "voice"


def test_speak_passes_a_named_voice_through(transport):
    mcp_server.speak.fn("hello", voice="Samantha")
    assert b"Samantha" in transport.calls[0].content


def test_play_places_an_item_on_the_named_bus(transport):
    transport.responses[("POST", "/buses/ambience/items")] = httpx.Response(
        201, json={"item_id": "x", "bus": "ambience"}
    )
    mcp_server.play.fn("tone.ok", bus="ambience")
    assert transport.calls[0].url.path == "/buses/ambience/items"


def test_play_labels_the_item_with_the_asset_name_by_default(transport):
    """Perform never interprets a label, but a human reading /status should not
    have to correlate ids to know what was played."""
    mcp_server.play.fn("tone.error")
    assert b"tone.error" in transport.calls[0].content


def test_stop_with_no_bus_hits_the_global_stop(transport):
    mcp_server.stop.fn()
    assert transport.calls[0].url.path == "/stop"


def test_stop_with_a_bus_hits_that_lane_only(transport):
    mcp_server.stop.fn(bus="voice")
    assert transport.calls[0].url.path == "/buses/voice/stop"


def test_status_is_trimmed_for_a_conversation(transport):
    """The full payload carries per-item detail that is right for a dashboard
    and noise in a chat."""
    transport.responses[("GET", "/status")] = httpx.Response(
        200,
        json={
            "quiet_because": "nothing is playing",
            "device": {"running": True, "xruns": 0, "frames_rendered": 480},
            "buses": {
                "voice": {
                    "state": "idle",
                    "current": None,
                    "pending": [],
                    "recent": [{"label": "old", "state": "played"}],
                    "gain_db": 0.0,
                }
            },
            "warnings": [{"kind": "dropped", "detail": "something went wrong"}],
        },
    )
    result = mcp_server.status.fn()

    assert result["quiet_because"] == "nothing is playing"
    assert result["device_running"] is True
    assert result["buses"]["voice"] == {"state": "idle", "playing": None, "queued": 0}
    assert result["warnings"] == ["something went wrong"]
    # The noisy parts are gone.
    assert "recent" not in result["buses"]["voice"]


def test_status_reports_what_is_playing(transport):
    transport.responses[("GET", "/status")] = httpx.Response(
        200,
        json={
            "quiet_because": None,
            "device": {"running": True},
            "buses": {
                "voice": {
                    "state": "playing",
                    "current": {"label": "a line"},
                    "pending": [{"label": "next"}],
                    "recent": [],
                }
            },
            "warnings": [],
        },
    )
    voice = mcp_server.status.fn()["buses"]["voice"]
    assert voice["playing"] == "a line"
    assert voice["queued"] == 1


def test_list_assets_is_trimmed_too(transport):
    transport.responses[("GET", "/assets")] = httpx.Response(
        200,
        json={
            "count": 1,
            "warm": 1,
            "assets": [
                {
                    "name": "tone.ok",
                    "pack": "tone",
                    "media": "audio",
                    "alias_of": None,
                    "warm": True,
                    "pinned": True,
                    "duration_s": 0.45,
                    "use_count": 3,
                    "description": "rising third — success",
                }
            ],
        },
    )
    result = mcp_server.list_assets.fn()
    assert result["count"] == 1
    assert result["assets"] == [
        {
            "name": "tone.ok",
            "media": "audio",
            "warm": True,
            "description": "rising third — success",
        }
    ]


def test_voices_passes_through(transport):
    transport.responses[("GET", "/voices")] = httpx.Response(
        200, json={"available": True, "voices": ["Samantha"], "count": 1}
    )
    assert mcp_server.voices.fn()["count"] == 1


# --- errors are returned, never raised ---------------------------------


def test_a_dead_server_returns_a_remediation_not_a_stack_trace(monkeypatch):
    """A raised exception in an MCP tool is a protocol error; a returned one is
    something the model can act on and relay to the human."""

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(
        mcp_server,
        "_client",
        lambda: httpx.Client(
            base_url=mcp_server.BASE_URL, transport=httpx.MockTransport(refuse)
        ),
    )

    result = mcp_server.speak.fn("nobody is listening")
    assert "not running" in result["error"]
    assert "make run" in result["remediation"]


def test_a_non_2xx_is_returned_with_the_detail(transport):
    transport.responses[("POST", "/speak")] = httpx.Response(
        422, text="text is 4000 chars, over the 2000 limit"
    )
    result = mcp_server.speak.fn("x")
    assert "422" in result["error"]
    assert "over the 2000 limit" in result["detail"]


def test_a_transport_error_is_returned(monkeypatch):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    monkeypatch.setattr(
        mcp_server,
        "_client",
        lambda: httpx.Client(
            base_url=mcp_server.BASE_URL, transport=httpx.MockTransport(boom)
        ),
    )
    assert "transport error" in mcp_server.speak.fn("x")["error"]


def test_status_surfaces_an_error_rather_than_trimming_it(transport):
    """The trimming path must not run on an error dict and turn a clear message
    into a KeyError."""
    transport.responses[("GET", "/status")] = httpx.Response(500, text="boom")
    result = mcp_server.status.fn()
    assert "error" in result
    assert "buses" not in result


def test_list_assets_surfaces_an_error_rather_than_trimming_it(transport):
    transport.responses[("GET", "/assets")] = httpx.Response(503, text="down")
    result = mcp_server.list_assets.fn()
    assert "error" in result
    assert "assets" not in result


# --- registration -------------------------------------------------------


async def test_every_tool_is_registered_with_a_description():
    """The docstring is the tool description the model reads; an undocumented
    tool is one it will not pick correctly."""
    tools = await mcp_server.mcp.get_tools()
    assert set(tools) == {"speak", "play", "stop", "status", "list_assets", "voices"}
    for name, tool in tools.items():
        assert tool.description, f"{name} has no description"
