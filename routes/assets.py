"""The asset catalog and the warm-cache control.

SPEC §5: *"'Pre-generating a theme' then becomes 'warming the cache before the
scene,' using the same code path as just-in-time delivery, and it degrades
gracefully — an unfinished warm just means the first few items are slower."*

`POST /assets/warm` is therefore a thin wrapper over exactly the function the
engine calls microseconds before an item plays. It is not a separate pipeline,
and there is deliberately no way for it to become one.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status
from pydantic import BaseModel

from core.errors import UnknownAsset
from core.item import Treatment
from deps import Authed, LibraryDep

router = APIRouter(tags=["assets"], dependencies=[Authed])


class WarmIn(BaseModel):
    names: list[str]
    # Treatments are baked in at ingest, so warming with the treatment a scene
    # will actually use is what makes that scene's first line fast. Warming
    # `clean` and then playing `phone` is a miss.
    treatment: Treatment = Treatment.CLEAN


@router.get("/assets")
async def list_assets(library: LibraryDep, pack: str | None = None) -> dict:
    catalog = library.catalog()
    if pack:
        catalog = [a for a in catalog if a["pack"] == pack]
    return {
        "count": len(catalog),
        "warm": sum(1 for a in catalog if a["warm"]),
        "assets": catalog,
    }


@router.post("/assets/warm")
async def warm(body: WarmIn, library: LibraryDep) -> dict:
    """Ingest a set of names ahead of a scene.

    Never raises for a single bad name — a warm is best-effort by construction
    and one typo should not abandon the other forty. Every failure comes back in
    the result and lands in `/status`, because a warm that quietly skipped half
    a theme is the silent failure SPEC §8 rules out.
    """
    results = library.warm(body.names, body.treatment)
    failed = {k: v for k, v in results.items() if v.startswith("failed")}
    return {
        "requested": len(body.names),
        "warmed": sum(1 for v in results.values() if v == "warmed"),
        "already_warm": sum(1 for v in results.values() if v == "hit"),
        "failed": len(failed),
        "results": results,
    }


@router.get("/media/{filename}")
async def media(filename: str, library: LibraryDep) -> Response:
    """Serve a visual asset to the browser surface.

    The only rule is that the resolved path stays under the blob directory —
    which is why `resolve_visual` links media in there rather than serving
    originals in place. Same guard conduct puts on `/output/{file}`, arrived at
    for the same reason: this is the one route that turns a caller-supplied
    string into a file read.
    """
    try:
        path = library.blob_path(filename)
    except UnknownAsset as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e

    from fastapi.responses import FileResponse  # noqa: PLC0415

    return FileResponse(path, media_type=_media_type(path.suffix))


def _media_type(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".svg": "image/svg+xml",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mov": "video/quicktime",
    }.get(suffix.lower(), "application/octet-stream")
