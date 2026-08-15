# Operations

Running it, reading it, and the failures you will actually hit.

## Running

```bash
make run                   # :8770, real audio device
make mcp                   # the stdio MCP server (needs `make run` up)
PERFORM_AUDIO_DEVICE=null make run    # headless: the mix graph runs, the audio is discarded
```

There is no container and no daemon script. Perform owns one machine's speakers, and Docker Desktop
on macOS cannot pass through an audio device — the same reason conduct leaves Ollama on the host.

## Startup is loud on purpose

A good boot says everything it did:

```
INFO config.buses    — loaded 4 bus(es) from buses.yaml: voice->local_audio, ambience->local_audio, ...
INFO library.tones   — built-in tones ready: tone.ping, tone.ok, tone.error, tone.warn, tone.tick
INFO library.packs   — discovered 1 pack(s), 2 asset name(s)
INFO sinks.registry  — sink registered: local_audio accepts=audio push completion=yes
INFO sinks.registry  — sink registered: claude accepts=image,video pull completion=no
INFO audio.device    — audio device open: 3 @ 48000Hz 2ch block=512
INFO core.engine     — engine started: 4 bus(es) — voice, ambience, music, backdrop
INFO lifespan        — perform ready on http://127.0.0.1:8770
```

A bad one exits non-zero with the reason, before the device opens. That is deliberate: a Perform that
booted "fine" and is silently unable to speak is the worst outcome available. Things that abort the
boot:

- a bus routed to a sink that does not exist, or that ducks a bus that does not exist
- a pack manifest that does not parse, names an unknown media type, uses a positional key, or points
  at a missing file
- a `PERFORM_SAY_VOICE` that is not installed on this machine
- an audio device that will not open

## Reading `/status`

`GET /status` is the answer to "why is it quiet", and `quiet_because` says it in a sentence so you do
not have to infer it from the bus table. `/ui` renders the same payload for humans and refreshes
itself.

```json
{
  "quiet_because": "bus(es) voice are holding an open-ended item and are not declared
                    resting_open_ended — Wander placed something with duration
                    'until_replaced' and never replaced it",
  "device":   { "running": true, "xruns": 0, "frames_rendered": 1083392 },
  "buses":    { "voice": { "state": "stalled", "current": {…}, "pending": [], "recent": [] } },
  "sinks":    { "visual": { "attached": 0, "showing": {} } },
  "warnings": [ { "kind": "dropped", "bus": "voice", "detail": "sre.greeting: no asset named …" } ]
}
```

Bus states:

| state | meaning |
|---|---|
| `idle` | nothing playing, nothing pending |
| `playing` | normal |
| `resting` | holding an open-ended item, **and that is expected here** |
| `stalled` | holding an open-ended item, and it is **not** expected here |

Warning kinds: `dropped`, `rejected`, `stalled`, `sink`, `no-completion`, `reload`.

## Common failures

**"It is quiet and nothing is obviously wrong."** Read `quiet_because` first. If it says idle, the
item was never placed or was dropped — check `warnings` and the `recent` list on the bus.

**"A name that used to work stopped working."** `GET /assets` lists every registered name. An unknown
name returns near-misses in the error, because a typo is the single most likely cause. If a pack was
edited, restart — packs are discovered at startup, not watched.

**"The screen is blank but the bus looks fine."** `sinks.visual.attached` is 0. Open `/surface`.
Perform warns on placement when nothing is listening.

**"The video plays but I hear nothing from it."** Correct, and by design. Audio and video are
separate sinks; a video's own track is dropped. `library_warnings` will name the file. Put the audio
on an audio bus.

**"Crackling, or `xruns` climbing."** The render callback is missing its deadline. Raise
`PERFORM_BLOCK_SIZE` (512 → 1024 doubles the budget to ~21 ms at the cost of latency on stop).

**"Two participants sound identical."** They are both on the system default voice. `make voices`
lists what is installed; `PERFORM_SAY_VOICE` is validated at startup so an unknown name fails the
boot rather than falling back — but an *unset* one is legitimately the default.

**"Everything sounds slow or wrong-pitched."** Sample rate mismatch. The rate is part of the cache
key precisely so this cannot happen silently; if you changed `PERFORM_SAMPLE_RATE`, `make clean` and
re-warm.

## Reloading the bus config

```bash
make reload-buses     # SIGUSR1
```

Bus definitions, sink routing, and the duck matrix reload without dropping what is playing. SIGUSR1
and not SIGHUP because uvicorn steals SIGHUP — conduct's pricing reload made the same discovery.

A reload that fails validation **keeps the running config** and logs why. Swapping in a half-parsed
bus set mid-scene would be worse than not reloading. A bus removed by a reload orphans its items and
gets a warning.

## The cache

```bash
make clean            # drop blobs/ and the index
```

Safe at any time. Every entry is rebuildable from a pack or from whatever Wander handed over; the
cost is the next warm. Pinned entries (packs, built-in tones) are never evicted automatically;
everything else is LRU under `PERFORM_CACHE_MAX_MB`.

## Telemetry

Traces and logs go OTLP/gRPC to `localhost:4317` — Watchtower's Alloy, the same collector conduct and
wander use. The OTel dependency floors are pinned to match theirs so all three speak one dialect.

Spans are `perform.item`, with attributes `perform.bus`, `perform.item_id`, `perform.label`,
`perform.sink`, `perform.media`, `perform.treatment`, `perform.duration_kind`, `perform.outcome`.
Shared keys (`job.id`, `model.used`) are copied verbatim from the other two and must not be renamed —
identical keys are the whole mechanism by which a Perform span joins a Wander span joins a Conduct
worker span.

Telemetry never breaks playback: every exporter path is wrapped, and `OTEL_SDK_DISABLED=true` skips
setup entirely.

## Auth

Off by default, and that is right for a loopback service that owns your speakers. Set `PERFORM_TOKEN`
and every route requires `Authorization: Bearer <token>`. Do that in the same edit as any change to
`PERFORM_HOST`.
