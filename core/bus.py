"""A bus: one playback lane with its own ordered list (SPEC §3).

Items on one bus play serially; buses run concurrently. Keep the bus count small
and fixed — buses are declared in config/buses.yaml at startup and are never
created at runtime, which is why `UnknownBus` is a 404 and not a "create it".

Two things here are easy to get wrong and are called out in SPEC:

**It is a list, not a queue.** Items already placed but not yet played must be
individually replaceable and removable, because Wander puts a placeholder in a
slot and swaps in the real asset when generation lands, or drops it if it never
does. A queue can't express that. So: a list with stable item ids, a cursor, and
played items retained for reporting rather than popped.

**Buses differ in resting behavior.** A backdrop bus is *expected* to end on an
open-ended item — that's its steady state. A voice bus ending on one is a hung
scene. Same item, same duration policy, opposite meanings, so the bus carries
the policy and `stalled` is computed against it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from core.errors import ItemAlreadyPlaying, UnknownItem
from core.item import Item, ItemState


class BusState(StrEnum):
    IDLE = "idle"  # nothing playing, nothing pending
    PLAYING = "playing"
    RESTING = "resting"  # holding an open-ended item, and that is expected here
    STALLED = "stalled"  # holding an open-ended item, and it is not expected here


@dataclass(frozen=True)
class DuckRule:
    """How much this bus pushes another one down while it has audio.

    This relative mix is the one audio control SPEC §3 keeps, on the grounds
    that no system volume control can express it: the listener can turn the
    whole thing down, but only Perform can say "words stay intelligible over a
    siren".
    """

    target: str
    depth_db: float  # negative. -12 means the target drops 12dB while this bus talks.
    attack_ms: float = 80.0
    release_ms: float = 400.0

    def __post_init__(self) -> None:
        if self.depth_db > 0:
            raise ValueError(f"duck depth_db must be <= 0, got {self.depth_db}")
        if self.attack_ms <= 0 or self.release_ms <= 0:
            raise ValueError("duck attack_ms and release_ms must be > 0")


@dataclass
class PlacedItem:
    """An item plus where it got to. The state is what `/status` reports and
    what stops a replaced or dropped item from vanishing without a trace."""

    item: Item
    state: ItemState = ItemState.PENDING
    started_at: float | None = None
    ended_at: float | None = None
    note: str = ""  # why it was dropped, if it was

    @property
    def terminal(self) -> bool:
        return self.state in (
            ItemState.PLAYED,
            ItemState.STOPPED,
            ItemState.REPLACED,
            ItemState.DROPPED,
        )


@dataclass
class Bus:
    name: str
    sink: str
    gain_db: float = 0.0
    # Backdrop and ambience buses rest on open-ended items; that is the point of
    # them. A voice bus doing the same is a hung scene, so it gets reported.
    resting_open_ended: bool = False
    ducks: list[DuckRule] = field(default_factory=list)
    # The mutable ordered list. Terminal entries stay put so `/status` can still
    # answer "what happened to the line I placed"; `prune` trims the tail.
    placed: list[PlacedItem] = field(default_factory=list)
    state: BusState = BusState.IDLE
    # When the current open-ended item started resting. Only meaningful while
    # holding one; the stall detector compares it against settings.stall_after_s.
    resting_since: float | None = None

    # --- placement ------------------------------------------------------

    def place(self, item: Item) -> PlacedItem:
        placed = PlacedItem(item=item)
        self.placed.append(placed)
        return placed

    def find(self, item_id: str) -> PlacedItem:
        for p in self.placed:
            if p.item.id == item_id:
                return p
        raise UnknownItem(f"no item {item_id!r} on bus {self.name!r}")

    def replace(self, item_id: str, replacement: Item) -> PlacedItem:
        """Swap a still-pending item in place, keeping its slot and its id."""
        existing = self.find(item_id)
        if existing.state is ItemState.PLAYING:
            raise ItemAlreadyPlaying(
                f"item {item_id!r} on bus {self.name!r} is already playing — "
                f"replace only works while pending; use stop to interrupt"
            )
        if existing.terminal:
            raise UnknownItem(
                f"item {item_id!r} on bus {self.name!r} already finished "
                f"({existing.state}) — nothing to replace"
            )
        existing.item = existing.item.replaced_by(replacement)
        return existing

    def remove(self, item_id: str, note: str = "removed by caller") -> PlacedItem:
        """Drop a still-pending item. It stays in the list marked DROPPED — SPEC
        §8 wants a dropped item obvious from status output, and an item that
        simply disappears is the definition of a silent failure."""
        existing = self.find(item_id)
        if existing.state is ItemState.PLAYING:
            raise ItemAlreadyPlaying(
                f"item {item_id!r} on bus {self.name!r} is already playing — "
                f"use stop to interrupt it"
            )
        if existing.terminal:
            raise UnknownItem(
                f"item {item_id!r} on bus {self.name!r} already finished "
                f"({existing.state}) — nothing to remove"
            )
        existing.state = ItemState.DROPPED
        existing.note = note
        return existing

    # --- traversal ------------------------------------------------------

    def next_pending(self) -> PlacedItem | None:
        for p in self.placed:
            if p.state is ItemState.PENDING:
                return p
        return None

    @property
    def current(self) -> PlacedItem | None:
        for p in self.placed:
            if p.state is ItemState.PLAYING:
                return p
        return None

    @property
    def pending(self) -> list[PlacedItem]:
        return [p for p in self.placed if p.state is ItemState.PENDING]

    def prune(self, keep: int = 20) -> None:
        """Trim finished history. Playback is a live surface, not a ledger — the
        job history lives in conduct, and Perform keeps only enough to answer
        'what just happened'."""
        done = [p for p in self.placed if p.terminal]
        if len(done) <= keep:
            return
        drop = {id(p) for p in done[: len(done) - keep]}
        self.placed = [p for p in self.placed if id(p) not in drop]

    # --- state ----------------------------------------------------------

    def recompute_state(self, now: float, stall_after_s: float) -> BusState:
        cur = self.current
        if cur is None:
            self.resting_since = None
            self.state = BusState.IDLE
            return self.state

        if not cur.item.duration.open_ended:
            self.resting_since = None
            self.state = BusState.PLAYING
            return self.state

        # Open-ended. Whether that is rest or a hang is the bus's call.
        if self.resting_open_ended:
            self.state = BusState.RESTING
            return self.state
        if self.resting_since is None:
            self.resting_since = now
        held = now - self.resting_since
        self.state = BusState.STALLED if held >= stall_after_s else BusState.PLAYING
        return self.state
