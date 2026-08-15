"""Placing, replacing, and removing items — the surface Wander drives.

The shape of this router is dictated by SPEC §3's addressability requirement:

    Items already placed but not yet played must be individually replaceable and
    removable. Wander puts a placeholder in a slot, swaps in the real asset when
    generation lands, or drops it if it doesn't. This is a mutable ordered list,
    not a queue.

So: POST appends and returns an id, PUT swaps in place keeping that id, DELETE
drops it. All three are 4xx once the item has started, because after that the
only lever is interruption.

Route conventions follow conduct — `APIRouter(prefix, tags)`, `<Thing>In` /
`<Thing>Out` models defined inline in the route module rather than in a shared
schemas package, `status.HTTP_*` constants, lowercase detail strings.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from core.errors import (
    AssetError,
    ItemAlreadyPlaying,
    PerformError,
    SinkRejected,
    UnknownBus,
    UnknownItem,
)
from core.item import AssetRef, Duration, DurationKind, Item, Media, Transition, Treatment
from deps import Authed, EngineDep

router = APIRouter(tags=["items"], dependencies=[Authed])


class DurationIn(BaseModel):
    kind: DurationKind = DurationKind.INTRINSIC
    repeat: int | None = None
    seconds: float | None = None

    def build(self) -> Duration:
        try:
            return Duration(kind=self.kind, repeat=self.repeat, seconds=self.seconds)
        except ValueError as e:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e)) from e


class ItemIn(BaseModel):
    """Exactly one of `asset` (a logical name) or `path` (a local file).

    Local paths are how Wander hands over something Conduct just generated —
    conduct writes TTS output to a host-mounted `output/{job_id}.mp3`, and its
    own docs treat that directory as a LAN-visible handoff point.
    """

    asset: str | None = None
    path: str | None = None
    media: Media = Media.AUDIO
    duration: DurationIn = Field(default_factory=DurationIn)
    treatment: Treatment = Treatment.CLEAN
    transition: Transition = Transition.CUT
    label: str = ""

    def build(self, bus: str) -> Item:
        if (self.asset is None) == (self.path is None):
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "supply exactly one of 'asset' (a logical name) or 'path' (a local file)",
            )
        ref = (
            AssetRef(key=self.asset)
            if self.asset is not None
            else AssetRef(path=Path(self.path))  # type: ignore[arg-type]
        )
        return Item(
            bus=bus,
            asset=ref,
            media=self.media,
            duration=self.duration.build(),
            treatment=self.treatment,
            transition=self.transition,
            label=self.label,
        )


class ItemOut(BaseModel):
    item_id: str
    bus: str
    state: str
    label: str


@router.post("/buses/{bus}/items", status_code=status.HTTP_201_CREATED)
async def place_item(bus: str, body: ItemIn, engine: EngineDep) -> ItemOut:
    item = body.build(bus)
    try:
        placed = engine.place(item)
    except UnknownBus as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except SinkRejected as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return ItemOut(
        item_id=placed.item.id, bus=bus, state=placed.state.value, label=placed.item.label
    )


@router.put("/items/{item_id}")
async def replace_item(item_id: str, body: ItemIn, engine: EngineDep) -> ItemOut:
    """Swap a pending item in place. The slot and the id survive the swap."""
    try:
        bus, _ = engine.find_item(item_id)
        placed = engine.replace(item_id, body.build(bus.name))
    except UnknownItem as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ItemAlreadyPlaying as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    except SinkRejected as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return ItemOut(
        item_id=placed.item.id,
        bus=placed.item.bus,
        state=placed.state.value,
        label=placed.item.label,
    )


@router.delete("/items/{item_id}")
async def remove_item(item_id: str, engine: EngineDep) -> ItemOut:
    """Drop a pending item. It stays visible in `/status` marked dropped —
    an item that simply disappeared is the definition of a silent failure."""
    try:
        bus, _ = engine.find_item(item_id)
        placed = engine.remove(bus.name, item_id)
    except UnknownItem as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    except ItemAlreadyPlaying as e:
        raise HTTPException(status.HTTP_409_CONFLICT, str(e)) from e
    return ItemOut(
        item_id=placed.item.id,
        bus=placed.item.bus,
        state=placed.state.value,
        label=placed.item.label,
    )


@router.get("/items/{item_id}")
async def get_item(item_id: str, engine: EngineDep) -> dict:
    try:
        bus, placed = engine.find_item(item_id)
    except UnknownItem as e:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(e)) from e
    return {
        "item_id": placed.item.id,
        "bus": bus.name,
        "state": placed.state.value,
        "label": placed.item.label,
        "asset": str(placed.item.asset),
        "note": placed.note,
        "started_at": placed.started_at,
        "ended_at": placed.ended_at,
    }


def as_http(e: PerformError) -> HTTPException:
    """Map the domain errors onto statuses in one place, the way conduct's route
    layer converts ProviderError -> 502 and SensitivityViolation -> 400."""
    if isinstance(e, (UnknownBus, UnknownItem)):
        return HTTPException(status.HTTP_404_NOT_FOUND, str(e))
    if isinstance(e, (ItemAlreadyPlaying, SinkRejected)):
        return HTTPException(status.HTTP_409_CONFLICT, str(e))
    if isinstance(e, AssetError):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(e))
    return HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, str(e))
