"""Startup and shutdown, in order, with the validation that has to happen first.

Same shape as conduct's `lifespan.py` — an `asynccontextmanager` that wires
telemetry, builds the long-lived objects, stashes them on `app.state`, installs
a SIGUSR1 reload handler, and tears everything down on the way out.

## Startup order is a design decision, not an accident

SPEC §8: *"Validate at startup and fail loudly rather than falling back. A
silent fallback to a default voice means two participants sound identical and
the listener has no idea why."*

So everything that can be checked is checked before the device opens and before
the first request is served:

    1. telemetry            (never fatal — traces must not break playback)
    2. bus config           parse and validate the duck matrix
    3. library              packs, manifests, media types, built-in tones
    4. sinks                register, then check every bus routes somewhere legal
    5. the system voice     an unknown `say` voice aborts the boot
    6. the audio device     open it now, so a missing device is a boot failure
    7. the engine           bus tasks start last, when everything they need exists

A failure at any of 2-6 raises out of the lifespan and uvicorn exits non-zero
with the reason. That is the intended behavior: a Perform that booted "fine" and
is silently unable to speak is the worst outcome available.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from audio import speech
from audio.device import NullDevice, PortAudioDevice, resolve_device
from audio.mixer import Mixer
from config.buses import load_buses
from config.settings import get_settings
from core.engine import Engine
from library.library import Library
from library.store import Store
from observability import logs, tracing
from sinks.claude import ClaudeSink
from sinks.local_audio import LocalAudioSink
from sinks.registry import SinkRegistry
from sinks.visual import VisualSink

log = logging.getLogger(__name__)


def build_engine(settings, *, device_override: str | None = None):
    """Construct the whole graph. Separated from the lifespan so tests can build
    the same thing on a NullDevice without an ASGI app around it."""
    buses = load_buses(settings.buses_config)

    store = Store(settings.library_db)
    library = Library(
        store=store,
        blobs_dir=Path(settings.blobs_dir),
        packs_dir=Path(settings.packs_dir),
        sample_rate=settings.sample_rate,
        channels=settings.channels,
        cache_max_mb=settings.cache_max_mb,
    )
    library.bootstrap()

    mixer = Mixer(
        sample_rate=settings.sample_rate,
        channels=settings.channels,
        block_size=settings.block_size,
    )

    sinks = SinkRegistry()
    audio_sink = LocalAudioSink(mixer, library)
    sinks.register(audio_sink)
    sinks.register(VisualSink(library))
    sinks.register(ClaudeSink(library))

    # The check that turns a config mistake into a boot failure instead of
    # silence forty seconds into a scene.
    sinks.validate_routes(buses)

    engine = Engine(
        buses=buses,
        sinks=sinks,
        mixer=mixer,
        stall_after_s=settings.stall_after_s,
    )

    name = device_override if device_override is not None else settings.audio_device
    if name == "null":
        device = NullDevice(
            mixer.render,
            sample_rate=settings.sample_rate,
            channels=settings.channels,
            block_size=settings.block_size,
        )
        log.warning(
            "PERFORM_AUDIO_DEVICE=null — the mix graph runs and the audio is discarded. "
            "Nothing will be audible."
        )
    else:
        device = PortAudioDevice(
            mixer.render,
            sample_rate=settings.sample_rate,
            channels=settings.channels,
            block_size=settings.block_size,
            device=resolve_device(name),
        )

    engine.attach_device(device)
    audio_sink.attach_device(device)
    return engine, library, store, device


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # First, before anything can fail — otherwise the startup validation below
    # would abort with its reason going nowhere.
    logs.init_console_logging()
    # The status page refreshes every 2s; without this its access lines drown
    # out everything worth reading in the log.
    logs.quiet_access_logs()

    tracing.init_tracing(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_endpoint,
        role="api",
    )
    logs.init_logging(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_endpoint,
        role="api",
    )

    # An unknown voice fails the boot. See the module docstring — this is the
    # exact failure SPEC §8 names, and conduct's TTS gets it wrong today
    # (a bad voice is accepted with a 202 and fails later inside the worker).
    speech.validate_voice(settings.say_voice)

    engine, library, store, device = build_engine(settings)

    device.start()
    await engine.start()

    app.state.engine = engine
    app.state.library = library
    app.state.settings = settings

    _install_reload_handler(engine)

    log.info(
        "perform ready on http://%s:%d — surface at /surface, status at /ui",
        settings.host,
        settings.port,
    )
    try:
        yield
    finally:
        await engine.close()
        device.stop()
        store.close()
        tracing.shutdown()
        log.info("perform stopped")


def _install_reload_handler(engine: Engine) -> None:
    """SIGUSR1 reloads config/buses.yaml without dropping what is playing.

    USR1 and not HUP because uvicorn takes HUP for itself — the same discovery
    conduct made when it wired up its pricing reload. A reload that fails leaves
    the running config in place and logs why; swapping in a half-parsed bus set
    would be worse than not reloading.
    """
    settings = get_settings()

    def _reload() -> None:
        try:
            buses = load_buses(settings.buses_config)
            engine.sinks.validate_routes(buses)
        except Exception as e:  # noqa: BLE001 - a bad edit must not kill the server
            log.exception("bus reload failed — keeping the running config")
            engine.warn("*", "reload", f"bus reload failed, config unchanged: {e}")
            return

        # Carry the live lists across so a reload does not drop a queued scene.
        for bus in buses:
            existing = engine.buses.get(bus.name)
            if existing is not None:
                bus.placed = existing.placed
                bus.state = existing.state
                bus.resting_since = existing.resting_since
        gone = set(engine.buses) - {b.name for b in buses}
        if gone:
            engine.warn(
                "*",
                "reload",
                f"bus(es) {sorted(gone)} removed by reload — their items are orphaned",
            )
        engine.buses = {b.name: b for b in buses}
        engine.configure_mixer()
        log.warning("buses reloaded: %s", ", ".join(engine.buses))

    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGUSR1, _reload)
        log.info("SIGUSR1 handler installed — `make reload-buses` reloads the bus config")
    except RuntimeError as e:
        # RuntimeError alone: NotImplementedError subclasses it, and that is the
        # one a loop without signal support raises.
        log.warning("could not install SIGUSR1 handler (%s) — reload unavailable", e)
