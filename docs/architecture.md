# Architecture

What the pieces are and why they are shaped this way. [SPEC.md](../SPEC.md) is the brief; this is
what got built from it.

## The one-sentence version

A bus is a lane with an ordered list. One asyncio task walks each lane, hands items to a sink, and
waits for the sink to say the item ended. For audio, the sink is a software mixer feeding one
PortAudio stream. Everything else is bookkeeping so that when it goes wrong you can tell why.

## Layers

```
routes/          HTTP. Thin — validate, translate errors, call the engine.
core/engine.py   One task per bus. Ordering, completion, interruption, the status payload.
core/bus.py      The mutable ordered list, the cursor, the resting policy.
sinks/           Where a bus is rendered. Declares what it accepts and whether it reports back.
audio/mixer.py   Per-bus voices, gain, ducks, transitions, summing.
audio/device.py  PortAudio, or a null that renders and discards.
library/         Resolve a name to PCM. One ingest path, one content-addressed cache.
```

Dependencies point downward only. The mixer knows nothing about HTTP; the library knows nothing
about buses; `core/` knows nothing about ffmpeg.

## How a bus advances

Three cases, and the third is what makes the model media-agnostic rather than audio-shaped.

**The sink reports completion.** Audio when a buffer runs out; the browser when a video ends or an
animation hits its loop count. The bus waits for the signal and takes the next item. This is the
normal path.

**The sink cannot report completion.** The Claude client, where media arrives only as the result of
a tool call and nothing comes back. The bus advances on dispatch and the duration metadata rides
along as advisory values. Not a gap to be closed later — there is no signal to be had.

**The item is open-ended** (`until_replaced`). Nothing will ever signal. The bus holds it and wakes
on one of three things: a stop, something placed behind it, or the sink reporting completion
*anyway*. The third one is easy to miss and matters: nothing ends an open-ended item naturally, so a
completion on one always means something broke — the page failed to decode, the tab went away. Not
listening for it wedges the lane forever on a signal the sink already sent.

Whether holding is *rest* or a *hang* is the bus's call, not the item's. `resting_open_ended: true`
(backdrop, ambience) means this is the steady state. `false` (voice) means it is a hung scene, and
`/status` says so after `PERFORM_STALL_AFTER_S`.

## What stop means

Stop clears the lane: the current item is interrupted **and** everything pending behind it is
dropped. A stop that killed only the current line would let the next one start milliseconds later,
which is not "stopping" to anyone holding the remote.

Dropped items are marked `dropped` and stay in the list. They are reported, never silently
discarded.

At the audio layer every stop is a 5 ms fade rather than a hard mute. Cutting a waveform at a
non-zero sample is a step discontinuity, and a step discontinuity is a broadband click. A click on
every stop trains people not to use stop, which defeats the point.

## The mix graph

One `OutputStream` at 48 kHz stereo float32. Per block the mixer:

1. drains its command deque,
2. pulls frames from each bus's active source,
3. computes each bus's target gain — its own, minus the deepest active duck,
4. ramps toward that target linear-in-dB,
5. sums, soft-clips, publishes a snapshot.

**Ducking is presence-triggered**: while a bus that declares `ducks` has a source, its targets ramp
to `depth_db` and release when it stops. Overlapping ducks take the *deepest*, not the sum — two
−12 dB ducks meaning −24 dB would be a surprise every time.

Ramping in dB rather than in linear amplitude is what makes a duck sound like a duck. A linear ramp
to −12 dB spends most of its time in the last couple of dB, which reads as a lurch and then a crawl.
Within a block the gain is interpolated per-sample, because one gain per block is a step
discontinuity and that is a click again.

### The threading rule

`render` runs on PortAudio's callback thread. Nothing on that thread may block: no locks held across
I/O, no large allocation, no logging (the handler takes a lock).

The two sides talk through `collections.deque`, whose `append` and `popleft` are atomic in CPython:

```
asyncio  --command deque-->  render     (start, stop, gain, config)
render   --done deque---->   asyncio    (item ids that finished)
```

The engine *polls* the done deque rather than being signalled, because signalling means
`call_soon_threadsafe`, and that takes a lock on the thread that must never take one. The cost is up
to one poll interval (~10 ms) between "the line ended" and "start the next one", which is inaudible.

Everything the render thread needs is preallocated or sliced. Nothing there decodes, resamples,
filters, or allocates a large buffer — which is only possible because of the next section.

## Everything expensive happens at ingest

Every asset — a pack file, a path Wander hands over, a `say` recording — passes through one ffmpeg
call and comes out 48 kHz stereo float32 with its treatment already baked in. That single decision
buys:

- **The mixer never resamples or decodes.** Conduct's Piper output is 22.05 kHz mono MP3; the device
  wants 48 kHz stereo float32. Doing that conversion in the render callback would be the most
  expensive thing on the audio thread.
- **Treatments are free at play time.** A treatment is a filter chain, and a biquad is a sequential
  recurrence — `y[n]` depends on `y[n-1]` — so it cannot be vectorized in numpy. Four biquads across
  two channels of a 512-frame block is ~4000 Python-level iterations every 10.7 ms, which will not
  hold the deadline. Baking it in at ingest, keyed into the cache, makes it a dictionary lookup.
- **One code path.** "Pre-generating a theme" is `POST /assets/warm`, which calls exactly the
  function a just-in-time play calls. An unfinished warm costs a few slow items, not a failure.

ffmpeg rather than a Python decoder because it is already a hard dependency of this system —
conduct's Dockerfile apt-installs it for the TTS path — and because "decode anything, resample,
filter, convert" is one invocation.

## What is deliberately absent

**No Postgres, Redis, RQ, or Docker.** Conduct has them because it is multi-tenant, durable, and
cloud-deployable. Perform is one listener on one machine, and playback state is meaningless across a
restart. The only durable thing is the asset cache index — stdlib `sqlite3`, following wander's
`bench/runner/store.py`. Docker Desktop on macOS cannot pass through an audio device anyway, which
is the same reason conduct leaves Ollama on the host.

**No master volume, no transport controls.** The system already has those. The only level control
here is the *relative* one, because no system volume control can express "words stay intelligible
over a siren".

**No generation, and no knowledge that generation exists.** Perform never calls Conduct, never sees
a job id, never polls. It receives assets. The `say` binary is not an exception to this — it takes a
string and returns a file, in the same category as the ffmpeg call in ingest, with no job, no model,
and no provider.

**No cast, no personas, no prompts.** Perform does not know what a "Tier 2 SRE" is. Keeping domain
vocabulary out is what makes it reusable for build notifications and stops it from re-implementing
Wander. The `label` on an item is the seam: Perform logs it and reports it and never interprets it.
