# Asset packs and the warm cache

How Perform finds media, and why the cache is a cache rather than a build step.

## A pack is a folder plus a manifest

Dropping a folder into `packs/` is the whole install. It is discovered at startup and namespaced by
its id, so two packs can both declare `greeting` without colliding.

```
packs/ops-bridge/
    pack.toml
    greeting.wav
    siren.ogg
    bridge.png
```

```toml
schema_version = 1
id = "ops-bridge"

[assets."sre.greeting"]
file = "greeting.wav"
media = "audio"
aliases = ["sre.hello"]

[assets."siren"]
file = "siren.ogg"
media = "audio"

[assets."bridge"]
file = "bridge.png"
media = "image"
```

That registers `ops-bridge.sre.greeting`, `ops-bridge.sre.hello`, `ops-bridge.siren`, and
`ops-bridge.bridge`.

The folder name must equal `id`, and `schema_version` is checked for **equality**, not
compatibility — the same call wander's spec loader makes, for the same reason: a loader that
half-understands a newer manifest is worse than one that refuses it.

## Three rules the loader enforces

**`media` is required and never inferred.** Retrofitting a media type into a key scheme that assumed
audio is miserable, and by the time it hurts there are saved scenes referencing the old names. It is
required from `schema_version = 1`.

**Keys describe the asset, not its position in a scene.** `sre.greeting` survives a rename;
`scene3.turn1.sre` means renaming a scene orphans the asset and buries a scene format inside a
naming convention. The loader rejects the second shape rather than trusting authors to remember —
`scene`, `turn`, `step`, `act`, `beat`, `line`, `part`, and `chapter` followed by a number all trip
it. A key that merely *contains* a number (`tier2.escalation`) is fine; the rule is about position,
not digits.

**A file must stay inside its pack.** A manifest is user-supplied content, and
`file = "../../etc/passwd"` is a startup error rather than a served file.

Every failure aborts the boot, naming the manifest and the key. A pack that half-loads means some
assets resolve and some silently do not, and the listener finds out by hearing four fifths of a
scene.

## Aliases

Once scenes reference assets by name, renaming breaks saved scenes. An alias is the only
non-breaking way out, and it is cheap now and annoying to retrofit — so it is in the schema from
version 1. An alias resolves to the same file and shows up in `GET /assets` with `alias_of` set.

## The warm cache

The library is a warm cache, not a build artifact. Everything playable passes through one ingest
path and lands in `blobs/`, content-addressed:

```
key = sha256(source bytes) + treatment + sample_rate + channels + voice
```

Content rather than path+mtime, because Conduct names every synthesis with a fresh job id — identical
audio arrives under a new filename every time, and hashing bytes is what makes the cache survive
that. Treatment, rate, and channels join the key because each one changes the rendered result, and a
key that silently returns a wrong-rate blob is worse than a miss.

**Pack assets and built-in tones are pinned.** They are never evicted and never counted against
`PERFORM_CACHE_MAX_MB`. A pack is an install; evicting part of one would mean a scene that worked
yesterday goes quiet today. Everything else is LRU.

### Warming

```bash
curl -X POST localhost:8770/assets/warm \
     -H 'Content-Type: application/json' \
     -d '{"names":["ops-bridge.sre.greeting","ops-bridge.siren"],"treatment":"incident_bridge"}'
```

This calls exactly the function a just-in-time play calls, so a warm and a play differ only in
whether anyone hears the result. An unfinished warm costs a few slow first items rather than a
failure.

Warm with the treatment the scene will actually use. Treatments are baked in at ingest and are part
of the key, so warming `clean` and then playing `phone` is a miss.

A warm never raises for one bad name — one typo should not abandon the other forty — but every
failure comes back in the response and lands in `/status`.

## The repo ships no media

The built-in tones are synthesized numerically at startup. That sidesteps licensing questions and
means something works on a fresh clone before a single asset is sourced:

| name | sound |
|---|---|
| `tone.ok` | rising major third — success |
| `tone.error` | falling minor third — failure |
| `tone.warn` | double beep — attention |
| `tone.ping` | single sine — neutral |
| `tone.tick` | short click — progress |

## Media that is not audio

Stills and video never enter the mixer. `resolve_visual` content-addresses the original, hardlinks it
under `blobs/visual/`, and hands back a key the `/media/{key}` route serves. Linking rather than
serving originals in place is what lets that route enforce a single rule — *resolves under the blob
directory* — instead of maintaining a list of trusted directories.

A video arriving with a live audio track is played muted and **said out loud** in the warnings. SPEC
§4 notes this case "will fail silently rather than error"; the muting was never the missing part,
anyone being told was.
