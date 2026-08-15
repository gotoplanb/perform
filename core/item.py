"""The item vocabulary, straight out of SPEC §3.

Everything here is frozen and free of behavior on purpose. An Item is what
Wander hands over and what Perform reports back; keeping it a value means the
engine can hold one, the mixer can hold a reference to the same one, and
`/status` can serialize it, with nobody able to mutate it out from under the
others.

The one thing worth reading twice is `label`. Perform logs it and reports it and
*never* interprets it. It exists so that when something plays at the wrong
moment, Wander's traces and Perform's logs name the same thing. The moment
Perform branches on a label, Perform has learned what a "Tier 2 SRE" is, and
SPEC §2's whole non-goal list starts unravelling.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Media(StrEnum):
    AUDIO = "audio"
    VIDEO = "video"
    IMAGE = "image"


class DurationKind(StrEnum):
    """Where an item's length comes from. Media-agnostic by design (SPEC §3) —
    a looping animation and an ambience bed are the same case."""

    INTRINSIC = "intrinsic"  # audio, video. The file knows.
    REPEAT = "repeat"  # one cycle x N
    DECLARED = "declared"  # a still held for a stated interval
    UNTIL_REPLACED = "until_replaced"  # held until the next item on that bus begins


@dataclass(frozen=True)
class Duration:
    kind: DurationKind
    repeat: int | None = None
    seconds: float | None = None

    def __post_init__(self) -> None:
        if self.kind is DurationKind.REPEAT and (self.repeat is None or self.repeat < 1):
            raise ValueError("duration.repeat must be >= 1 when kind is 'repeat'")
        if self.kind is DurationKind.DECLARED and (self.seconds is None or self.seconds <= 0):
            raise ValueError("duration.seconds must be > 0 when kind is 'declared'")

    @property
    def open_ended(self) -> bool:
        """True when nothing but a replacement will end this item.

        A bus resting here is either at rest or hung, and which one it is
        depends on the bus, not the item — see Bus.resting_open_ended.
        """
        return self.kind is DurationKind.UNTIL_REPLACED

    @classmethod
    def intrinsic(cls) -> Duration:
        return cls(kind=DurationKind.INTRINSIC)


@dataclass(frozen=True)
class AssetRef:
    """What to play, named two ways.

    `key` is the logical name — `sre.greeting` — which is what scenes reference
    and what makes them portable across machines (SPEC §5). `path` is the
    resolved local file, which is how Wander hands over something it just had
    generated. Exactly one is supplied by the caller; the library fills in the
    other during resolution.

    Splitting these mirrors conduct's MediaResponse, which keeps `file_path`
    (worker-local) apart from `url_path` (what the API serves) so the storage
    backend can move without touching the callers.
    """

    key: str | None = None
    path: Path | None = None

    def __post_init__(self) -> None:
        if (self.key is None) == (self.path is None):
            raise ValueError("AssetRef needs exactly one of key or path")

    def __str__(self) -> str:
        return self.key if self.key is not None else str(self.path)


class Treatment(StrEnum):
    """A filter preset applied to an individual item, not to its bus (SPEC §3).

    A property of the item because the same voice bus carries a customer on a
    phone line and an SRE on the bridge, one after the other.
    """

    CLEAN = "clean"
    INTERNAL_BRIDGE = "internal_bridge"
    INCIDENT_BRIDGE = "incident_bridge"
    PHONE = "phone"


class Transition(StrEnum):
    """How an item arrives. SPEC §3 notes these matter far more for visuals than
    for audio — a hard cut between backdrops looks broken in a way a hard cut
    between lines does not sound broken."""

    CUT = "cut"
    FADE = "fade"
    DISSOLVE = "dissolve"


class ItemState(StrEnum):
    PENDING = "pending"  # placed, not yet started. Replaceable and removable.
    PLAYING = "playing"
    PLAYED = "played"
    STOPPED = "stopped"  # interrupted mid-item
    REPLACED = "replaced"  # swapped out while still pending
    DROPPED = "dropped"  # removed, or unplayable. Always reported, never silent.


def new_item_id() -> str:
    return uuid.uuid4().hex[:12]


@dataclass(frozen=True)
class Item:
    """An asset plus a target bus, a treatment, a duration policy, and an
    opaque label."""

    bus: str
    asset: AssetRef
    media: Media = Media.AUDIO
    duration: Duration = field(default_factory=Duration.intrinsic)
    treatment: Treatment = Treatment.CLEAN
    transition: Transition = Transition.CUT
    # Opaque. Logged, reported, never interpreted — see the module docstring.
    label: str = ""
    id: str = field(default_factory=new_item_id)

    def replaced_by(self, other: Item) -> Item:
        """The successor keeps this item's id, so a caller holding a reference
        can keep addressing the slot after a swap.

        This is what SPEC §3's "mutable ordered list, not a queue" buys: Wander
        drops a placeholder in a slot, then swaps in the real asset when
        generation lands — and the slot's identity survives the swap.
        """
        return Item(
            bus=self.bus,
            asset=other.asset,
            media=other.media,
            duration=other.duration,
            treatment=other.treatment,
            transition=other.transition,
            label=other.label or self.label,
            id=self.id,
        )
