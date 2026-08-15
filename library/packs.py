"""Asset packs: a folder of media plus a manifest, discovered at startup.

SPEC §5: *"Assets are requested by logical name, never by filename, so scenes
stay portable across machines. Organize them as packs: a folder of media plus a
manifest, discovered at startup, namespaced to avoid collisions. Dropping a
folder in should be the whole install."*

A pack is:

    packs/ops-bridge/
        pack.toml
        greeting.wav
        siren.ogg
        ...

and a manifest looks like:

    schema_version = 1
    id = "ops-bridge"

    [assets."sre.greeting"]
    file = "greeting.wav"
    media = "audio"
    aliases = ["sre.hello"]

Validation follows wander's `bench/specs/__init__.py` beat for beat — a
`required` set difference, `schema_version` checked for *equality* rather than
compatibility, `frozenset` membership for enums, a filename/id agreement check,
and `ValueError(f"{path.name}: …")` for every failure. Consistency with that
loader is worth more here than any local improvement.

Two rules this loader enforces that SPEC calls out specifically:

**Manifests carry a media type from the start.** Not inferred, not optional.
Retrofitting one into a key scheme that assumed audio is miserable, and by the
time it hurts there are saved scenes referencing the old names.

**Keys describe the asset, not its position in a scene.** `sre.greeting`
survives; `scene3.turn1.sre` means renaming a scene orphans assets and buries a
scene format inside a naming convention. The loader rejects the second shape
rather than trusting authors to remember.
"""

from __future__ import annotations

import logging
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from core.item import Media

log = logging.getLogger(__name__)

CURRENT_SCHEMA_VERSION = 1
MANIFEST_NAME = "pack.toml"

VALID_MEDIA: frozenset[str] = frozenset({m.value for m in Media})

# Pack ids and asset keys: lowercase, dot-separated segments. Restrictive on
# purpose — these end up in URLs, log lines, and span attributes, and a key with
# a space in it is a bug three layers away from where it was introduced.
_PACK_ID = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
_ASSET_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9][a-z0-9_-]*)*$")

# The positional-key smell: a segment that is a scene/turn/step number.
# `scene3.turn1.sre`, `act2.line4`, `step_7.intro` all trip this.
_POSITIONAL = re.compile(r"(^|\.)(scene|turn|step|act|beat|line|part|chapter)[_-]?\d+(\.|$)")


@dataclass(frozen=True)
class PackAsset:
    """One named asset. `name` is already namespaced with the pack id."""

    name: str
    pack: str
    media: Media
    path: Path
    alias_of: str | None = None


@dataclass(frozen=True)
class Pack:
    id: str
    root: Path
    assets: tuple[PackAsset, ...]
    schema_version: int

    @property
    def manifest(self) -> Path:
        return self.root / MANIFEST_NAME


