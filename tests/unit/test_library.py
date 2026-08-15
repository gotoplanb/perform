"""The warm cache: keys, hits, pinning, eviction, and the media route guard.

The built-in tones make most of this testable with no media files and no
network — they are synthesized numerically, which is the reason SPEC §5 says to
ship them that way rather than committing a folder of wavs.
"""

from __future__ import annotations

import pytest

from core.errors import UnknownAsset
from core.item import AssetRef, Treatment
from library import ingest
from library.tones import PACK_ID


def test_bootstrap_registers_the_built_in_tones(library):
    names = {row.name for row in library.store.list_names()}
    assert f"{PACK_ID}.ok" in names
    assert f"{PACK_ID}.error" in names
    assert f"{PACK_ID}.ping" in names


def test_tones_are_pinned_so_they_never_evict(library):
    resolved = library.resolve(AssetRef(key="tone.ok"))
    assert library.store.get_blob(resolved.key).pinned is True


def test_resolving_a_tone_returns_playable_pcm(library):
    resolved = library.resolve(AssetRef(key="tone.ok"))
    assert resolved.pcm.shape[1] == library.channels
    assert resolved.pcm.dtype.name == "float32"
    assert resolved.duration_s > 0


def test_the_second_resolve_is_a_cache_hit(library):
    """The property that makes a repeated notification free: keyed on content,
    and the content did not change."""
    first = library.resolve(AssetRef(key="tone.ok"))
    second = library.resolve(AssetRef(key="tone.ok"))
    assert second.cache_hit is True
    assert second.key == first.key


def test_use_count_tracks_plays(library):
    for _ in range(3):
        library.resolve(AssetRef(key="tone.ok"))
    assert library.store.get_blob(library.resolve(AssetRef(key="tone.ok")).key).use_count >= 3


def test_an_unknown_name_suggests_near_misses(library):
    """A typo'd asset name is the single most likely reason a scene goes quiet,
    so the error carries the repair material."""
    with pytest.raises(UnknownAsset, match="did you mean"):
        library.resolve(AssetRef(key="tone.okay"))


def test_an_unknown_name_with_no_near_miss_still_points_somewhere(library):
    with pytest.raises(UnknownAsset, match="GET /assets"):
        library.resolve(AssetRef(key="zzzz.qqqq"))


def test_a_missing_path_says_what_probably_happened(library, tmp_path):
    with pytest.raises(UnknownAsset, match="another host|cleaned up"):
        library.resolve(AssetRef(path=tmp_path / "gone.wav"))


def test_warm_reports_per_name_and_never_raises(library):
    """A warm is best-effort by construction: one typo should not abandon the
    other forty. But every failure is reported."""
    result = library.warm(["tone.ok", "tone.error", "tone.nope"])

    assert result["tone.ok"] in ("warmed", "hit")
    assert result["tone.error"] in ("warmed", "hit")
    assert result["tone.nope"].startswith("failed")
    assert any("tone.nope" in w for w in library.warnings)


def test_warming_then_playing_is_the_same_code_path(library):
    """SPEC §5: 'pre-generating a theme' becomes 'warming the cache before the
    scene', using the same code path as just-in-time delivery."""
    library.warm(["tone.ping"])
    resolved = library.resolve(AssetRef(key="tone.ping"))
    assert resolved.cache_hit is True


def test_an_already_normalized_blob_is_not_re_ingested(library):
    """`POST /speak` places the normalized recording rather than the temp AIFF,
    because the AIFF is deleted straight after. Resolving that path used to
    hash the WAV, land on a different key, and run ffmpeg again — a duplicate
    cache entry for every single utterance."""
    from pathlib import Path

    first = library.resolve(AssetRef(key="tone.ok"))
    blob_path = Path(library.store.get_blob(first.key).path)
    before = len(list(library.blobs_dir.glob("*.wav")))

    again = library.resolve(AssetRef(path=blob_path))

    assert again.cache_hit is True
    assert again.key == first.key
    assert len(list(library.blobs_dir.glob("*.wav"))) == before


