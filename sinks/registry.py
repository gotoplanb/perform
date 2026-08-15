"""Sink lookup by name, with the startup check that makes routing safe.

Mirrors conduct's `providers/registry.py` — register by name, get by name, and
one place that knows what exists. The addition here is `validate_routes`, which
is where SPEC §8's "validate at startup and fail loudly" actually happens for
the bus/sink relationship.
"""

from __future__ import annotations

import logging

from core.errors import ConfigError
from core.item import Media
from sinks.base import BaseSink

log = logging.getLogger(__name__)


class SinkRegistry:
    def __init__(self) -> None:
        self._sinks: dict[str, BaseSink] = {}

    def register(self, sink: BaseSink) -> None:
        self._sinks[sink.name] = sink
        caps = sink.capabilities
        log.info(
            "sink registered: %s accepts=%s %s completion=%s",
            sink.name,
            ",".join(sorted(m.value for m in caps.accepts)),
            "push" if caps.push else "pull",
            "yes" if caps.reports_completion else "no",
        )

    def get(self, name: str) -> BaseSink:
        try:
            return self._sinks[name]
        except KeyError:
            raise ConfigError(
                f"no sink named {name!r} — registered sinks are "
                f"{sorted(self._sinks)}"
            ) from None

    def has(self, name: str) -> bool:
        return name in self._sinks

    def __iter__(self):
        return iter(self._sinks.values())

    def validate_routes(self, buses) -> None:
        """Every bus must route to a sink that accepts what it will carry.

        Called once at startup, before the device opens. A bus whose sink cannot
        take its media is a configuration error, not a runtime one, and finding
        out at play time means the failure surfaces as silence during a scene.
        """
        problems: list[str] = []
        for bus in buses:
            if not self.has(bus.sink):
                problems.append(
                    f"bus {bus.name!r} routes to sink {bus.sink!r}, which does not exist "
                    f"(registered: {sorted(self._sinks)})"
                )
                continue
            caps = self.get(bus.sink).capabilities
            # A bus carries whatever items it is given, so the check is that the
            # sink accepts *something* — the per-item check happens on placement,
            # where the media type is actually known.
            if not caps.accepts:
                problems.append(f"sink {bus.sink!r} accepts no media at all")

            for rule in bus.ducks:
                if rule.target not in {b.name for b in buses}:
                    problems.append(
                        f"bus {bus.name!r} ducks {rule.target!r}, which is not a bus "
                        f"(buses: {sorted(b.name for b in buses)})"
                    )
                if rule.target == bus.name:
                    problems.append(f"bus {bus.name!r} ducks itself")

        if problems:
            raise ConfigError(
                "bus configuration is not routable:\n  - " + "\n  - ".join(problems)
            )

    def check_item(self, sink_name: str, media: Media) -> None:
        """Per-item capability check, run when an item is placed rather than
        when it plays — so the caller gets a 4xx at the point of the mistake
        instead of silence forty seconds later."""
        caps = self.get(sink_name).capabilities
        if not caps.accepts_media(media):
            from core.errors import SinkRejected  # noqa: PLC0415 - avoid an import cycle

            raise SinkRejected(
                f"sink {sink_name!r} does not accept {media} "
                f"(accepts: {sorted(m.value for m in caps.accepts)})"
            )
