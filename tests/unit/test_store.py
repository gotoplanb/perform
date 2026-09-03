"""The warm-cache index.

The only durable state in Perform, and even this is a cache. Most of it was
covered incidentally through the library; the eviction and query paths were not,
and eviction is the one place a bug means a scene that worked yesterday goes
quiet today.
"""

from __future__ import annotations

import sqlite3
import time

import pytest

from library.store import BlobRow, NameRow, Store


@pytest.fixture
def store(tmp_path) -> Store:
    with Store(tmp_path / "library.sqlite") as s:
        yield s


def add_blob(store: Store, key: str, *, size=1000, pinned=False, path=None):
    store.upsert_blob(
        key=key,
        path=path or f"/blobs/{key}.wav",
        media="audio",
        treatment="clean",
        duration_s=1.0,
        sample_rate=48_000,
        channels=2,
        size_bytes=size,
        pinned=pinned,
        source="test",
    )


# --- lifecycle ----------------------------------------------------------


def test_it_creates_its_parent_directory(tmp_path):
    """The blobs dir may not exist on a fresh clone."""
    with Store(tmp_path / "nested" / "deep" / "library.sqlite") as s:
        assert s.path.exists()


def test_it_works_as_a_context_manager(tmp_path):
    with Store(tmp_path / "x.sqlite") as s:
        add_blob(s, "k")
    # Closed on exit; using it again raises rather than silently reopening.
    with pytest.raises(sqlite3.ProgrammingError):
        s.get_blob("k")


def test_a_failed_write_rolls_back(store):
    """_txn rolls back on any exception, so a half-applied write cannot leave
    the index disagreeing with the blobs on disk."""
    add_blob(store, "good")
    with pytest.raises(sqlite3.IntegrityError), store._txn() as cur:
        cur.execute("INSERT INTO blobs (key) VALUES ('bad')")  # NOT NULL violation
    assert store.get_blob("good") is not None
    assert store.get_blob("bad") is None


# --- blobs --------------------------------------------------------------


def test_round_trip(store):
    add_blob(store, "abc", size=2048)
    row = store.get_blob("abc")
    assert isinstance(row, BlobRow)
    assert row.key == "abc"
    assert row.bytes == 2048
    assert row.pinned is False
    assert row.use_count == 0


def test_an_unknown_key_is_none(store):
    assert store.get_blob("nope") is None


def test_upsert_updates_rather_than_duplicating(store):
    add_blob(store, "abc", size=100)
    add_blob(store, "abc", size=200)
    assert store.get_blob("abc").bytes == 200


def test_pinning_is_sticky(store):
    """A blob first warmed just-in-time and later claimed by a pack becomes
    pinned and stays that way — otherwise a re-scan could unpin a pack asset
    and expose it to eviction."""
    add_blob(store, "abc", pinned=False)
    add_blob(store, "abc", pinned=True)
    assert store.get_blob("abc").pinned is True

    add_blob(store, "abc", pinned=False)
    assert store.get_blob("abc").pinned is True


def test_touch_records_a_play(store):
    add_blob(store, "abc")
    before = store.get_blob("abc").last_used_at
    time.sleep(0.01)
    store.touch_blob("abc")
    store.touch_blob("abc")

    row = store.get_blob("abc")
    assert row.use_count == 2
    assert row.last_used_at >= before


def test_delete_removes_it(store):
    add_blob(store, "abc")
    store.delete_blob("abc")
    assert store.get_blob("abc") is None


# --- eviction -----------------------------------------------------------


def test_unpinned_bytes_excludes_pinned(store):
    """A pack is an install, not a guess about what might get played, so its
    bytes are neither counted nor evicted."""
    add_blob(store, "pinned", size=5000, pinned=True)
    add_blob(store, "loose", size=300)
    assert store.unpinned_bytes() == 300


def test_unpinned_bytes_on_an_empty_store(store):
    assert store.unpinned_bytes() == 0


def test_no_candidates_when_under_the_ceiling(store):
    add_blob(store, "a", size=100)
    assert store.eviction_candidates(0) == []
    assert store.eviction_candidates(-50) == []


def test_candidates_are_least_recently_used_first(store):
    for key in ("oldest", "middle", "newest"):
        add_blob(store, key, size=100)
        time.sleep(0.01)
        store.touch_blob(key)

    candidates = store.eviction_candidates(150)
    assert [c.key for c in candidates] == ["oldest", "middle"]


def test_candidates_stop_once_enough_is_freed(store):
    for key in "abcde":
        add_blob(store, key, size=100)
    assert len(store.eviction_candidates(250)) == 3


def test_pinned_blobs_are_never_candidates(store):
    add_blob(store, "pinned", size=10_000, pinned=True)
    add_blob(store, "loose", size=100)
    assert [c.key for c in store.eviction_candidates(50_000)] == ["loose"]


def test_asking_for_more_than_exists_returns_everything_available(store):
    add_blob(store, "a", size=100)
    assert len(store.eviction_candidates(999_999)) == 1


# --- names --------------------------------------------------------------


def test_name_round_trip(store):
    store.upsert_name(
        name="ops.sre.greeting", pack="ops", media="audio", source_path="/p/g.wav"
    )
    row = store.get_name("ops.sre.greeting")
    assert isinstance(row, NameRow)
    assert row.pack == "ops"
    assert row.warm is False


def test_an_unknown_name_is_none(store):
    assert store.get_name("nope") is None


def test_binding_a_name_marks_it_warm(store):
    add_blob(store, "key1")
    store.upsert_name(name="a.b", pack="p", media="audio", source_path="/x")
    store.bind_name("a.b", "key1")

    row = store.get_name("a.b")
    assert row.blob_key == "key1"
    assert row.warm is True


def test_upsert_name_keeps_an_existing_binding(store):
    """A pack re-scan should not un-warm a name that is already cached."""
    add_blob(store, "key1")
    store.upsert_name(name="a.b", pack="p", media="audio", source_path="/x")
    store.bind_name("a.b", "key1")

    store.upsert_name(name="a.b", pack="p", media="audio", source_path="/x2")

    row = store.get_name("a.b")
    assert row.blob_key == "key1"
    assert row.source_path == "/x2"


def test_list_names_is_sorted(store):
    for name in ("z.a", "a.b", "m.c"):
        store.upsert_name(name=name, pack="p", media="audio", source_path="/x")
    assert [r.name for r in store.list_names()] == ["a.b", "m.c", "z.a"]


def test_list_names_filters_by_pack(store):
    store.upsert_name(name="one", pack="alpha", media="audio", source_path="/x")
    store.upsert_name(name="two", pack="beta", media="audio", source_path="/x")

    assert [r.name for r in store.list_names(pack="alpha")] == ["one"]
    assert len(store.list_names()) == 2


def test_an_alias_records_what_it_points_at(store):
    store.upsert_name(
        name="p.hello", pack="p", media="audio", source_path="/x", alias_of="p.greeting"
    )
    assert store.get_name("p.hello").alias_of == "p.greeting"


def test_forget_pack_drops_names_but_keeps_blobs(store):
    """Blobs are content-addressed and may be shared with another pack or with
    something Wander handed over."""
    add_blob(store, "shared")
    store.upsert_name(
        name="p.one", pack="p", media="audio", source_path="/x", blob_key="shared"
    )
    store.upsert_name(name="q.one", pack="q", media="audio", source_path="/y")

    store.forget_pack("p")

    assert store.get_name("p.one") is None
    assert store.get_name("q.one") is not None
    assert store.get_blob("shared") is not None
