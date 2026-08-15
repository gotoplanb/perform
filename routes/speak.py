"""`POST /speak` — the day-one surface.

SPEC §2, on why Perform is a service rather than a Wander module:

    The thing worth having on day one has nothing to do with scenarios: a Claude
    Code session that can say "the migration finished" out loud. Folded into
    Wander, that only exists when Wander is running, and therefore doesn't get
    used.

This route is that sentence. It takes a string, runs it through the OS `say`
binary, and places the result on a bus as an ordinary item — the same item type,
the same bus, the same stop, the same warm cache as everything Wander drives.
There is no separate speech pipeline, which is the point: the recording lands in
the cache keyed by content, so the hundredth "tests passed" is a cache hit.
"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from audio import speech
from core.errors import PerformError, SpeechError
from core.item import AssetRef, Duration, DurationKind, Item, Media, Treatment
from deps import Authed, EngineDep, LibraryDep, SettingsDep
from routes.items import as_http

log = logging.getLogger(__name__)

router = APIRouter(tags=["speak"], dependencies=[Authed])


class SpeakIn(BaseModel):
    text: str = Field(min_length=1, max_length=speech.MAX_CHARS)
    # Empty means the configured default, which in turn may mean the system
    # default. An unknown name was already rejected at startup.
    voice: str = ""
    bus: str = "voice"
    treatment: Treatment = Treatment.CLEAN
    label: str = ""


class SpeakOut(BaseModel):
    item_id: str
    bus: str
    voice: str
    cache_hit: bool
    duration_s: float


@router.post("/speak", status_code=status.HTTP_202_ACCEPTED)
async def speak(
    body: SpeakIn, engine: EngineDep, library: LibraryDep, settings: SettingsDep
) -> SpeakOut:
    voice = body.voice or settings.say_voice

    try:
        recording = speech.synthesize(
            body.text,
            Path(settings.blobs_dir) / "speech",
            voice=voice,
            rate=settings.say_rate,
        )
    except SpeechError as e:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e

    try:
        # Straight into the ordinary asset path. `say` output is a local file
        # like any other, and treating it as one is what gets it cached.
        resolved = library.resolve(AssetRef(path=recording), body.treatment)
    except PerformError as e:
        recording.unlink(missing_ok=True)
        raise as_http(e) from e
    finally:
        # The AIFF has been normalized into the cache under its content hash;
        # the temp recording itself is not the artifact and should not linger.
        recording.unlink(missing_ok=True)

    item = Item(
        bus=body.bus,
        # Address the cached blob rather than the file we just deleted.
        asset=AssetRef(path=Path(library.store.get_blob(resolved.key).path)),
        media=Media.AUDIO,
        duration=Duration(kind=DurationKind.INTRINSIC),
        treatment=body.treatment,
        label=body.label or _snippet(body.text),
    )

    try:
        placed = engine.place(item)
    except PerformError as e:
        raise as_http(e) from e

    return SpeakOut(
        item_id=placed.item.id,
        bus=body.bus,
        voice=voice or "(system default)",
        cache_hit=resolved.cache_hit,
        duration_s=round(resolved.duration_s, 3),
    )


@router.get("/voices")
async def voices(settings: SettingsDep) -> dict:
    """What `say` can do on this machine.

    Conduct has no equivalent — its voice string goes straight to the filesystem
    with no validation and no listing endpoint, so a bad name is accepted with a
    202 and fails later inside the worker. Perform lists them and validates at
    startup instead, because SPEC §8 asks for the opposite of that.
    """
    installed = speech.list_voices()
    return {
        "available": speech.say_available(),
        "configured": settings.say_voice or "(system default)",
        "voices": installed,
        "count": len(installed),
    }


def _snippet(text: str, limit: int = 48) -> str:
    """A default label. Perform never interprets a label, but a human reading
    `/status` at 2am should not have to correlate ids to know what was said."""
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
