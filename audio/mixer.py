"""The mix graph: per-bus voices, gain, duck envelopes, summing.

Buses run concurrently and items on one bus play serially (SPEC §3), so the
mixer is one voice per bus, each holding at most one playing source plus
whatever is still fading out behind it.

## Crossing the thread boundary without a lock

`render` runs on PortAudio's callback thread. Everything else — placing items,
stopping buses, reloading config — runs on the asyncio loop. They communicate
through two `collections.deque`s, whose `append` and `popleft` are atomic in
CPython, so neither side ever waits on the other:

    asyncio  --command deque-->  render        (start, stop, gain, config)
    render   --done deque---->   asyncio       (item ids that finished)

The engine drains the done deque on a short poll rather than being signalled,
because signalling means `call_soon_threadsafe`, and that takes a lock on the
thread that must never take one. The cost is up to one poll interval of latency
between "the line ended" and "start the next one", which is ~10ms and inaudible.

Everything the render thread needs is preallocated or sliced; nothing here
decodes, resamples, filters, allocates a large buffer, or logs.

## What the mixer deliberately does not do

No master volume and no transport controls — the system already has those and
SPEC §2 lists them as non-goals. The only level control here is the *relative*
one: how far one bus pushes another down, which no system volume control can
express.
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from audio.dsp import approach_db, gain_ramp, ramp_step_db, soft_clip
from audio.source import DECLICK_MS, Source
from core.bus import DuckRule
from core.item import Transition

log = logging.getLogger(__name__)

DEFAULT_FADE_MS = 400.0


# --- commands ---------------------------------------------------------------


@dataclass(frozen=True)
class Start:
    bus: str
    source: Source
    transition: Transition = Transition.CUT
    fade_ms: float = DEFAULT_FADE_MS


@dataclass(frozen=True)
class Stop:
    bus: str
    fade_ms: float = DECLICK_MS


@dataclass(frozen=True)
class StopAll:
    fade_ms: float = DECLICK_MS


@dataclass(frozen=True)
class SetGain:
    bus: str
    gain_db: float


@dataclass(frozen=True)
class Configure:
    """Replace the bus set. Sent at startup and on every SIGUSR1 reload."""

    gains: dict[str, float]
    ducks: dict[str, list[DuckRule]]


Command = Start | Stop | StopAll | SetGain | Configure


@dataclass
class Completion:
    """Why a source stopped rendering. The engine turns this into an item state.

    `natural` separates "the line finished" from "someone hit stop", which is
    the difference between advancing the bus and leaving it idle.
    """

    bus: str
    item_id: str
    natural: bool
    cycles: int
    elapsed_s: float


@dataclass
class BusVoice:
    name: str
    gain_db: float = 0.0
    ducks: list[DuckRule] = field(default_factory=list)
    source: Source | None = None
    # Sources still fading out behind the current one. A dissolve holds two;
    # a cut holds one for ~5ms.
    outgoing: list[Source] = field(default_factory=list)
    # A source waiting for the outgoing ones to clear. This is what makes FADE
    # sequential (out, then in) where DISSOLVE overlaps.
    waiting: tuple[Source, float] | None = None
    # Live duck state, in dB relative to gain_db. 0 when nothing is ducking us.
    # `prev_duck_db` is last block's value: the gain applied to a block is
    # interpolated from it to the current one, so a duck never steps.
    duck_db: float = 0.0
    prev_duck_db: float = 0.0
    duck_target_db: float = 0.0
    last_rule: DuckRule | None = None

    @property
    def has_audio(self) -> bool:
        """Whether this bus is currently making sound, which is what triggers a
        duck on its targets."""
        return self.source is not None and not self.source.finished


def _deepest_duck(
    target: str, active: list[BusVoice]
) -> tuple[float, DuckRule | None]:
    """The deepest duck any currently-sounding bus applies to `target`.

    Deepest rather than the sum: two -12dB ducks meaning -24dB would be a
    surprise every time. Returns (0.0, None) when nothing is ducking it.
    """
    deepest = 0.0
    rule: DuckRule | None = None
    for ducker in active:
        if ducker.name == target:
            continue  # a bus never ducks itself
        for candidate in ducker.ducks:
            if candidate.target == target and candidate.depth_db < deepest:
                deepest, rule = candidate.depth_db, candidate
    return deepest, rule


class Mixer:
    def __init__(self, *, sample_rate: int, channels: int, block_size: int) -> None:
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self.block_ms = 1000.0 * block_size / sample_rate

        self._voices: dict[str, BusVoice] = {}
        self._commands: deque[Command] = deque()
        self._done: deque[Completion] = deque()
        self.frames_rendered = 0
        # Plain dict, replaced wholesale each block. Reading a reference is
        # atomic, so /status never sees a half-written snapshot and never has
        # to take a lock to look.
        self.snapshot: dict[str, Any] = {}

    # --- asyncio side ---------------------------------------------------

    def submit(self, command: Command) -> None:
        self._commands.append(command)

    def drain_completions(self) -> list[Completion]:
        out: list[Completion] = []
        while self._done:
            out.append(self._done.popleft())
        return out

    def make_source(self, item, pcm: np.ndarray, fade_in_ms: float = DECLICK_MS) -> Source:
        return Source(
            item,
            pcm,
            sample_rate=self.sample_rate,
            channels=self.channels,
            fade_in_ms=fade_in_ms,
        )

    # --- render thread --------------------------------------------------

    def render(self, frames: int) -> np.ndarray:
        self._apply_commands()

        out = np.zeros((frames, self.channels), dtype=np.float32)
        raw: dict[str, np.ndarray] = {}

        for voice in self._voices.values():
            block = np.zeros((frames, self.channels), dtype=np.float32)

            # list() is load-bearing: finished sources are removed below.
            for src in list(voice.outgoing):
                block += src.read(frames)
                if src.finished:
                    voice.outgoing.remove(src)
                    self._done.append(self._completion(voice.name, src, natural=False))

            # A source held for a sequential fade starts once the lane is clear.
            if voice.source is None and voice.waiting is not None and not voice.outgoing:
                src, fade_ms = voice.waiting
                src.envelope.total = max(1, int(self.sample_rate * fade_ms / 1000.0))
                voice.source = src
                voice.waiting = None

            if voice.source is not None:
                block += voice.source.read(frames)
                if voice.source.finished:
                    natural = not voice.source.stopping
                    self._done.append(self._completion(voice.name, voice.source, natural=natural))
                    voice.source = None

            raw[voice.name] = block

        self._update_ducks()

        for name, block in raw.items():
            voice = self._voices[name]
            start_db = voice.gain_db + voice.prev_duck_db
            end_db = voice.gain_db + voice.duck_db
            out += block * gain_ramp(frames, start_db, end_db, self.channels)

        out = soft_clip(out)
        self.frames_rendered += frames
        self._publish()
        return out

    def _completion(self, bus: str, src: Source, *, natural: bool) -> Completion:
        return Completion(
            bus=bus,
            item_id=src.item.id,
            natural=natural,
            cycles=src.cycles_done,
            elapsed_s=src.elapsed_s,
        )

    def _apply_commands(self) -> None:
        """Drain the command deque. One dispatch, three handlers."""
        while self._commands:
            cmd = self._commands.popleft()
            if isinstance(cmd, Configure):
                self._reconfigure(cmd)
            elif isinstance(cmd, StopAll):
                for voice in self._voices.values():
                    self._stop_voice(voice, cmd.fade_ms)
            else:
                self._apply_to_bus(cmd)

    def _reconfigure(self, cmd: Configure) -> None:
        for name, gain in cmd.gains.items():
            voice = self._voices.setdefault(name, BusVoice(name=name))
            voice.gain_db = gain
            voice.ducks = cmd.ducks.get(name, [])
        # A bus that vanished from the config keeps playing what it has but
        # stops accepting work; the engine refuses to route to it.
        for gone in set(self._voices) - set(cmd.gains):
            self._voices[gone].ducks = []

    def _apply_to_bus(self, cmd: Start | Stop | SetGain) -> None:
        voice = self._voices.get(cmd.bus)
        if voice is None:
            return  # unknown bus: rejected at the API long before here

        if isinstance(cmd, SetGain):
            voice.gain_db = cmd.gain_db
        elif isinstance(cmd, Stop):
            self._stop_voice(voice, cmd.fade_ms)
        elif isinstance(cmd, Start):
            self._start(voice, cmd)

    def _stop_voice(self, voice: BusVoice, fade_ms: float) -> None:
        if voice.waiting is not None:
            src, _ = voice.waiting
            voice.waiting = None
            self._done.append(self._completion(voice.name, src, natural=False))
        if voice.source is not None:
            voice.source.begin_stop(fade_ms)
            voice.outgoing.append(voice.source)
            voice.source = None
        for src in voice.outgoing:
            src.begin_stop(fade_ms)

    def _start(self, voice: BusVoice, cmd: Start) -> None:
        """Begin an item, honoring its transition.

        CUT      declick out, declick in. The default, and right for dialogue.
        DISSOLVE overlap: old fades out while new fades in. A crossfade.
        FADE     sequential: old fades out, *then* new fades in. The difference
                 from DISSOLVE matters when both are music-like and overlapping
                 them would be muddy.
        """
        if cmd.transition is Transition.DISSOLVE:
            if voice.source is not None:
                voice.source.begin_stop(cmd.fade_ms)
                voice.outgoing.append(voice.source)
                voice.source = None
            cmd.source.envelope.total = max(1, int(self.sample_rate * cmd.fade_ms / 1000.0))
            voice.source = cmd.source
            return

        if cmd.transition is Transition.FADE and voice.source is not None:
            voice.source.begin_stop(cmd.fade_ms)
            voice.outgoing.append(voice.source)
            voice.source = None
            voice.waiting = (cmd.source, cmd.fade_ms)
            return

        # CUT, or FADE onto a silent bus.
        self._stop_voice(voice, DECLICK_MS)
        voice.source = cmd.source

    def _update_ducks(self) -> None:
        """Recompute every bus's duck depth for this block.

        Note the direction. SPEC §3's sentence — "an ambience bus ducks a voice
        bus by a configured amount so words stay intelligible over a siren" —
        reads either way, but the *purpose* clause settles it: for words to stay
        intelligible over a siren, the siren has to move. So the shipped
        config/buses.yaml has voice duck ambience and backdrop. The mechanism
        here is symmetric and expresses whichever direction the config asks for.

        Overlapping ducks take the deepest, not the sum: two -12dB ducks meaning
        -24dB would be a surprise every time.
        """
        active = [v for v in self._voices.values() if v.has_audio]

        for voice in self._voices.values():
            voice.prev_duck_db = voice.duck_db
            deepest, rule = _deepest_duck(voice.name, active)
            voice.duck_target_db = deepest
            if rule is not None:
                voice.last_rule = rule
            self._ramp_duck(voice, deepest, rule)

    def _ramp_duck(
        self, voice: BusVoice, deepest: float, rule: DuckRule | None
    ) -> None:
        """Move one bus's duck level toward its target for this block.

        `last_rule` is remembered so a *release* uses the timing of the rule that
        applied the duck — by the time a bus is recovering, the rule that ducked
        it is no longer active and would otherwise be unavailable.
        """
        timing = rule or voice.last_rule
        if timing is None:
            voice.duck_db = deepest
            return

        # Going down is attack, coming back up is release. Ramping in dB is what
        # makes the recovery sound natural rather than lurching.
        ms = timing.attack_ms if deepest < voice.duck_db else timing.release_ms
        step = ramp_step_db(timing.depth_db, ms, self.block_ms)
        voice.duck_db = approach_db(voice.duck_db, deepest, step)

    def _publish(self) -> None:
        self.snapshot = {
            "frames_rendered": self.frames_rendered,
            "buses": {
                name: {
                    "item_id": v.source.item.id if v.source else None,
                    "label": v.source.item.label if v.source else "",
                    "elapsed_s": round(v.source.elapsed_s, 3) if v.source else None,
                    "total_s": (
                        round(v.source.total_s, 3) if v.source and v.source.total_s else None
                    ),
                    "cycles": v.source.cycles_done if v.source else 0,
                    "gain_db": round(v.gain_db, 2),
                    "duck_db": round(v.duck_db, 2),
                    "effective_db": round(v.gain_db + v.duck_db, 2),
                    "tail": len(v.outgoing),
                }
                for name, v in self._voices.items()
            },
        }