def test_a_path_outside_the_cache_is_still_ingested_normally(library, tmp_path):
    """The short-circuit must be narrow — only a file sitting under blobs_dir
    under its own key is known to be normalized already."""
    from pathlib import Path

    stray = library.blobs_dir / "not-a-key.wav"
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.write_bytes(b"not a real wav")

    assert library._as_cached_blob(Path(stray)) is None


def test_the_catalog_reports_warmth(library):
    library.resolve(AssetRef(key="tone.ok"))
    catalog = {a["name"]: a for a in library.catalog()}

    assert catalog["tone.ok"]["warm"] is True
    assert catalog["tone.ok"]["pinned"] is True
    assert catalog["tone.ok"]["media"] == "audio"
    assert catalog["tone.ok"]["description"]


# --- cache keys -------------------------------------------------------------


def test_the_key_includes_the_treatment(library, tmp_path):
    """The same line through `phone` is a different sound, so it is a different
    entry — which is what makes treatments free at play time."""
    source = tmp_path / "x.wav"
    source.write_bytes(b"some bytes")

    clean = ingest.content_key(source, Treatment.CLEAN, sample_rate=48000, channels=2)
    phone = ingest.content_key(source, Treatment.PHONE, sample_rate=48000, channels=2)
    assert clean != phone


def test_the_key_includes_rate_and_channels(library, tmp_path):
    """A blob at the wrong rate is unplayable, and silently reusing one presents
    as 'why does everything sound slow'."""
    source = tmp_path / "x.wav"
    source.write_bytes(b"some bytes")

    a = ingest.content_key(source, Treatment.CLEAN, sample_rate=48000, channels=2)
    b = ingest.content_key(source, Treatment.CLEAN, sample_rate=44100, channels=2)
    c = ingest.content_key(source, Treatment.CLEAN, sample_rate=48000, channels=1)
    assert len({a, b, c}) == 3


def test_the_key_is_content_not_filename(library, tmp_path):
    """Conduct names every synthesis with a fresh job id, so identical audio
    arrives under a new filename every time. Hashing bytes is what makes the
    cache survive that."""
    first = tmp_path / "aaaaaaaa-1111.mp3"
    second = tmp_path / "bbbbbbbb-2222.mp3"
    first.write_bytes(b"identical audio")
    second.write_bytes(b"identical audio")

    key_a = ingest.content_key(first, Treatment.CLEAN, sample_rate=48000, channels=2)
    key_b = ingest.content_key(second, Treatment.CLEAN, sample_rate=48000, channels=2)
    assert key_a == key_b


# --- eviction ---------------------------------------------------------------


def test_pinned_blobs_are_never_evicted(library):
    """A pack is an install. Evicting part of one would mean a scene that worked
    yesterday goes quiet today."""
    library.resolve(AssetRef(key="tone.ok"))
    library.cache_max_bytes = 1  # absurdly small ceiling
    library._evict_if_over()

    assert library.store.get_blob(library.resolve(AssetRef(key="tone.ok")).key) is not None


def test_pinned_bytes_are_not_counted_against_the_ceiling(library):
    library.warm(["tone.ok", "tone.error", "tone.ping"])
    assert library.store.unpinned_bytes() == 0


# --- the media route guard --------------------------------------------------


def test_blob_path_refuses_traversal(library):
    """The one route that turns a caller-supplied string into a file read.
    Same guard conduct puts on /output/{file}."""
    for attempt in ("../../etc/passwd", "..%2f..%2fetc", "/etc/passwd"):
        with pytest.raises(UnknownAsset):
            library.blob_path(attempt)


def test_blob_path_refuses_a_name_that_does_not_exist(library):
    with pytest.raises(UnknownAsset):
        library.blob_path("deadbeef.png")
