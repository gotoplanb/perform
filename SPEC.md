# Perform — architecture brief

**What this is:** a scoping and boundaries document for a third local service, to be read
alongside the Conduct and Wander repos. It contains **no implementation detail** — no
endpoints, no schemas, no module layout. Those belong to whoever reads this with the source
in front of them, and anything invented here would be a guess that outranks better
information.

> **Perform is a multichannel playback server.** It renders what it's given, where it's told,
> in the order it's told. It coordinates nothing.

---

## 0. Conformance — read this first

Perform is the third service in an existing system, not a greenfield project. Before writing
code:

- **Read Wander and Conduct and follow their established architectural patterns.** Project
  layout, configuration approach, error handling, logging and telemetry conventions, testing
  style, packaging, and deployment should match what those repos already do. Consistency
  across the three services is worth more than any local improvement in this one.
- **Coordinate the boundaries with those projects directly.** The responsibilities table in
  §2 is a proposal, not a decree. Where it disagrees with what Wander or Conduct already do,
  the existing code wins and this document should be corrected.
- **Push work to whichever service is the natural owner**, even when it's easier to absorb it
  here. The failure mode for a third service is quietly re-implementing the other two.
- **Where a capability already exists upstream, depend on it rather than reproducing it.**
  Conduct already handles synthesis; Wander already handles scenario state.

The goal is clean separation of concerns across three services that look and feel like one
system.

---

## 1. The problem

A Claude-driven scenario — an incident bridge, an IFR flight, a Sierra-style adventure, a
murder mystery — should be experienced, not read. That needs distinguishable voices, sound
effects, imagery, and something that owns the output devices.

Conduct dispatches models and already ships text-to-speech. Wander is the scenario engine,
already speaks MCP, and already holds the content. The missing piece is playback.

---

## 2. Boundaries

| Concern | Owner |
|---|---|
| Who is in the scene, what they say, turn order | Wander |
| The cast list — participant → voice | Wander |
| Submitting generation jobs and collecting results | Wander |
| Deciding what to play when results arrive out of order | Wander |
| Placeholders and stalling while assets aren't ready | Wander |
| Model dispatch, prompts, evaluation, synthesis | Conduct |
| Playback, per-bus ordering, mixing, interruption | **Perform** |
| Treatment — making a bridge sound like a bridge | **Perform** |
| The asset library and its warm cache | **Perform** |
| Output surfaces and the devices behind them | **Perform** |

### Perform's non-goals

- **No generation, and no knowledge that generation exists.** It receives assets. It does not
  know about Conduct, job ids, or polling.
- **No scene coordination.** Out-of-order results, retries, and what to do while waiting are
  Wander's problems; Wander is the only component with the context to solve them well.
- **No cast, no personas, no prompts, no themes.** Perform should not know what a "Tier 2 SRE"
  is, or what a murder mystery is. Keeping domain vocabulary out is what makes it reusable for
  build notifications and stops it from re-implementing Wander.
- **No master volume or transport controls.** The system already has those.
- **No speech recognition, no cloud, no multi-user.** One listener, one machine.

### Why a service and not a Wander module

The thing worth having on day one has nothing to do with scenarios: a Claude Code session that
can say "the migration finished" out loud. Folded into Wander, that only exists when Wander is
running, and therefore doesn't get used. The scenario case is the interesting one; the
notification case is what earns the service a permanent place in the setup.

---

## 3. Core model

**Bus** — a playback lane with its own ordered list. Items on one bus play serially; buses run
concurrently. Voice, ambience, and backdrop at minimum. Buses declare how they interact: an
ambience bus ducks a voice bus by a configured amount so words stay intelligible over a siren.
This relative mix is the one audio control that stays, because no system volume control can
express it. Keep the bus count small and fixed.

Buses differ in resting behavior. A backdrop bus is *expected* to end on an open-ended item —
that's its steady state. A voice bus ending on one is a hung scene.

**Treatment** — a filter or transition preset applied to an individual item: clean, internal
bridge, incident bridge, phone; hard cut, fade, dissolve. A property of the item rather than
the bus, because the same voice bus carries a customer on a phone line and an SRE on the
bridge. Transitions matter more for visuals than audio — a hard cut between backdrops looks
broken in a way a hard cut between lines does not sound broken.

**Item** — an asset plus a target bus, a treatment, a duration policy, and an opaque label.
Perform logs the label and reports it but never interprets it; it exists so Wander's traces and
Perform's logs line up when something plays at the wrong moment.

**Duration** comes from one of four sources, and this is media-agnostic:

- *Intrinsic* — audio, video. The file knows.
- *Repeat count* — a looping animation or ambience bed; one cycle times N.
- *Declared* — a still held for a stated interval.
- *Until replaced* — held until the next item on that bus begins.

**Addressability** — items already placed but not yet played must be individually replaceable
and removable. Wander puts a placeholder in a slot, swaps in the real asset when generation
lands, or drops it if it doesn't. This is a mutable ordered list, not a queue.

