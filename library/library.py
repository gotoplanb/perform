"""The asset library: one resolve path for everything playable.

This is the module SPEC §5 is describing when it says *"one path for all assets
... 'pre-generating a theme' then becomes 'warming the cache before the scene,'
using the same code path as just-in-time delivery, and it degrades gracefully."*

`resolve()` is that path. It is called by `POST /assets/warm` ahead of a scene
and by the engine microseconds before an item plays, and it does the same thing
either way: look for a warm blob, ingest one if there isn't, hand back PCM. A
warm cache makes it a dictionary lookup. A cold one makes it an ffmpeg call. No
other difference, which is what makes an unfinished warm cost you a few slow
items rather than a failure.

Resolution order for a logical name:

    1. `names` table  — declared by a pack, or by the built-in tones
    2. an alias       — same thing, one indirection
    3. nothing        — UnknownAsset, and the message lists near misses,
                        because a typo'd asset name is the single most likely
                        reason a scene goes quiet

A path, by contrast, resolves directly: that is Wander handing over something
Conduct just generated, which by definition no pack declares.
"""

from __future__ import annotations

import difflib
import logging
import shutil
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from core.errors import AssetError, UnknownAsset
from core.item import AssetRef, Media, Treatment
from library import ingest, tones
from library.packs import discover
from library.store import Store

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Resolved:
    """A playable asset: the PCM the mixer slices, plus what we know about it."""

    key: str
    name: str
    media: Media
    treatment: Treatment
    pcm: np.ndarray
    duration_s: float
    cache_hit: bool
    source: str


@dataclass(frozen=True)
class ResolvedVisual:
    """A still or a video, ready to serve to the browser.

    `path` is where the bytes are and `url_path` is what the surface fetches —
    the same split conduct keeps between `file_path` and `url_path` on
    MediaResponse, so the storage backend can move without touching callers.
    """

    key: str
    name: str
    media: Media
    path: Path
    url_path: str
    audio_stripped: bool
    warnings: tuple[str, ...]


