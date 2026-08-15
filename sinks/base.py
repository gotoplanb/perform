"""A sink is where a bus is rendered (SPEC §4).

Shaped after conduct's `providers/base.py`: a narrow ABC, a frozen response
dataclass, and a registry keyed by name. The engine talks only to this
interface, which is what lets the same bus logic drive a sound card, a browser
page, and a Claude conversation.

## Capabilities are declared, not assumed

    Sinks declare their capabilities: which media types they accept, whether
    they are push or pull, and whether they report completion. Buses route to
    sinks.

The engine reads those at *startup* and refuses a bus routed to a sink that
cannot accept its media, loudly, before anything plays. SPEC §8 asks for exactly
this — validate at startup and fail loudly rather than falling back — and the
alternative is discovering at play time that the backdrop bus has been pointed
at the speakers.

## The completion asymmetry, which is the interesting part

Two of the three real sinks report completion and one cannot, ever:

- **local audio** — the mixer knows when a buffer ran out. Exact.
- **visual surface** — the browser posts back when a video ends or an animation
  hits its loop count. That report is what keeps visuals sequencing exactly like
  audio rather than drifting.
- **Claude client** — media arrives only as the result of a tool call, and
  nothing reports back. There is no completion signal and there cannot be one.
  Duration and loop metadata are advisory here, passed through for the client to
  honor, and the sequencing unit is the conversational turn rather than the
  clock.

`reports_completion = False` is therefore not a gap to be filled in later. It is
a property of the medium, and the engine advances such a bus on placement rather
than waiting for a signal that will never come.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from core.item import Item, Media


@dataclass(frozen=True)
class SinkCapabilities:
    accepts: frozenset[Media]
    push: bool  # True: the sink is told. False: the sink is asked.
    reports_completion: bool

    def accepts_media(self, media: Media) -> bool:
        return media in self.accepts


@dataclass(frozen=True)
class Dispatch:
    """What a sink did with an item.

    `expected_duration_s` is what the sink thinks the item will take, where it
    can tell — the engine uses it for `declared` durations and for the stall
    detector, and treats None as "you will hear from me, or you won't".
    """

    item_id: str
    accepted: bool
    expected_duration_s: float | None = None
    detail: str = ""
    warnings: tuple[str, ...] = field(default_factory=tuple)


class BaseSink(ABC):
    """One rendering surface."""

    name: str
    capabilities: SinkCapabilities

    @abstractmethod
    async def start(self, item: Item) -> Dispatch:
        """Begin rendering. Returns immediately; completion arrives separately
        via the engine's completion channel, if this sink reports it at all."""

    @abstractmethod
    async def stop(self, item_id: str | None = None) -> None:
        """Interrupt. `None` means everything this sink is rendering.

        SPEC §3: being unable to shut it up is the most common reason tools like
        this get abandoned, and it matters more here because Wander drives
        Perform faster than a person can react. Every sink implements this, and
        none of them are allowed to make it slow.
        """

    async def close(self) -> None:
        """Release resources at shutdown. Default is nothing to do."""
        return None

    def status(self) -> dict:
        """Whatever this sink wants `/status` to show. Free-form on purpose —
        the audio sink reports xruns, the visual sink reports whether a browser
        is actually attached, and neither is meaningful to the other."""
        return {}
