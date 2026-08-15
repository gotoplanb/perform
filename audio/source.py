"""One item's audio, as the mixer sees it: a buffer, a playhead, a loop counter.

Sources are handed to the mixer already normalized — device sample rate, device
channel count, float32, treatment baked in. Everything expensive (decoding,
resampling, filtering) happened at ingest. What is left is slicing an array,
which is what makes it safe to do on the audio thread.

The four duration sources from SPEC §3 all land here, and the mapping is the
whole reason the model is media-agnostic:

    INTRINSIC       play the buffer once. The file knows how long it is.
    REPEAT          play it N times.
    DECLARED        loop until `frames_budget` is exhausted, then stop.
    UNTIL_REPLACED  loop forever. Only a replacement or a stop ends it.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from audio.dsp import linear_ramp
from core.item import DurationKind, Item

# A short ramp on every start and every stop, regardless of the item's declared
# transition. This is not a fade — it is the difference between a clean edit and
# an audible tick, because cutting a waveform at a non-zero sample is a step
# discontinuity and a step discontinuity is broadband click. 5ms is short enough
# to read as instant and long enough to remove it.
DECLICK_MS = 5.0


@dataclass
class Ramp:
    """A linear amplitude ramp consumed a block at a time."""

    start: float
    end: float
    total: int
    done_frames: int = 0

    @property
    def finished(self) -> bool:
        return self.done_frames >= self.total

    def take(self, frames: int, channels: int) -> np.ndarray:
        """The next `frames` of the ramp, held at `end` once exhausted."""
        if self.finished:
            return np.full((frames, channels), self.end, dtype=np.float32)
        n = min(frames, self.total - self.done_frames)
        a = self.start + (self.end - self.start) * (self.done_frames / self.total)
        b = self.start + (self.end - self.start) * ((self.done_frames + n) / self.total)
        head = linear_ramp(n, a, b, channels)
        self.done_frames += n
        if n == frames:
            return head
        tail = np.full((frames - n, channels), self.end, dtype=np.float32)
        return np.concatenate([head, tail], axis=0)


class Source:
    """A playing item. Not thread-safe; only the render thread touches one."""

    def __init__(
        self,
        item: Item,
        pcm: np.ndarray,
        *,
        sample_rate: int,
        channels: int,
        fade_in_ms: float = DECLICK_MS,
    ) -> None:
        self.item = item
        self.pcm = pcm  # (n, channels) float32
        self.sample_rate = sample_rate
        self.channels = channels
        self.playhead = 0
        self.cycles_done = 0
        self.frames_emitted = 0

        d = item.duration
        self.kind = d.kind
        # None means unbounded — REPEAT gets a count, DECLARED gets a frame
        # budget, INTRINSIC is a single pass, UNTIL_REPLACED is neither.
        self.max_cycles: int | None = d.repeat if self.kind is DurationKind.REPEAT else None
        if self.kind is DurationKind.INTRINSIC:
            self.max_cycles = 1
        self.frames_budget: int | None = (
            int(d.seconds * sample_rate) if self.kind is DurationKind.DECLARED else None
        )
        self.loops = self.kind in (
            DurationKind.REPEAT,
            DurationKind.DECLARED,
            DurationKind.UNTIL_REPLACED,
        )

        self.envelope = Ramp(0.0, 1.0, self._ms(fade_in_ms))
        self._stopping: Ramp | None = None
        self.finished = False

    def _ms(self, ms: float) -> int:
        return max(1, int(self.sample_rate * ms / 1000.0))

    # --- lifecycle ------------------------------------------------------

    def begin_stop(self, fade_ms: float = DECLICK_MS) -> None:
        """Start fading out. The source keeps rendering until the ramp lands, so
        an interruption is immediate to the ear without being a click."""
        if self._stopping is None:
            self._stopping = Ramp(1.0, 0.0, self._ms(fade_ms))

    @property
    def stopping(self) -> bool:
        return self._stopping is not None

    @property
    def elapsed_s(self) -> float:
        return self.frames_emitted / self.sample_rate

    @property
    def total_s(self) -> float | None:
        """Wall length, where it is knowable. None for open-ended items — which
        is exactly the thing `/status` needs to say out loud rather than guess."""
        one = len(self.pcm) / self.sample_rate
        if self.kind is DurationKind.INTRINSIC:
            return one
        if self.kind is DurationKind.REPEAT and self.max_cycles:
            return one * self.max_cycles
        if self.kind is DurationKind.DECLARED and self.frames_budget:
            return self.frames_budget / self.sample_rate
        return None

    # --- rendering ------------------------------------------------------

    def read(self, frames: int) -> np.ndarray:
        """The next `frames` of audio. Sets `finished` when there is no more.

        Always returns a full block; the tail is zero-padded. Returning short
        blocks would push the padding decision onto the mixer, which would then
        have to know about loop boundaries.
        """
        if self.finished:
            return np.zeros((frames, self.channels), dtype=np.float32)

        out = np.zeros((frames, self.channels), dtype=np.float32)
        written = 0
        n = len(self.pcm)

        while written < frames:
            if n == 0:
                self.finished = True
                break

            remaining_in_budget = frames - written
            if self.frames_budget is not None:
                left = self.frames_budget - self.frames_emitted - written
                if left <= 0:
                    self.finished = True
                    break
                remaining_in_budget = min(remaining_in_budget, left)

            take = min(remaining_in_budget, n - self.playhead)
            out[written : written + take] = self.pcm[self.playhead : self.playhead + take]
            written += take
            self.playhead += take

            if self.playhead >= n:
                self.cycles_done += 1
                self.playhead = 0
                if not self.loops or (
                    self.max_cycles is not None and self.cycles_done >= self.max_cycles
                ):
                    self.finished = True
                    break

        self.frames_emitted += written

        out *= self.envelope.take(frames, self.channels)
        if self._stopping is not None:
            out *= self._stopping.take(frames, self.channels)
            if self._stopping.finished:
                self.finished = True

        return out