def load_pack(root: Path) -> Pack:
    """Read and validate one pack folder. Raises ValueError on anything wrong.

    Every failure here is a startup failure by design (SPEC §8): a pack that
    half-loads means some assets resolve and some silently don't, and the
    listener finds out by hearing nothing.
    """
    path = root / MANIFEST_NAME
    if not path.is_file():
        raise ValueError(f"{root.name}: no {MANIFEST_NAME} — not a pack")

    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{root.name}/{MANIFEST_NAME}: invalid TOML: {e}") from e

    required = {"schema_version", "id", "assets"}
    missing = required - data.keys()
    if missing:
        raise ValueError(f"{root.name}/{MANIFEST_NAME}: missing required keys: {sorted(missing)}")

    schema_version = data["schema_version"]
    if schema_version != CURRENT_SCHEMA_VERSION:
        raise ValueError(
            f"{root.name}/{MANIFEST_NAME}: schema_version is {schema_version}, "
            f"loader expects {CURRENT_SCHEMA_VERSION}"
        )

    pack_id = data["id"]
    if not _PACK_ID.match(str(pack_id)):
        raise ValueError(
            f"{root.name}/{MANIFEST_NAME}: id {pack_id!r} must be lowercase "
            f"alphanumeric with - or _"
        )
    if root.name != pack_id:
        raise ValueError(
            f"{root.name}/{MANIFEST_NAME}: folder name must match id ({pack_id!r})"
        )

    raw_assets = data["assets"]
    if not isinstance(raw_assets, dict) or not raw_assets:
        raise ValueError(f"{root.name}/{MANIFEST_NAME}: [assets] must be a non-empty table")

    assets: list[PackAsset] = []
    seen: set[str] = set()

    for key, entry in raw_assets.items():
        _validate_key(key, pack_id, root)
        if not isinstance(entry, dict):
            raise ValueError(f"{root.name}/{MANIFEST_NAME}: asset {key!r} must be a table")

        entry_missing = {"file", "media"} - entry.keys()
        if entry_missing:
            raise ValueError(
                f"{root.name}/{MANIFEST_NAME}: asset {key!r} missing {sorted(entry_missing)} "
                f"— media is required from schema_version 1 and is never inferred"
            )

        media = entry["media"]
        if media not in VALID_MEDIA:
            raise ValueError(
                f"{root.name}/{MANIFEST_NAME}: asset {key!r} media {media!r} "
                f"not in {sorted(VALID_MEDIA)}"
            )

        # Resolve inside the pack and confirm it stayed there — a manifest is
        # user-supplied content, and `file = "../../etc/passwd"` should be a
        # startup error rather than a served file.
        file_path = (root / entry["file"]).resolve()
        if not file_path.is_relative_to(root.resolve()):
            raise ValueError(
                f"{root.name}/{MANIFEST_NAME}: asset {key!r} file {entry['file']!r} "
                f"escapes the pack directory"
            )
        if not file_path.is_file():
            raise ValueError(
                f"{root.name}/{MANIFEST_NAME}: asset {key!r} file {entry['file']!r} not found"
            )

        full = f"{pack_id}.{key}"
        if full in seen:
            raise ValueError(f"{root.name}/{MANIFEST_NAME}: duplicate asset name {full!r}")
        seen.add(full)
        assets.append(
            PackAsset(name=full, pack=pack_id, media=Media(media), path=file_path)
        )

        # Aliases are cheap now and annoying to retrofit (SPEC §7): once scenes
        # reference assets by name, renaming breaks saved scenes, and an alias
        # is the only non-breaking way out.
        for alias in entry.get("aliases", []):
            _validate_key(alias, pack_id, root)
            full_alias = f"{pack_id}.{alias}"
            if full_alias in seen:
                raise ValueError(
                    f"{root.name}/{MANIFEST_NAME}: duplicate asset name {full_alias!r}"
                )
            seen.add(full_alias)
            assets.append(
                PackAsset(
                    name=full_alias,
                    pack=pack_id,
                    media=Media(media),
                    path=file_path,
                    alias_of=full,
                )
            )

    return Pack(
        id=pack_id,
        root=root,
        assets=tuple(sorted(assets, key=lambda a: a.name)),
        schema_version=schema_version,
    )


def _validate_key(key: str, pack_id: str, root: Path) -> None:
    if not _ASSET_KEY.match(key):
        raise ValueError(
            f"{root.name}/{MANIFEST_NAME}: asset key {key!r} must be lowercase "
            f"dot-separated segments, e.g. 'sre.greeting'"
        )
    if _POSITIONAL.search(key):
        raise ValueError(
            f"{root.name}/{MANIFEST_NAME}: asset key {key!r} names a position in a "
            f"scene. Keys are about the asset, not where it is used — 'sre.greeting' "
            f"survives a rename, 'scene3.turn1.sre' orphans the asset and buries a "
            f"scene format in a naming convention (SPEC §5)."
        )


def discover(packs_dir: Path) -> list[Pack]:
    """Every valid pack under `packs_dir`, sorted by id.

    A folder that fails validation aborts the scan rather than being skipped.
    Skipping would mean booting with a partially-loaded library, and the first
    symptom would be a scene that plays four of its five lines.
    """
    if not packs_dir.is_dir():
        log.info("no packs directory at %s — running with built-in tones only", packs_dir)
        return []

    packs: list[Pack] = []
    for child in sorted(packs_dir.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if not (child / MANIFEST_NAME).is_file():
            log.warning("%s has no %s — skipping (not a pack)", child, MANIFEST_NAME)
            continue
        packs.append(load_pack(child))

    total = sum(len(p.assets) for p in packs)
    log.info("discovered %d pack(s), %d asset name(s)", len(packs), total)
    return packs
