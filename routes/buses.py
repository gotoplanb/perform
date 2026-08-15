"""Bus listing, per-bus stop, and the global stop.

SPEC §3 on why stop gets its own router and its own care:

    Interruption — per-bus stop and a global stop. Being unable to shut it up is
    the most common reason tools like this get abandoned, and it matters more
    here because Wander drives Perform faster than a person can react.

So `POST /stop` takes no arguments, needs no lookup, and cannot 404. If a caller
is panicking, the panic button must not ask questions.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from core.errors import UnknownBus
from deps import Authed, EngineDep

router = APIRouter(tags=["buses"], dependencies=[Authed])


class StopOut(BaseModel):
    stopped: list[str]
    dropped: int


@router.get("/buses")
async def list_buses(engine: EngineDep) -> dict:
    status_ = engine.status()
    return {"buses": status_["buses"]}


@router.post("/stop")
async def stop_all(engine: EngineDep) -> StopOut:
    """Everything, everywhere, now. No arguments, no lookup, no failure mode."""
    dropped = await engine.stop_all()
    return StopOut(stopped=sorted(engine.buses), dropped=dropped)


@router.post("/buses/{bus}/stop")
async def stop_bus(bus: str, engine: EngineDep) -> StopOut:
    """Interrupt one lane and clear it.

    Clearing is deliberate: a stop that killed only the current item would let
    the next one start milliseconds later, which is not what anyone means by
    stop. Dropped items are reported in `/status`, never silently discarded.
    """
    try:
        dropped = await engine.stop_bus(bus)
    except UnknownBus as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return StopOut(stopped=[bus], dropped=dropped)


class GainIn(BaseModel):
    gain_db: float


@router.put("/buses/{bus}/gain")
async def set_gain(bus: str, body: GainIn, engine: EngineDep) -> dict:
    """The relative mix — the one audio control SPEC §3 keeps, on the grounds
    that no system volume control can express it.

    This is not a master volume (SPEC §2 lists that as a non-goal). It sets one
    bus's level against the others, which is the thing that decides whether
    words stay intelligible over a siren.
    """
    try:
        b = engine.bus(bus)
    except UnknownBus as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    b.gain_db = body.gain_db
    engine.configure_mixer()
    return {"bus": bus, "gain_db": b.gain_db}
