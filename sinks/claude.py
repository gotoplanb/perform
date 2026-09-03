"""The Claude client sink: pull, and the odd one out.

SPEC §4: *"Pull, and the odd one out. Media can only arrive as the result of a
tool call and nothing reports back, so there is no completion signal and there
cannot be one. Duration and loop metadata are advisory here, passed through for
the client to honor. Its sequencing unit is the conversational turn, not the
clock. Practical limits: images work far better than video, and payload size is
a real constraint."*

Every one of those sentences is a constraint on this file rather than a
suggestion, so:

- **`reports_completion = False`, permanently.** The engine advances a bus
  routed here on dispatch. This is not a TODO — there is no mechanism by which a
  chat client could tell us a picture finished being looked at.
- **`push = False`.** Nothing is sent. Items go into a small buffer, and the
  `next_visual` MCP tool drains it when the model happens to call it. A turn may
  pick up three items or none; both are correct.
- **Images only, by default.** Video is accepted but warned about, because it
  will be large and will mostly not render. The payload limit is enforced here
  rather than discovered downstream as a truncated response.

The buffer is bounded and drops the *oldest*. In a conversation the newest image
is almost always the relevant one, and a backlog from four turns ago is noise.
"""

from __future__ import annotations

import base64
import logging
from collections import deque

from core.item import Item, Media
from library.library import Library
from sinks.base import BaseSink, Dispatch, SinkCapabilities

log = logging.getLogger(__name__)

# Beyond this an image is likely to be truncated or rejected by the client, and
# a silently-truncated image is worse than a refused one.
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024

BUFFER_DEPTH = 8


class ClaudeSink(BaseSink):
    name = "claude"
    capabilities = SinkCapabilities(
        accepts=frozenset({Media.IMAGE, Media.VIDEO}),
        push=False,
        # Not a gap. See the module docstring — there is no signal to be had.
        reports_completion=False,
    )

    def __init__(self, library: Library) -> None:
        self._library = library
        self._pending: deque[dict] = deque(maxlen=BUFFER_DEPTH)
        self.dropped = 0

    async def start(self, item: Item) -> Dispatch:
        resolved = self._library.resolve_visual(item.asset)
        size = resolved.path.stat().st_size

        warnings: list[str] = []
        if resolved.media is Media.VIDEO:
            warnings.append(
                f"{item.label or item.id!r} is video on the claude sink — images work far "
                f"better; most clients will not render this"
            )
        if size > MAX_PAYLOAD_BYTES:
            detail = (
                f"{item.label or item.id!r} is {size // 1024}KB, over the "
                f"{MAX_PAYLOAD_BYTES // 1024}KB payload limit for a tool result — refused "
                f"rather than truncated"
            )
            log.warning("%s", detail)
            return Dispatch(item_id=item.id, accepted=False, detail=detail)

        if len(self._pending) == self._pending.maxlen:
            self.dropped += 1
            warnings.append(
                "claude sink buffer full — dropped the oldest item. Nothing pulls from this "
                "sink until the model calls next_visual, so a backlog means it hasn't."
            )

        self._pending.append(
            {
                "item_id": item.id,
                "label": item.label,
                "media": resolved.media.value,
                "path": str(resolved.path),
                "mime": _mime_for(resolved.path.suffix),
                # Advisory, per SPEC §4: passed through for the client to honor,
                # never enforced here, because the sequencing unit is the
                # conversational turn and not the clock.
                "duration": {
                    "kind": item.duration.kind.value,
                    "repeat": item.duration.repeat,
                    "seconds": item.duration.seconds,
                },
            }
        )

        return Dispatch(
            item_id=item.id,
            accepted=True,
            expected_duration_s=None,
            detail=f"buffered ({len(self._pending)} waiting)",
            warnings=tuple(warnings),
        )

    async def stop(self, item_id: str | None = None) -> None:
        if item_id is None:
            self._pending.clear()
            return
        self._pending = deque(
            (p for p in self._pending if p["item_id"] != item_id), maxlen=BUFFER_DEPTH
        )

    async def stop_bus(self, _bus: str) -> None:
        """Clear the whole buffer regardless of which bus asked.

        The parameter is part of the interface the engine calls and cannot be
        dropped, but it is genuinely unused: this sink holds one buffer for the
        conversation rather than one per bus, because a chat has no lanes.
        Underscored so that is a statement rather than an oversight.
        """
        await self.stop()

    # --- pull -----------------------------------------------------------

    def take(self, limit: int = 1) -> list[dict]:
        """Drain up to `limit` buffered items, newest-relevant order preserved.

        The MCP `next_visual` tool calls this. Base64 is done here rather than at
        placement so a never-pulled item never pays for it.
        """
        out: list[dict] = []
        for _ in range(min(limit, len(self._pending))):
            entry = self._pending.popleft()
            try:
                data = _read_b64(entry["path"])
            except OSError as e:
                log.warning("could not read %s for the claude sink: %s", entry["path"], e)
                continue
            out.append({**entry, "data": data})
        return out

    def status(self) -> dict:
        return {
            "buffered": len(self._pending),
            "dropped": self.dropped,
            "note": "pull sink — never reports completion; duration is advisory",
        }


def _read_b64(path: str) -> str:
    with open(path, "rb") as fh:
        return base64.b64encode(fh.read()).decode("ascii")


def _mime_for(suffix: str) -> str:
    return {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
    }.get(suffix.lower(), "application/octet-stream")
