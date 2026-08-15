"""Perform's exception surface.

One small hierarchy, rooted at a plain Exception subclass, in the style conduct
uses for `TTSEngineError → VoiceNotFound` and `ProviderError → ProviderTimeout`.
Kept in one module rather than scattered per-boundary only because Perform is
small enough that the split would be noise.

Every message here is expected to carry its own remediation. SPEC §8 asks for
failures that are visible and never silent, and the tone to match is wander's

    ConductError("CONDUCT_TOKEN not set (and no token passed) — mint a client
                 API key (cdt_<random>) per conduct/docs/auth.md")

i.e. say what went wrong *and* what to do about it, in the exception itself.
"""

from __future__ import annotations


class PerformError(Exception):
    """Base for everything Perform raises on purpose."""


class ConfigError(PerformError):
    """A bus, sink, or voice is configured in a way that cannot work.

    Always raised during startup, never at play time. SPEC §8: validate at
    startup and fail loudly rather than falling back.
    """


class AssetError(PerformError):
    """An asset could not be resolved, read, or normalized."""


class UnknownAsset(AssetError):
    """No pack declares this logical name, and it isn't a readable path."""


class IngestError(AssetError):
    """ffmpeg could not turn the source into playable PCM."""


class UnknownBus(PerformError):
    """No bus by that name. Buses are fixed at startup and never created at runtime."""


class UnknownItem(PerformError):
    """No item by that id on any bus, or it has already been played."""


class ItemAlreadyPlaying(PerformError):
    """Tried to replace or remove an item that is currently rendering.

    SPEC §3 makes items addressable *while pending*; once a bus has started an
    item the only lever is interruption, which is `stop`, not `replace`.
    """


class SinkRejected(PerformError):
    """A sink was handed media it declared it does not accept."""


class SpeechError(PerformError):
    """The system voice could not produce audio."""


class VoiceNotFound(SpeechError):
    """The configured `say` voice isn't installed on this machine."""
