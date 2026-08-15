"""MCP surface — the direct-from-Claude-Code notification case.

SPEC §6: *"Wander should talk to Perform as a service rather than through MCP —
that's service-to-service. An MCP surface is worth adding for the
direct-from-Claude-Code notification case, which is a different consumer with
different needs."*

So this is deliberately not a mirror of the HTTP API. It is the small set of
things a coding session wants: say something, play a tone, shut up, and check
what is going on. Placing an item with a treatment and a duration policy is
Wander's job, and Wander speaks HTTP.

Follows wander's `app/mcp_server.py` beat for beat — FastMCP, stdio transport,
bare `@mcp.tool`, docstring-as-description, `dict[str, Any]` returns, and errors
*returned* rather than raised, because a raised exception in an MCP tool is a
protocol error where a returned one is something the model can act on.

    speak(text)      ->  the migration finished, out loud
    play(asset)      ->  tone.ok / tone.error / anything a pack declares
    stop()           ->  shut up
    status()         ->  why is it quiet
    next_visual()    ->  drain the pull sink into this conversation

This talks to a running Perform over HTTP rather than importing the engine: two
processes must not both open the audio device, and the server is the one that
owns it.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx
from fastmcp import FastMCP

log = logging.getLogger(__name__)

mcp = FastMCP("perform")

BASE_URL = os.environ.get("PERFORM_BASE_URL", "http://127.0.0.1:8770")
TOKEN = os.environ.get("PERFORM_TOKEN", "")
TIMEOUT_S = 15.0


def _client() -> httpx.Client:
    headers = {"Authorization": f"Bearer {TOKEN}"} if TOKEN else {}
    return httpx.Client(base_url=BASE_URL.rstrip("/"), headers=headers, timeout=TIMEOUT_S)


def _call(method: str, path: str, **kwargs) -> dict[str, Any]:
    """One place that turns transport and non-2xx into a returned error dict.

    The remediation goes in the message: a model that gets "perform is not
    running" and the command to start it can tell the human something useful,
    where one that gets a ConnectionError cannot.
    """
    try:
        with _client() as client:
            response = client.request(method, path, **kwargs)
    except httpx.ConnectError:
        return {
            "error": f"perform is not running at {BASE_URL}",
            "remediation": "start it with `make run` in the perform repo, or set PERFORM_BASE_URL",
        }
    except httpx.HTTPError as e:
        return {"error": f"{method} {path}: transport error: {e}"}

    if response.status_code >= 400:
        return {
            "error": f"{method} {path}: {response.status_code}",
            "detail": response.text[:400],
        }
    return response.json()


@mcp.tool
def speak(text: str, voice: str = "", bus: str = "voice") -> dict[str, Any]:
    """Say something out loud on this machine's speakers.

    For notifications from a coding session — "the migration finished", "tests
    are green", "the build broke". Returns immediately; the audio plays in the
    background. Call stop() to interrupt it.
    """
    body: dict[str, Any] = {"text": text, "bus": bus}
    if voice:
        body["voice"] = voice
    return _call("POST", "/speak", json=body)


@mcp.tool
def play(asset: str, bus: str = "voice", label: str = "") -> dict[str, Any]:
    """Play a named asset — a built-in tone or anything an installed pack declares.

    Built-ins: tone.ok (success), tone.error (failure), tone.warn (attention),
    tone.ping (neutral), tone.tick (progress). Call list_assets() for the rest.
    """
    return _call(
        "POST", f"/buses/{bus}/items", json={"asset": asset, "label": label or asset}
    )


@mcp.tool
def stop(bus: str = "") -> dict[str, Any]:
    """Stop playback immediately. Empty bus means everything.

    Clears the lane as well as interrupting what is playing — a stop that let
    the next queued item start would not be a stop.
    """
    return _call("POST", f"/buses/{bus}/stop" if bus else "/stop")


@mcp.tool
def status() -> dict[str, Any]:
    """What is playing, what is queued, and why it might be quiet.

    The `quiet_because` field answers that question directly rather than making
    you infer it from the bus table.
    """
    result = _call("GET", "/status")
    if "error" in result:
        return result
    # Trim to what is useful in a conversation. The full payload carries
    # per-item detail that is right for a dashboard and noise in a chat.
    return {
        "quiet_because": result.get("quiet_because"),
        "device_running": result.get("device", {}).get("running"),
        "buses": {
            name: {
                "state": b["state"],
                "playing": (b["current"] or {}).get("label"),
                "queued": len(b["pending"]),
            }
            for name, b in result.get("buses", {}).items()
        },
        "warnings": [w["detail"] for w in result.get("warnings", [])[:5]],
    }


@mcp.tool
def list_assets() -> dict[str, Any]:
    """Every asset name Perform can play, and whether it is warm."""
    result = _call("GET", "/assets")
    if "error" in result:
        return result
    return {
        "count": result["count"],
        "assets": [
            {
                "name": a["name"],
                "media": a["media"],
                "warm": a["warm"],
                "description": a["description"],
            }
            for a in result["assets"]
        ],
    }


@mcp.tool
def voices() -> dict[str, Any]:
    """The system voices `say` can use on this machine."""
    return _call("GET", "/voices")


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )
    mcp.run()


if __name__ == "__main__":
    main()
