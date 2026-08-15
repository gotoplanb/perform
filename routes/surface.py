"""The browser surface: the page, its SSE stream, and its completion callback.

Three routes and a deliberate asymmetry — events go out over SSE, completions
come back as an ordinary POST. That is not an oversight: SSE is one-directional,
and a plain POST is both sufficient and consistent with how the rest of this
system talks (conduct and wander are all-HTTP, no sockets).

`GET /surface/events` is the only streaming endpoint in any of the three
services. See sinks/visual.py for why it earns the exception.
"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from deps import Authed, EngineDep

log = logging.getLogger(__name__)

router = APIRouter(tags=["surface"])

HEARTBEAT_S = 15.0


def _sink(engine):
    if not engine.sinks.has("visual"):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no visual sink registered — no bus routes to one",
        )
    return engine.sinks.get("visual")


@router.get("/surface", response_class=HTMLResponse)
async def surface(request: Request) -> HTMLResponse:
    """The page itself. Server-rendered, no build step, no framework — the same
    posture as wander's Jinja + HTMX + Alpine app."""
    return request.app.state.templates.TemplateResponse(request, "surface.html", {})


@router.get("/surface/events")
async def events(request: Request, engine: EngineDep) -> StreamingResponse:
    """Server-sent events. `EventSource` reconnects on its own, which is the
    whole reason this shape was chosen over a socket — a closed laptop lid
    should not need client code to recover from."""
    sink = _sink(engine)
    queue = sink.subscribe()

    async def stream():
        # Disconnection is detected by this generator being closed, not by
        # polling `request.is_disconnected()`. That call awaits a receive
        # message, and on a transport that never sends one it blocks forever —
        # which stalls the whole stream rather than the one check. The `finally`
        # below runs on close either way, which is all the cleanup needs.
        try:
            # Tell the browser to back off a little before retrying. The default
            # is aggressive enough to hammer a server that is restarting.
            yield "retry: 2000\n\n"
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_S)
                except TimeoutError:
                    # A comment frame. Keeps proxies and power management from
                    # dropping an idle connection, which would otherwise look
                    # like a surface that mysteriously stopped updating.
                    yield ": keepalive\n\n"
                    continue
                yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"
        finally:
            sink.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # nginx and friends buffer by default, which turns an event stream
            # into a very slow batch job.
            "X-Accel-Buffering": "no",
        },
    )


class CompleteIn(BaseModel):
    item_id: str
    # False when the page is reporting an error rather than an ending — a video
    # that failed to decode still needs to unblock the bus, but it should not be
    # recorded as having played.
    natural: bool = True
    detail: str = ""


@router.post("/surface/complete", dependencies=[Authed])
async def complete(body: CompleteIn, engine: EngineDep) -> dict:
    """The page reporting that a video ended or an animation hit its loop count.

    This report is what keeps visuals sequencing exactly like audio (SPEC §4).
    A stale id — from a tab that reconnected mid-scene — is accepted and
    ignored rather than 404'd, because the page has no way to know it is stale
    and retrying would not help.
    """
    sink = _sink(engine)
    accepted = await sink.report_complete(body.item_id, natural=body.natural)
    if body.detail:
        log.info("surface reported %s: %s", body.item_id, body.detail)
        engine.warn("backdrop", "surface", f"{body.item_id}: {body.detail}")
    return {"accepted": accepted, "item_id": body.item_id}