class Library:
    def __init__(
        self,
        *,
        store: Store,
        blobs_dir: Path,
        packs_dir: Path,
        sample_rate: int,
        channels: int,
        cache_max_mb: int = 0,
    ) -> None:
        self.store = store
        self.blobs_dir = Path(blobs_dir)
        self.packs_dir = Path(packs_dir)
        self.sample_rate = sample_rate
        self.channels = channels
        self.cache_max_bytes = cache_max_mb * 1024 * 1024
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        # Decoded PCM, keyed by blob key. The disk cache saves the ffmpeg call;
        # this saves the file read. Bounded by nothing on purpose — the whole
        # library of a scene is tens of megabytes, and an eviction here would
        # be a stutter in the middle of a line.
        self._pcm: dict[str, np.ndarray] = {}
        self.warnings: list[str] = []

    # --- startup --------------------------------------------------------

    def bootstrap(self) -> None:
        """Register the built-in tones and scan for packs. Called once, at boot.

        A pack that fails validation raises out of here and aborts the boot
        (SPEC §8: validate at startup and fail loudly rather than falling back).
        A pack that is merely *absent* is fine — the tones alone are enough for
        the notification case.
        """
        for name, path, _desc in tones.generate(
            self.blobs_dir / "tones", sample_rate=self.sample_rate, channels=self.channels
        ):
            key = ingest.content_key(
                path, Treatment.CLEAN, sample_rate=self.sample_rate, channels=self.channels
            )
            info = _wav_info(path)
            self.store.upsert_blob(
                key=key,
                path=str(path),
                media=Media.AUDIO.value,
                treatment=Treatment.CLEAN.value,
                duration_s=info["duration_s"],
                sample_rate=self.sample_rate,
                channels=self.channels,
                size_bytes=info["bytes"],
                pinned=True,  # built-ins never evict
                source="built-in tone",
            )
            self.store.upsert_name(
                name=name,
                pack=tones.PACK_ID,
                media=Media.AUDIO.value,
                source_path=str(path),
                blob_key=key,
            )

        for pack in discover(self.packs_dir):
            # Re-scan from scratch: a pack whose manifest dropped an asset
            # should stop resolving it, not keep a stale row alive.
            self.store.forget_pack(pack.id)
            for asset in pack.assets:
                self.store.upsert_name(
                    name=asset.name,
                    pack=asset.pack,
                    media=asset.media.value,
                    source_path=str(asset.path),
                    alias_of=asset.alias_of,
                )

    # --- resolution -----------------------------------------------------

    def resolve(self, ref: AssetRef, treatment: Treatment = Treatment.CLEAN) -> Resolved:
        """Get playable PCM for an asset, warming the cache if it is cold."""
        source_path, name, declared_media = self._locate(ref)

        already = self._as_cached_blob(source_path)
        if already is not None:
            return already

        key = ingest.content_key(
            source_path, treatment, sample_rate=self.sample_rate, channels=self.channels
        )

        cached = self.store.get_blob(key)
        if cached is not None and Path(cached.path).is_file():
            pcm = self._pcm.get(key)
            if pcm is None:
                pcm = ingest.load_pcm(Path(cached.path), channels=self.channels)
                self._pcm[key] = pcm
            self.store.touch_blob(key)
            if name:
                self.store.bind_name(name, key)
            return Resolved(
                key=key,
                name=name or str(source_path),
                media=Media(cached.media),
                treatment=treatment,
                pcm=pcm,
                duration_s=cached.duration_s,
                cache_hit=True,
                source=cached.source or str(source_path),
            )

        return self._warm_one(
            source_path, key, name, declared_media, treatment, pinned=bool(name)
        )

    def _as_cached_blob(self, source_path: Path) -> Resolved | None:
        """Short-circuit for a path that is already a warm-cache blob.

        `POST /speak` places the *normalized* recording rather than the `say`
        AIFF, because the AIFF is a temp file that gets deleted. Without this
        check, resolving that path would hash the normalized WAV, get a
        different key than the one it is stored under, and run it through ffmpeg
        a second time — producing a duplicate cache entry for every utterance
        and re-normalizing something that is already in exactly the right
        format. Caught by seeing two blobs for one spoken line.

        Anything already sitting under `blobs_dir` under its own key is by
        construction at the device rate, in the device's channel count, with its
        treatment baked in. There is nothing left to do but read it.
        """
        if source_path.parent != self.blobs_dir:
            return None
        blob = self.store.get_blob(source_path.stem)
        if blob is None or not Path(blob.path).is_file():
            return None

        pcm = self._pcm.get(blob.key)
        if pcm is None:
            pcm = ingest.load_pcm(Path(blob.path), channels=self.channels)
            self._pcm[blob.key] = pcm
        self.store.touch_blob(blob.key)

        return Resolved(
            key=blob.key,
            name=blob.source or source_path.name,
            media=Media(blob.media),
            treatment=Treatment(blob.treatment),
            pcm=pcm,
            duration_s=blob.duration_s,
            cache_hit=True,
            source=blob.source or str(source_path),
        )

    def resolve_visual(self, ref: AssetRef) -> ResolvedVisual:
        """Get a servable file for a still or a video.

        Visual media never enters the mixer — SPEC §4 keeps audio and video on
        separate sinks, and the browser decodes video far better than we would.
        So this path does not normalize; it content-addresses the original,
        links it under blobs_dir, and hands back a key the `/media` route can
        serve.

        Linking rather than serving the original path in place is what lets the
        media route enforce a single rule — *resolves under blobs_dir* — instead
        of maintaining a list of trusted directories. A hardlink costs nothing;
        the copy fallback is for a packs dir on another filesystem.
        """
        source_path, name, declared_media = self._locate(ref)
        media = declared_media or ingest.media_of(source_path)
        if media is Media.AUDIO:
            raise AssetError(
                f"{name or source_path.name} is audio — route it to a bus whose sink "
                f"is local_audio, not to the visual surface"
            )

        key = ingest.content_key(
            source_path, Treatment.CLEAN, sample_rate=0, channels=0
        )
        suffix = source_path.suffix.lower() or ".bin"
        dest = self.blobs_dir / "visual" / f"{key}{suffix}"

        warnings: list[str] = []
        # SPEC §4: a video arriving with a live audio track "will fail silently
        # rather than error — strip or mute at the sink, and log when you do."
        # The browser always gets `muted`, so it is already silent; what was
        # missing is anyone being told, which is what this warning is for.
        stripped = media is Media.VIDEO and ingest.has_audio_track(source_path)
        if stripped:
            msg = (
                f"{name or source_path.name} arrived with a live audio track — the "
                f"surface plays it muted (audio and video stay separate, SPEC §4). "
                f"Its sound will not be heard; put it on an audio bus if you want it."
            )
            warnings.append(msg)
            self.warnings.append(msg)
            log.warning("%s", msg)

        if not dest.is_file():
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                dest.hardlink_to(source_path)
            except OSError:
                shutil.copy2(source_path, dest)

        self.store.upsert_blob(
            key=key,
            path=str(dest),
            media=media.value,
            treatment=Treatment.CLEAN.value,
            duration_s=0.0,
            sample_rate=0,
            channels=0,
            size_bytes=dest.stat().st_size,
            pinned=bool(name),
            source=str(source_path),
        )
        if name:
            self.store.bind_name(name, key)

        return ResolvedVisual(
            key=key,
            name=name or str(source_path),
            media=media,
            path=dest,
            url_path=f"/media/{key}{suffix}",
            audio_stripped=stripped,
            warnings=tuple(warnings),
        )

    def blob_path(self, filename: str) -> Path:
        """Resolve a `/media/{filename}` request, refusing anything that leaves
        the blob directory. Same guard conduct puts on `/output/{file}`."""
        root = (self.blobs_dir / "visual").resolve()
        candidate = (root / filename).resolve()
        if not candidate.is_relative_to(root) or not candidate.is_file():
            raise UnknownAsset(f"no media at {filename!r}")
        return candidate

    def warm(self, names: list[str], treatment: Treatment = Treatment.CLEAN) -> dict[str, str]:
        """Pre-ingest a set of names. The 'warming the cache before the scene'
        call, and identical in effect to playing each one and throwing the
        audio away — just without the audio.

        Returns name -> outcome, never raising for a single failure: a warm is
        best-effort by construction, and one bad name should not abandon the
        other forty. Every failure is reported, though — SPEC §8 has no room
        for a warm that quietly skipped half a theme.
        """
        out: dict[str, str] = {}
        for name in names:
            try:
                resolved = self.resolve(AssetRef(key=name), treatment)
                out[name] = "hit" if resolved.cache_hit else "warmed"
            except (AssetError, OSError) as e:
                out[name] = f"failed: {e}"
                self.warnings.append(f"warm {name}: {e}")
                log.warning("warm failed for %s: %s", name, e)
        return out

    def _locate(self, ref: AssetRef) -> tuple[Path, str, Media | None]:
        """(source file, logical name or '', declared media)."""
        if ref.path is not None:
            path = Path(ref.path).expanduser()
            if not path.is_file():
                raise UnknownAsset(
                    f"no file at {path} — Wander hands Perform local paths, so this "
                    f"is either a path from another host or a file that has since "
                    f"been cleaned up"
                )
            return path, "", None

        assert ref.key is not None
        row = self.store.get_name(ref.key)
        if row is None:
            raise UnknownAsset(self._unknown_message(ref.key))
        return Path(row.source_path), row.name, Media(row.media)

    def _unknown_message(self, key: str) -> str:
        known = [r.name for r in self.store.list_names()]
        near = difflib.get_close_matches(key, known, n=3, cutoff=0.6)
        msg = f"no asset named {key!r}"
        if near:
            msg += f" — did you mean {', '.join(repr(n) for n in near)}?"
        elif known:
            msg += f" — {len(known)} names are registered; GET /assets lists them"
        else:
            msg += " — no packs are installed; drop a folder in the packs directory"
        return msg

    def _warm_one(
        self,
        source_path: Path,
        key: str,
        name: str,
        declared_media: Media | None,
        treatment: Treatment,
        *,
        pinned: bool,
    ) -> Resolved:
        media = declared_media or ingest.media_of(source_path)

        if media is not Media.AUDIO:
            # Visual media never enters the mixer — SPEC §4 keeps audio and
            # video on separate sinks, and a video's audio track is dropped at
            # the sink with a warning rather than being played here.
            raise AssetError(
                f"{name or source_path.name} is {media}, not audio — visual media "
                f"is served to the surface sink, not ingested into the mixer"
            )

        dest = self.blobs_dir / f"{key}.wav"
        stats = ingest.normalize_audio(
            source_path,
            dest,
            sample_rate=self.sample_rate,
            channels=self.channels,
            treatment=treatment,
        )

        self.store.upsert_blob(
            key=key,
            path=str(dest),
            media=media.value,
            treatment=treatment.value,
            duration_s=stats["duration_s"],
            sample_rate=self.sample_rate,
            channels=self.channels,
            size_bytes=stats["bytes"],
            pinned=pinned,
            source=str(source_path),
        )
        if name:
            self.store.bind_name(name, key)

        pcm = ingest.load_pcm(dest, channels=self.channels)
        self._pcm[key] = pcm
        self._evict_if_over()

        log.info(
            "warmed %s (%s, %.2fs, %s)",
            name or source_path.name,
            treatment,
            stats["duration_s"],
            _human_bytes(stats["bytes"]),
        )
        return Resolved(
            key=key,
            name=name or str(source_path),
            media=media,
            treatment=treatment,
            pcm=pcm,
            duration_s=stats["duration_s"],
            cache_hit=False,
            source=str(source_path),
        )

    # --- eviction -------------------------------------------------------

    def _evict_if_over(self) -> None:
        """Trim unpinned blobs back under the ceiling, least-recently-used first.

        Pinned entries — packs, built-in tones — are neither counted nor
        considered. A pack is an install, and evicting part of one would mean a
        scene that worked yesterday goes quiet today.
        """
        if self.cache_max_bytes <= 0:
            return
        over = self.store.unpinned_bytes() - self.cache_max_bytes
        if over <= 0:
            return
        for row in self.store.eviction_candidates(over):
            Path(row.path).unlink(missing_ok=True)
            self.store.delete_blob(row.key)
            self._pcm.pop(row.key, None)
            log.info("evicted %s (%s, last used %.0fs ago)",
                     row.key[:12], _human_bytes(row.bytes), row.last_used_at)

    # --- reporting ------------------------------------------------------

    def catalog(self) -> list[dict]:
        """Every registered name and whether it is warm. Backs `GET /assets`."""
        out = []
        for row in self.store.list_names():
            blob = self.store.get_blob(row.blob_key) if row.blob_key else None
            out.append(
                {
                    "name": row.name,
                    "pack": row.pack,
                    "media": row.media,
                    "alias_of": row.alias_of,
                    "warm": blob is not None,
                    "pinned": bool(blob and blob.pinned),
                    "duration_s": round(blob.duration_s, 3) if blob else None,
                    "use_count": blob.use_count if blob else 0,
                    "description": tones.describe(row.name) if row.pack == tones.PACK_ID else "",
                }
            )
        return out


def _wav_info(path: Path) -> dict:
    import soundfile as sf  # noqa: PLC0415 - keep libsndfile off the import path

    info = sf.info(str(path))
    return {"duration_s": info.duration, "bytes": path.stat().st_size}


def _human_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB"):
        if n < 1024:
            return f"{n:.0f}{unit}"
        n //= 1024
    return f"{n}GB"
