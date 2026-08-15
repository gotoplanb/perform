"""`GET /status` — the "why is it quiet" endpoint.

SPEC §8 is unusually specific about this one:

    Failures visible, never silent. A dropped item, a missing asset name, a
    stalled bus, a muted-on-arrival video — each obvious from status output.
    "Why is it quiet" should take seconds to answer.

Which makes this a feature rather than a debug endpoint, and the reason the
payload carries a `quiet_because` string: making someone read a bus table and
infer the answer is not seconds. Saying it is.
"""

from __future__ import annotations

from fastapi import APIRouter

from deps import Authed, EngineDep, LibraryDep

router = APIRouter(tags=["status"], dependencies=[Authed])


@router.get("/status")
async def status(engine: EngineDep, library: LibraryDep) -> dict:
    payload = engine.status()
    # Library warnings — a failed warm, a video whose audio track was dropped —
    # are the other half of "why is it quiet", and they happen outside any bus.
    payload["library_warnings"] = library.warnings[-10:]
    return payload
