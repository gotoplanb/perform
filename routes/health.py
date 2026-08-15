"""Liveness. Open, unauthenticated, never blocks on anything slow.

Shaped like conduct's `/health`: report degraded rather than failing, so a
monitor can tell "Perform is up but its audio device went away" apart from
"Perform is gone". The distinction matters here because a dead device is the
single most likely cause of "why is it quiet".
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    engine = getattr(request.app.state, "engine", None)
    device_ok = bool(engine and engine.device_running)
    return {
        "status": "ok" if device_ok else "degraded",
        "device": device_ok,
        "buses": sorted(engine.buses) if engine else [],
    }
