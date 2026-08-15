"""The warm cache index — what has been normalized, and where it landed.

SPEC §5: *"Treat the library as a warm cache, not a build artifact. One path for
all assets, keyed by content plus voice, persisted to disk, with fixed assets
pinned so they never evict."*

Shaped after wander's `bench/runner/store.py`: a module-level SCHEMA string,
`executescript` on init, `PRAGMA foreign_keys = ON`, upserts via
`ON CONFLICT … DO UPDATE`, frozen dataclass row types, and a context-manager
`Store`. Nothing clever — the point of using sqlite for this rather than a JSON
file is atomic upserts from more than one place, not query power.

Two tables:

    blobs    one row per (content, treatment, rate, channels) — the cache proper
    names    logical name -> blob, plus the pack that declared it and the media
             type from its manifest

`names` exists separately from `blobs` because the same audio can be reachable
under several names — the alias case SPEC §7 flags as cheap now and annoying to
retrofit — and because a pack can declare a name whose blob has not been warmed
yet, which is precisely the state `POST /assets/warm` exists to resolve.

This is the only durable state in Perform, and even this is a cache: every row
is rebuildable from a pack folder or from whatever Wander handed over. Playback
state — what is on a bus right now — is in memory and is meaningless after a
restart, which is why there is no table for it.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS blobs (
    -- sha256(content) + treatment + rate + channels + voice
    key           TEXT PRIMARY KEY,
    path          TEXT    NOT NULL,          -- the normalized PCM under blobs_dir
    media         TEXT    NOT NULL,
    treatment     TEXT    NOT NULL,
    duration_s    REAL    NOT NULL,
    sample_rate   INTEGER NOT NULL,
    channels      INTEGER NOT NULL,
    bytes         INTEGER NOT NULL,
    -- Pinned entries are never evicted and never counted against the cache
    -- ceiling. Pack assets are pinned: a pack is an install, not a guess about
    -- what might get played.
    pinned        INTEGER NOT NULL DEFAULT 0,
    created_at    REAL    NOT NULL,
    last_used_at  REAL    NOT NULL,
    use_count     INTEGER NOT NULL DEFAULT 0,
    source        TEXT                       -- where it came from, for humans
);

CREATE TABLE IF NOT EXISTS names (
    name          TEXT PRIMARY KEY,          -- logical name: pack.key, e.g. "ops.sre.greeting"
    pack          TEXT    NOT NULL,
    media         TEXT    NOT NULL,          -- declared in the manifest, not sniffed
    source_path   TEXT    NOT NULL,          -- the original file inside the pack
    alias_of      TEXT,                      -- set when this name is an alias
    blob_key      TEXT REFERENCES blobs(key) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_blobs_pinned ON blobs(pinned, last_used_at);
CREATE INDEX IF NOT EXISTS idx_names_pack ON names(pack);
"""


@dataclass(frozen=True)
class BlobRow:
    key: str
    path: str
    media: str
    treatment: str
    duration_s: float
    sample_rate: int
    channels: int
    bytes: int
    pinned: bool
    created_at: float
    last_used_at: float
    use_count: int
    source: str | None


@dataclass(frozen=True)
class NameRow:
    name: str
    pack: str
    media: str
    source_path: str
    alias_of: str | None
    blob_key: str | None

    @property
    def warm(self) -> bool:
        return self.blob_key is not None