**Interruption** — per-bus stop and a global stop. Being unable to shut it up is the most
common reason tools like this get abandoned, and it matters more here because Wander drives
Perform faster than a person can react.

---

## 4. Sinks

A **sink** is where a bus is rendered. Sinks declare their capabilities: which media types they
accept, whether they are push or pull, and whether they report completion. Buses route to sinks.

- **Local audio.** Push. Reports completion. The system already provides the device and the
  volume control, so there's nothing to build but the routing.
- **Local visual surface.** A small local web server rendering to a browser page. Push. Reports
  completion — the page can report that a video ended or an animation hit its loop count, which
  is what keeps visuals sequencing exactly like audio. Server-to-client push with automatic
  reconnect is the right shape; validate the specific mechanism against how Wander and Conduct
  already handle streaming.
- **Claude client.** Pull, and the odd one out. Media can only arrive as the result of a tool
  call and nothing reports back, so there is no completion signal and there cannot be one.
  Duration and loop metadata are advisory here, passed through for the client to honor. Its
  sequencing unit is the conversational turn, not the clock. Practical limits: images work far
  better than video, and payload size is a real constraint.

**Audio and video stay separate.** Sound comes out of the local audio sink; video on the visual
surface carries no audio track. This is the correct architecture and it happens to sidestep
browser autoplay restrictions entirely, since muted animation autoplays unconditionally. A
video arriving with a live audio track will fail silently rather than error — strip or mute at
the sink, and log when you do.

---

## 5. The asset library

Assets are requested by logical name, never by filename, so scenes stay portable across
machines. Organize them as **packs**: a folder of media plus a manifest, discovered at startup,
namespaced to avoid collisions. Dropping a folder in should be the whole install.

**Manifests carry a media type from the start.** Retrofitting one into a key scheme that
assumed audio is miserable.

**Treat the library as a warm cache, not a build artifact.** One path for all assets, keyed by
content plus voice, persisted to disk, with fixed assets pinned so they never evict. "Pre-generating
a theme" then becomes "warming the cache before the scene," using the same code path as
just-in-time delivery, and it degrades gracefully — an unfinished warm just means the first few
items are slower.

This matters more than it sounds. In a Sierra-style game most of what gets said is a fixed
response table spoken by one narrator voice, repeated constantly. Pre-rendered assets are the
primary path there and generation is the exception.

Ship no media files. Generate the handful of simple tones programmatically; richer packs are
user-supplied. That sidesteps licensing questions and means something works before a single
asset is sourced.

Keep keys about the asset, not its position in a scene. `sre.greeting` survives; `scene3.turn1.sre`
means renaming a scene orphans assets and buries a scene format inside a naming convention.

---

## 6. Sequencing

1. **Local system voice, one bus, stop.** No dependency on Conduct or Wander. Useful immediately.
2. **Play supplied assets**, with completion signalling. The point at which Wander can drive it.
3. **Asset library** with media types and the warm cache.
4. **Visual surface** as a second sink.
5. **Multiple buses, ducking, treatments, transitions.**

Step 5 is the fun part and therefore the most likely to consume the project before step 1 is
usable. Resist accordingly.

Wander should talk to Perform as a service rather than through MCP — that's service-to-service.
An MCP surface is worth adding for the direct-from-Claude-Code notification case, which is a
different consumer with different needs.

---

## 7. Open decisions

- **Where the cast list lives.** This brief puts participant → voice in Wander. The alternative
  is a named-voice registry in Conduct, so any client can ask for "the ops manager voice" without
  carrying the mapping. Decide before Wander hardcodes it.
- **How assets get from Wander to Perform** — file paths on a shared host, bytes over the wire, or
  a URL Perform fetches. Local paths are simplest and probably right, but they assume same-host
  deployment forever.
- **Whether Conduct needs a change.** Its TTS was built for long-form work; whether short
  conversational utterances round-trip fast enough is a question for the code. If generation
  latency dominates pacing, the fix belongs in Conduct.
- **Whether Wander's existing MCP already covers scene sequencing**, and if so, where the
  Wander/Perform line actually falls. This brief assumes it doesn't; verify.
- **Clock drift** between the audio sink and the visual surface once they're separate processes.
  Irrelevant at ten seconds, visible at ten minutes.
- **Auth.** Loopback-only with no auth is fine to start. Anything exposed over a tunnel needs a
  shared secret first.
- **Asset name stability.** Once scenes reference assets by name, renaming breaks saved scenes.
  Aliasing is cheap now and annoying to retrofit.

---

## 8. Constraints for whoever implements this

- **Fully testable with no audio device and no display.** A test run should never make noise or
  open a window. Bus interaction and completion signalling are the behaviors most worth testing
  and the most likely to break silently.
- **Failures visible, never silent.** A dropped item, a missing asset name, a stalled bus, a
  muted-on-arrival video — each obvious from status output. "Why is it quiet" should take seconds
  to answer.
- **Validate at startup and fail loudly** rather than falling back. A silent fallback to a default
  voice means two participants sound identical and the listener has no idea why.
