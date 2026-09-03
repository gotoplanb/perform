"""The warm cache: keys, hits, pinning, eviction, and the media route guard.

The built-in tones make most of this testable with no media files and no
network — they are synthesized numerically, which is the reason SPEC §5 says to
ship them that way rather than committing a folder of wavs.
"""

from __future__ import annotations

from pathlib import Path

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


# --- packs, visuals, and eviction --------------------------------------


def test_bootstrap_registers_pack_names(library, tmp_path):
    """Dropping a folder in is the whole install (SPEC §5)."""
    from library.packs import CURRENT_SCHEMA_VERSION

    pack = library.packs_dir / "ops-bridge"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "greeting.wav").write_bytes(b"RIFF....WAVEfmt ")
    (pack / "pack.toml").write_text(
        f'schema_version = {CURRENT_SCHEMA_VERSION}\n'
        'id = "ops-bridge"\n\n'
        '[assets."sre.greeting"]\n'
        'file = "greeting.wav"\n'
        'media = "audio"\n'
        'aliases = ["sre.hello"]\n'
    )

    library.bootstrap()

    names = {row.name for row in library.store.list_names(pack="ops-bridge")}
    assert names == {"ops-bridge.sre.greeting", "ops-bridge.sre.hello"}


def test_a_rescan_forgets_a_dropped_asset(library):
    """A pack whose manifest dropped an asset should stop resolving it, not
    keep a stale row alive."""
    from library.packs import CURRENT_SCHEMA_VERSION

    pack = library.packs_dir / "p"
    pack.mkdir(parents=True, exist_ok=True)
    (pack / "a.wav").write_bytes(b"RIFF")
    manifest = pack / "pack.toml"

    def write(keys):
        body = f"schema_version = {CURRENT_SCHEMA_VERSION}\nid = \"p\"\n\n"
        for k in keys:
            body += f'[assets."{k}"]\nfile = "a.wav"\nmedia = "audio"\n\n'
        manifest.write_text(body)

    write(["one", "two"])
    library.bootstrap()
    assert len(library.store.list_names(pack="p")) == 2

    write(["one"])
    library.bootstrap()
    assert [r.name for r in library.store.list_names(pack="p")] == ["p.one"]


def test_resolving_a_visual_as_audio_is_refused(library, tmp_path):
    """Audio and video stay separate (SPEC §4), and the message says which sink
    the caller wanted."""
    from core.errors import AssetError

    still = tmp_path / "still.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    library.store.upsert_name(
        name="scene.bridge", pack="p", media="image", source_path=str(still)
    )

    with pytest.raises(AssetError, match="not audio"):
        library.resolve(AssetRef(key="scene.bridge"))


def test_resolving_audio_as_a_visual_is_refused(library):
    from core.errors import AssetError

    with pytest.raises(AssetError, match="is audio"):
        library.resolve_visual(AssetRef(key="tone.ok"))


def test_resolve_visual_links_into_the_blob_dir(library, tmp_path):
    """Linking rather than serving originals in place is what lets /media
    enforce a single rule: it must resolve under the blob directory."""
    still = tmp_path / "outside.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\npixels")
    library.store.upsert_name(
        name="scene.bridge", pack="p", media="image", source_path=str(still)
    )

    resolved = library.resolve_visual(AssetRef(key="scene.bridge"))

    assert resolved.path.parent == library.blobs_dir / "visual"
    assert resolved.url_path.startswith("/media/")
    assert resolved.path.read_bytes() == still.read_bytes()
    # And the media route will now serve it.
    assert library.blob_path(resolved.url_path.removeprefix("/media/")) == resolved.path


def test_resolve_visual_is_idempotent(library, tmp_path):
    still = tmp_path / "s.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    library.store.upsert_name(
        name="s", pack="p", media="image", source_path=str(still)
    )
    first = library.resolve_visual(AssetRef(key="s"))
    second = library.resolve_visual(AssetRef(key="s"))
    assert first.key == second.key
    assert first.path == second.path


def test_resolve_visual_falls_back_to_copy_when_linking_fails(
    library, tmp_path, monkeypatch
):
    """A packs directory on another filesystem cannot be hardlinked into the
    blob dir."""
    still = tmp_path / "s.png"
    still.write_bytes(b"\x89PNG\r\n\x1a\n")
    library.store.upsert_name(
        name="s", pack="p", media="image", source_path=str(still)
    )

    def refuse(self, target):
        raise OSError("cross-device link")

    monkeypatch.setattr(Path, "hardlink_to", refuse)

    resolved = library.resolve_visual(AssetRef(key="s"))
    assert resolved.path.read_bytes() == still.read_bytes()


def test_eviction_removes_the_file_and_the_row(library):
    """The caller unlinks and then deletes the row, so a failed unlink never
    leaves a dangling index entry.

    The blob is seeded directly rather than ingested: this is a test of eviction,
    and routing it through `resolve` would drag ffmpeg into a unit test for no
    gain — which is exactly what put ffmpeg-dependent tests in tests/unit/ and
    broke the no-system-packages CI job.
    """
    blob_file = library.blobs_dir / "deadbeef.wav"
    blob_file.parent.mkdir(parents=True, exist_ok=True)
    blob_file.write_bytes(b"\x00" * 4096)
    library.store.upsert_blob(
        key="deadbeef",
        path=str(blob_file),
        media="audio",
        treatment="clean",
        duration_s=1.0,
        sample_rate=48_000,
        channels=2,
        size_bytes=4096,
        pinned=False,
    )
    library._pcm["deadbeef"] = object()  # the decoded-PCM cache must drop it too

    library.cache_max_bytes = 1
    library._evict_if_over()

    assert library.store.get_blob("deadbeef") is None
    assert not blob_file.is_file()
    assert "deadbeef" not in library._pcm


def test_eviction_is_a_no_op_when_disabled(library):
    library.cache_max_bytes = 0
    library._evict_if_over()  # must not raise or delete anything
    assert library.store.get_blob(library.resolve(AssetRef(key="tone.ok")).key)


def test_human_bytes_scales():
    from library.library import _human_bytes

    assert _human_bytes(500).endswith("B")
    assert _human_bytes(2048).endswith("KB")
    assert _human_bytes(5 * 1024 * 1024).endswith("MB")
    assert _human_bytes(3 * 1024 * 1024 * 1024).endswith("GB")
