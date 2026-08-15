"""Local audio: the mixer, wrapped in the sink interface.

SPEC §4: *"Push. Reports completion. The system already provides the device and
the volume control, so there's nothing to build but the routing."*

That is close to literally true of this file. The mixing lives in audio/, the
device lives in audio/device.py, and this is the adapter that turns "start this
item" into a mixer command and lets the engine collect completions. There is no
volume control here and there will not be one — SPEC §2 lists master volume and
transport controls as non-goals, on the grounds that the system already has
them. The only level control in Perform is the *relative* one, and it lives in
the duck matrix.
"""

from __future__ import annotations

import logging

from audio.mixer import Mixer, SetGain, Start, Stop, StopAll
from audio.source import DECLICK_MS
from core.item import Item, Media, Transition
from library.library import Library
from sinks.base import BaseSink, Dispatch, SinkCapabilities

log = logging.getLogger(__name__)


class LocalAudioSink(BaseSink):
    name = "local_audio"
    capabilities = SinkCapabilities(
        accepts=frozenset({Media.AUDIO}),
        push=True,
        reports_completion=True,
    )

    def __init__(self, mixer: Mixer, library: Library, *, fade_ms: float = 400.0) -> None:
        self._mixer = mixer
        self._library = library
        self._fade_ms = fade_ms
        self._device = None  # set by the engine once the device exists

    def attach_device(self, device) -> None:
        self._device = device

    async def start(self, item: Item) -> Dispatch:
        """Resolve the asset and hand a source to the mixer.

        Resolution can be slow on a cache miss — it is an ffmpeg call — and that
        is deliberately *not* on the audio thread. By the time the mixer sees
        this item it is a numpy array.
        """
        resolved = self._library.resolve(item.asset, item.treatment)

        source = self._mixer.make_source(
            item,
            resolved.pcm,
            fade_in_ms=(
                self._fade_ms if item.transition is not Transition.CUT else DECLICK_MS
            ),
        )
        self._mixer.submit(
            Start(
                bus=item.bus,
                source=source,
                transition=item.transition,
                fade_ms=self._fade_ms,
            )
        )

        return Dispatch(
            item_id=item.id,
            accepted=True,
            expected_duration_s=source.total_s,
            detail=f"{resolved.name} ({'hit' if resolved.cache_hit else 'warmed'})",
        )

    async def stop(self, item_id: str | None = None) -> None:
        """Interruption. Always a short fade rather than a hard mute — cutting a
        waveform mid-cycle is a click, and a click on every stop trains people
        not to use stop."""
        self._mixer.submit(StopAll(fade_ms=DECLICK_MS))

    async def stop_bus(self, bus: str) -> None:
        self._mixer.submit(Stop(bus=bus, fade_ms=DECLICK_MS))

    def set_gain(self, bus: str, gain_db: float) -> None:
        self._mixer.submit(SetGain(bus=bus, gain_db=gain_db))

    def status(self) -> dict:
        snap = self._mixer.snapshot
        return {
            "device_running": bool(self._device and self._device.running),
            "xruns": getattr(self._device, "xruns", 0),
            "frames_rendered": snap.get("frames_rendered", 0),
            "buses": snap.get("buses", {}),
        }
