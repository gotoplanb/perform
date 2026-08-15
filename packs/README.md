# packs/

Drop an asset pack folder in here. That is the whole install — packs are discovered at startup and
namespaced by their id, so two packs can both declare `greeting` without colliding.

**Nothing in this directory is committed except this file.** Packs are user-supplied media, and the
repo deliberately ships none: the built-in tones (`tone.ok`, `tone.error`, `tone.warn`, `tone.ping`,
`tone.tick`) are synthesized numerically at startup, which sidesteps licensing questions and means
something works on a fresh clone before a single asset is sourced.

A minimal pack:

```
packs/ops-bridge/
    pack.toml
    greeting.wav
```

```toml
schema_version = 1
id = "ops-bridge"          # must equal the folder name

[assets."sre.greeting"]    # becomes ops-bridge.sre.greeting
file = "greeting.wav"
media = "audio"            # required, never inferred
aliases = ["sre.hello"]    # optional; cheap now, annoying to retrofit
```

Then:

```bash
make run
curl -s localhost:8770/assets
curl -X POST localhost:8770/assets/warm -H 'Content-Type: application/json' \
     -d '{"names":["ops-bridge.sre.greeting"]}'
curl -X POST localhost:8770/buses/voice/items -H 'Content-Type: application/json' \
     -d '{"asset":"ops-bridge.sre.greeting"}'
```

Any audio, video, or image format ffmpeg can read works — it is normalized on ingest, so a 22 kHz
mono MP3 and a 96 kHz stereo FLAC both end up as the same thing by the time the mixer sees them.

Keys are about the *asset*, not where it gets used: `sre.greeting` survives a scene rename,
`scene3.turn1.sre` does not. The loader enforces this rather than trusting you to remember.

Full details in [docs/packs.md](../docs/packs.md).