class Store:
    def __init__(self, db_path: Path | str) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False because FastAPI serves requests on a thread
        # pool; every write here goes through _txn, which serializes them.
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON;")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Cursor]:
        cur = self._conn.cursor()
        try:
            yield cur
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise
        finally:
            cur.close()

    # ----- blobs --------------------------------------------------------

    def upsert_blob(
        self,
        *,
        key: str,
        path: str,
        media: str,
        treatment: str,
        duration_s: float,
        sample_rate: int,
        channels: int,
        size_bytes: int,
        pinned: bool = False,
        source: str | None = None,
    ) -> None:
        now = time.time()
        with self._txn() as cur:
            cur.execute(
                """
                INSERT INTO blobs
                    (key, path, media, treatment, duration_s, sample_rate, channels,
                     bytes, pinned, created_at, last_used_at, use_count, source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(key) DO UPDATE SET
                    path        = excluded.path,
                    media       = excluded.media,
                    duration_s  = excluded.duration_s,
                    bytes       = excluded.bytes,
                    -- Pinning is sticky: a blob first warmed just-in-time and
                    -- later claimed by a pack becomes pinned and stays that way.
                    pinned      = MAX(blobs.pinned, excluded.pinned),
                    source      = COALESCE(excluded.source, blobs.source)
                """,
                (key, path, media, treatment, duration_s, sample_rate, channels,
                 size_bytes, int(pinned), now, now, source),
            )

    def get_blob(self, key: str) -> BlobRow | None:
        row = self._conn.execute(
            "SELECT key, path, media, treatment, duration_s, sample_rate, channels, "
            "bytes, pinned, created_at, last_used_at, use_count, source "
            "FROM blobs WHERE key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        return BlobRow(*row[:8], bool(row[8]), *row[9:])

    def touch_blob(self, key: str) -> None:
        """Record a play. Drives LRU eviction, and `use_count` is the number
        that tells you whether warming a theme was worth it."""
        with self._txn() as cur:
            cur.execute(
                "UPDATE blobs SET last_used_at = ?, use_count = use_count + 1 WHERE key = ?",
                (time.time(), key),
            )

    def unpinned_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(bytes), 0) FROM blobs WHERE pinned = 0"
        ).fetchone()
        return int(row[0])

    def eviction_candidates(self, over_bytes: int) -> list[BlobRow]:
        """Least-recently-used unpinned blobs, enough to get back under the
        ceiling. Returns rows; the caller unlinks the files and calls
        `delete_blob`, so a failed unlink never leaves a dangling index row."""
        if over_bytes <= 0:
            return []
        rows = self._conn.execute(
            "SELECT key, path, media, treatment, duration_s, sample_rate, channels, "
            "bytes, pinned, created_at, last_used_at, use_count, source "
            "FROM blobs WHERE pinned = 0 ORDER BY last_used_at ASC"
        ).fetchall()
        out: list[BlobRow] = []
        freed = 0
        for row in rows:
            out.append(BlobRow(*row[:8], bool(row[8]), *row[9:]))
            freed += int(row[7])
            if freed >= over_bytes:
                break
        return out

    def delete_blob(self, key: str) -> None:
        with self._txn() as cur:
            cur.execute("DELETE FROM blobs WHERE key = ?", (key,))

    # ----- names --------------------------------------------------------

    def upsert_name(
        self,
        *,
        name: str,
        pack: str,
        media: str,
        source_path: str,
        alias_of: str | None = None,
        blob_key: str | None = None,
    ) -> None:
        with self._txn() as cur:
            cur.execute(
                """
                INSERT INTO names (name, pack, media, source_path, alias_of, blob_key)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(name) DO UPDATE SET
                    pack        = excluded.pack,
                    media       = excluded.media,
                    source_path = excluded.source_path,
                    alias_of    = excluded.alias_of,
                    blob_key    = COALESCE(excluded.blob_key, names.blob_key)
                """,
                (name, pack, media, source_path, alias_of, blob_key),
            )

    def bind_name(self, name: str, blob_key: str) -> None:
        with self._txn() as cur:
            cur.execute("UPDATE names SET blob_key = ? WHERE name = ?", (blob_key, name))

    def get_name(self, name: str) -> NameRow | None:
        row = self._conn.execute(
            "SELECT name, pack, media, source_path, alias_of, blob_key FROM names WHERE name = ?",
            (name,),
        ).fetchone()
        return NameRow(*row) if row else None

    def list_names(self, pack: str | None = None) -> list[NameRow]:
        if pack:
            cur = self._conn.execute(
                "SELECT name, pack, media, source_path, alias_of, blob_key "
                "FROM names WHERE pack = ? ORDER BY name",
                (pack,),
            )
        else:
            cur = self._conn.execute(
                "SELECT name, pack, media, source_path, alias_of, blob_key "
                "FROM names ORDER BY name"
            )
        return [NameRow(*row) for row in cur.fetchall()]

    def forget_pack(self, pack: str) -> None:
        """Drop a pack's names. Its blobs stay — they are content-addressed and
        may be shared with another pack or with something Wander handed over."""
        with self._txn() as cur:
            cur.execute("DELETE FROM names WHERE pack = ?", (pack,))
