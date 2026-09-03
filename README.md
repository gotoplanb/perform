# Perform

**Multichannel playback server.** It renders what it's given, where it's told, in the order it's told. It coordinates nothing. Python + FastAPI + a software mixer over PortAudio. See [SPEC.md](SPEC.md) for the design brief.

The third local service alongside [Conduct](https://github.com/gotoplanb/conduct) (LLM dispatch + synthesis) and [Wander](https://github.com/gotoplanb/wander) (scenario engine). Conduct dispatches models and makes the audio; Wander owns the scene, the cast, and turn order; Perform owns the output devices.

## Two purposes

1. **Day one — a Claude Code session that can say "the migration finished" out loud.** No Wander, no Conduct, no job ids. `POST /speak` shells to the OS `say` binary and plays the result. This is the case that earns the service a permanent place in the setup: folded into Wander it would only exist when Wander is running, and therefore wouldn't get used.
2. **The interesting one — a scenario, experienced rather than read.** Wander hands over finished audio and imagery, one item at a time, and Perform plays it on the right lane in the right order with the ambience ducked under the dialogue.

## Quickstart

```bash
make install                    # uv sync
make run                        # http://127.0.0.1:8770

curl -X POST localhost:8770/speak \
     -H 'Content-Type: application/json' \
     -d '{"text":"the migration finished"}'

curl -X POST localhost:8770/stop        # shuts up immediately
open http://localhost:8770/ui           # what is playing, and why it might not be
```

No `.env` is required — every setting has a working default. Copy `.env.example` to `.env` when you want to change one. There is no database, no Redis, no queue, and no container: Perform is one process that owns one machine's speakers.

Wire it into Claude Code as an MCP server (`speak`, `play`, `stop`, `status`):

```bash
claude mcp add perform -- uv run --directory /path/to/perform perform-mcp
```

## What runs where

```
                        ┌──────────────────────────────┐
   Wander ──HTTP──────▶ │        Perform :8770         │
   (places items,       │                              │
    hands over paths)   │  voice ────┐                 │
                        │  ambience ─┼─▶ mixer ──▶ CoreAudio
   Claude Code ──MCP──▶ │  music ────┘   (duck,        │
   (speak / stop)       │                 treat, sum)  │
                        │                              │
                        │  backdrop ─▶ SSE ──▶ browser │ /surface
                        └──────────────┬───────────────┘
                                       │  OTLP gRPC
                                       ▼
                                     :4317  (Watchtower's Alloy)

   Conduct :8000 ──writes──▶ output/{job}.mp3 ──Wander hands the path──▶ Perform
```

Perform never talks to Conduct and does not know generation exists. It receives assets.

## API surface

| Method | Path | Notes |
|---|---|---|
| POST | `/speak` | `{text, voice?, bus?}` → the OS voice, on a bus. The day-one surface |
| GET | `/voices` | system voices `say` can use |
| POST | `/buses/{bus}/items` | place an item — `{asset}` or `{path}`, plus duration/treatment/label |
| PUT | `/items/{id}` | replace a **pending** item in place, keeping its id. 409 once playing |
| DELETE | `/items/{id}` | drop a pending item. 409 once playing |
| GET | `/items/{id}` | one item's state and note |
| POST | `/stop` | everything, everywhere. No arguments, cannot fail |
| POST | `/buses/{bus}/stop` | one lane — interrupts *and* clears what was queued |
| PUT | `/buses/{bus}/gain` | the relative mix. Not a master volume |
| GET | `/status` | **the "why is it quiet" endpoint** — see below |
| GET | `/buses` | bus config + live state |
| GET | `/assets` | every logical name, media type, and whether it is warm |
| POST | `/assets/warm` | pre-ingest a scene's assets. Same code path as playing them |
| GET | `/media/{key}` | serve a still or video to the surface |
| GET | `/surface` | the browser page |
| GET | `/surface/events` | SSE — the only streaming endpoint in the three services |
| POST | `/surface/complete` | the page reporting that a video ended |
| GET | `/ui` | the status page |
| GET | `/health` | open |

Auth is off by default and that is deliberate — Perform binds to loopback and owns your speakers. Set `PERFORM_TOKEN` before exposing it anywhere, and every route then needs `Authorization: Bearer <token>`.

## Why is it quiet?

`GET /status` answers that in a field rather than making you infer it:

```json
{
  "quiet_because": "bus(es) voice are holding an open-ended item and are not declared
                    resting_open_ended — Wander placed something with duration
                    'until_replaced' and never replaced it",
  "buses": { "voice": { "state": "stalled", "current": {...}, "pending": [...] } },
  "warnings": [ { "kind": "dropped", "detail": "sre.greeting: no asset named ..." } ]
}
```

A dropped item, a missing asset name, a stalled bus, a video whose audio track was thrown away, a surface with no browser attached — each one is in `warnings` and rendered on `/ui`. Nothing fails silently.

## The model

**Bus** — a playback lane with its own ordered list. Items on one bus play serially; buses run concurrently. Declared in `config/buses.yaml`, fixed at startup, reloadable with `make reload-buses` (SIGUSR1 — uvicorn steals SIGHUP).

Buses differ in *resting behavior*, which is the difference between a steady state and a bug. A backdrop resting on an open-ended item is doing its job; a voice bus doing the same is a hung scene, and `/status` says so.

**Item** — an asset, a bus, a treatment, a duration policy, and an opaque label. Perform logs the label and reports it but **never interprets it**; it exists so Wander's traces and Perform's logs line up when something plays at the wrong moment.

**Duration** comes from one of four places, and this is media-agnostic — a looping animation and an ambience bed are the same case:

| kind | meaning |
|---|---|
| `intrinsic` | audio, video. The file knows |
| `repeat` | one cycle × N |
| `declared` | held for a stated interval |
| `until_replaced` | held until the next item on that bus begins |

**Addressability** — items already placed but not yet played are individually replaceable and removable. Wander drops a placeholder in a slot and swaps in the real asset when generation lands, or removes it if it doesn't. It is a mutable ordered list, not a queue, and a replacement keeps the slot's id.

**Ducking** — a bus declares how far it pushes another one down while it has audio. The shipped config has voice duck ambience by 12 dB. This relative mix is the one audio control Perform keeps, because no system volume control can express it.

**Treatment** — `clean`, `phone`, `internal_bridge`, `incident_bridge`. A property of the item, not the bus, because the same voice bus carries a customer on a phone line and an SRE on the bridge one line apart. Baked in at ingest by ffmpeg and keyed into the cache, so it costs nothing at play time.

## The asset library

Assets are requested by logical name, never by filename, so scenes stay portable. A **pack** is a folder of media plus a `pack.toml`; dropping it into `packs/` is the whole install. See [docs/packs.md](docs/packs.md).

The library is a **warm cache, not a build artifact**. Everything — a pack asset, a path Wander hands over, a `say` recording — passes through one ingest path, is normalized to 48 kHz stereo float32 by ffmpeg, and is keyed by content plus treatment. So "pre-generating a theme" is just `POST /assets/warm`, using the same function a just-in-time play calls, and an unfinished warm costs you a few slow items rather than a failure.

**The repo ships no media.** The built-in tones (`tone.ok`, `tone.error`, `tone.warn`, `tone.ping`, `tone.tick`) are synthesized numerically at startup, which sidesteps licensing and means something works before a single asset is sourced.

## Development

```bash
make install-hooks   # wire the quality gate into git — do this first
make check           # the whole local gate: lint + secret scan + coverage floor
make test            # 324 tests, ~2s. Never opens a device, never makes noise
make voices          # what `say` can do on this machine
make clean           # drop the warm cache. Rebuildable
```

### The quality gate

Same shape as [conduct](https://github.com/gotoplanb/conduct) and
[crossover](https://github.com/gotoplanb/crossover): a fast `pre-commit`, a full
`pre-push`, and a GitHub Actions run that mirrors the latter so `--no-verify`
buys a local shortcut rather than an exemption.

| | pre-commit | pre-push | CI |
|---|---|---|---|
| secret scan | staged | full tree | full tree |
| `.env` / media not staged | ✓ | — | — |
| ruff | staged `.py` | whole tree | whole tree |
| unit tests | ✓ | — | ✓ (fast job) |
| full suite + coverage floor | — | ✓ | ✓ |
| SonarQube gate | — | if reachable | if configured |

Sonar is skipped rather than failed when unreachable — a local observability
stack being down should not block a push. Set `SONAR_TOKEN` in `.env` to enable
it against Watchtower.

Two things the gate enforces that are specific to this repo: **the suite must
never open an audio device or a window** (SPEC §8), and **no media may be
committed** (SPEC §5 — the tones are synthesized, everything else belongs in a
gitignored pack). A skipped test is also treated as a failure in CI, because
eleven surface tests silently vanishing while the run reports green is worse
than a red build. See [CLAUDE.md](CLAUDE.md) for the standing bar.

The suite runs on a `NullDevice` that renders through the real mix graph and discards the output, and a `NullSink` + `ManualClock` that let sequencing be driven by hand. So mixer tests assert duck envelopes sample-for-sample and engine tests assert ordering exactly, with nothing plugged in and nothing to go flaky under load.

Set `PERFORM_AUDIO_DEVICE=null` to get the same for a headless run.

## Read more

- **[docs/architecture.md](docs/architecture.md)** — buses, the mix graph, the threading rule, how a bus advances
- **[docs/packs.md](docs/packs.md)** — pack manifests, naming rules, aliases, the warm cache
- **[docs/sinks.md](docs/sinks.md)** — the three sinks and the completion asymmetry
- **[docs/mcp.md](docs/mcp.md)** — driving Perform from a Claude Code session
- **[docs/operations.md](docs/operations.md)** — reading `/status`, reloading config, common failures
- **[SPEC.md](SPEC.md)** — the original architecture brief

## Notes on conformance

Perform follows Conduct's conventions for packaging (uv + hatchling + ruff + pytest), configuration (`pydantic-settings` with explicit aliases), service shape (module-scope `app`, `lifespan.py`, one route module per concern), and Wander's for telemetry — the OTel dependency floors and shared attribute keys are pinned to match both, so a Perform span, a Wander span, and a Conduct worker span join on the same work.

Three deliberate departures, each earning its keep:

- **No Postgres, Redis, RQ, or Docker.** Perform is one listener on one machine, and playback state is meaningless across a restart. The only durable thing is the asset cache index, which is stdlib `sqlite3` following Wander's `bench/runner/store.py`. Docker Desktop on macOS can't pass through an audio device anyway — the same reason Conduct leaves Ollama on the host.
- **SSE.** Neither sibling streams anything; the house pattern is `202 + poll_url`. The browser surface can't poll — it has to show item *N* the instant the audio bus reaches it — so `/surface/events` is a genuinely new mechanism here rather than conformance to an existing one. `EventSource` was chosen over a socket because it reconnects on its own and stays inside the all-HTTP posture.
- **`.python-version` is committed.** The siblings gitignore it and tolerate whatever interpreter uv picks. Perform pulls in native audio wheels, so a fresh interpreter release with none published turns `uv sync` into a compiler error. 3.12 matches Conduct's runtime.

---

© 2026 Dave Stanton — released under the [MIT-0 license](LICENSE)
