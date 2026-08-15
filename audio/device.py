"""The boundary between the mix graph and the sound card.

This is the first of the two seams SPEC §8 needs: *"a test run should never make
noise or open a window."* `Device` is a protocol with two implementations —
`PortAudioDevice`, which hands blocks to CoreAudio, and `NullDevice`, which
renders exactly the same blocks through exactly the same mix graph and throws
them away.

The suite runs on `NullDevice`, so it exercises real mixing arithmetic — the
duck ramps, the crossfades, the loop counting — while making no sound at all.
Set `PERFORM_AUDIO_DEVICE=null` to get the same for a headless server.

## The threading rule

`PortAudioDevice` calls its callback on PortAudio's own high-priority thread.
Nothing on that thread may block: no locks held across I/O, no allocation of
anything large, no logging (the handler takes a lock). The mixer is built to
respect this — see the command deque in mixer.py. Break the rule and the
symptom is not an exception, it is a click, which is much harder to trace back.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Protocol

import numpy as np

log = logging.getLogger(__name__)

# frames -> (frames, channels) float32
RenderFn = Callable[[int], np.ndarray]


class Device(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    @property
    def running(self) -> bool: ...


class PortAudioDevice:
    """A real output stream. Opened at startup, closed at shutdown, never
    reopened per item — reopening a CoreAudio stream costs tens of ms and would
    put an audible gap in front of every line."""

    def __init__(
        self,
        render: RenderFn,
        *,
        sample_rate: int,
        channels: int,
        block_size: int,
        device: str | int | None = None,
    ) -> None:
        self._render = render
        self._sample_rate = sample_rate
        self._channels = channels
        self._block_size = block_size
        self._device = device
        self._stream = None

    def start(self) -> None:
        import sounddevice as sd  # noqa: PLC0415 - keep the native lib off the import path

        def _callback(outdata, frames, _time, status) -> None:
            if status:
                # Underruns are the audible failure mode and SPEC §8 wants
                # failures visible. Counted here, surfaced in /status; not
                # logged, because the logging handler takes a lock and this is
                # the real-time thread.
                self.xruns += 1
            outdata[:] = self._render(frames)

        self.xruns = 0
        self._stream = sd.OutputStream(
            samplerate=self._sample_rate,
            channels=self._channels,
            blocksize=self._block_size,
            dtype="float32",
            device=self._device if self._device not in ("", None) else None,
            callback=_callback,
        )
        self._stream.start()
        log.info(
            "audio device open: %s @ %dHz %dch block=%d",
            self._stream.device,
            self._sample_rate,
            self._channels,
            self._block_size,
        )

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
            log.info("audio device closed")

    @property
    def running(self) -> bool:
        return self._stream is not None and self._stream.active


class NullDevice:
    """Renders on demand and discards. The test suite's device, and the one a
    headless run gets.

    It does not spawn a thread: nothing advances until `pull` is called. That is
    what lets a test say "render exactly 100 blocks, now assert" rather than
    sleeping and hoping.
    """

    def __init__(
        self,
        render: RenderFn,
        *,
        sample_rate: int,
        channels: int,
        block_size: int,
    ) -> None:
        self._render = render
        self.sample_rate = sample_rate
        self.channels = channels
        self.block_size = block_size
        self._running = False
        self.blocks_rendered = 0
        self.xruns = 0

    def start(self) -> None:
        self._running = True

    def stop(self) -> None:
        self._running = False

    @property
    def running(self) -> bool:
        return self._running

    # --- test-facing ----------------------------------------------------

    def pull(self, blocks: int = 1) -> np.ndarray:
        """Render `blocks` blocks and return them concatenated.

        Returns the audio so a test can assert on it directly: that two buses
        summed to the sample values they should have, that a fade reached zero,
        that a stopped bus is silent from the block after the stop.
        """
        out = [self._render(self.block_size) for _ in range(blocks)]
        self.blocks_rendered += blocks
        return np.concatenate(out, axis=0) if out else np.zeros((0, self.channels), np.float32)

    def pull_seconds(self, seconds: float) -> np.ndarray:
        blocks = max(1, round(seconds * self.sample_rate / self.block_size))
        return self.pull(blocks)


def resolve_device(name: str) -> str | int | None:
    """Turn the PERFORM_AUDIO_DEVICE setting into something PortAudio accepts.

    Empty means the system default. Digits mean an index. Anything else is a
    name, passed through for PortAudio to match — and if it matches nothing the
    stream open raises at startup, which is the loud failure SPEC §8 asks for
    rather than silently landing on the wrong speakers.
    """
    if not name:
        return None
    return int(name) if name.isdigit() else name
