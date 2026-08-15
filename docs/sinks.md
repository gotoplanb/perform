# Sinks

A sink is where a bus is rendered. Buses route to sinks; sinks declare what they can do.

| sink | accepts | mode | reports completion |
|---|---|---|---|
| `local_audio` | audio | push | yes — from the mixer |
| `visual` | image, video | push | yes — the browser posts back |
| `claude` | image, video | **pull** | **never, and cannot** |
| `null` | everything | push | on command (tests) |

## Capabilities are declared, not assumed

The engine reads them at startup and refuses a bus routed to a sink that cannot take its media —
before the device opens and before the first request is served. The per-item check happens at
*placement*, so a caller gets a 409 at the point of the mistake rather than silence forty seconds
into a scene.

## The completion asymmetry

This is the interesting part of the design, and it is why `reports_completion` is a declared
capability rather than an assumption.

**`local_audio`** knows exactly when a buffer ran out. Exact, sample-accurate, free.

**`visual`** does not know — the browser does. The page reports that a video ended or that an
animation hit its loop count, and taking its word for it is what keeps visuals sequencing like audio
rather than drifting. Duration therefore travels *to* the page rather than being enforced
server-side: only the page knows when a cycle actually completed.

The engine does arm a backstop timer for `declared` durations. If the backstop wins, that gets said
out loud — a sink that silently stopped reporting would otherwise present as buses that gradually
all go quiet.

**`claude`** cannot report, ever. Media arrives only as the result of a tool call and nothing comes
back. So:

- the bus advances on dispatch,
- duration and loop metadata are passed through as advisory values for the client to honor,
- the sequencing unit is the conversational turn, not the clock.

`reports_completion = False` here is not a TODO. There is no mechanism by which a chat client could
tell us a picture finished being looked at.

## Audio and video stay separate

Sound comes out of `local_audio`. Video on the visual surface carries no audio track — every
`<video>` is `muted`, unconditionally.

This is the correct architecture and it happens to sidestep browser autoplay policy entirely, since
muted media autoplays without a gesture. That is a happy side effect, not the reason.

A video arriving with a live audio track is detected with `ffprobe` at ingest, played silently, and
reported in `/status` and on the surface itself. It is the one case SPEC §4 singles out as failing
silently rather than erroring.

## The visual surface

`GET /surface` is a page; `GET /surface/events` is an SSE stream; `POST /surface/complete` is how the
page reports back. Events out over SSE, completions back over an ordinary POST — SSE is
one-directional and a plain POST is both sufficient and consistent with how the rest of this system
talks.

**This is the only streaming endpoint in the three services.** Conduct is `202 + poll_url` end to
end and Wander is HTMX fragment swaps over plain POST; neither streams anything. SSE was adopted
anyway because the browser cannot poll — it has to show item *N* the instant the audio bus reaches
it, and a poll interval is exactly the drift being avoided — and because `EventSource` reconnects on
its own with no client code.

Two behaviors worth knowing:

- **A late subscriber is caught up.** A tab that connects or reconnects mid-scene is replayed the
  current item per bus, so a refresh does not blank the screen until the next item.
- **Nobody attached is a first-class state.** An SSE stream with no subscriber is a laptop with the
  tab closed, not an error. `/status` reports the subscriber count and warns when an item is placed
  with nothing listening — it is the fastest answer to "why is the screen blank", and without it the
  bus looks perfectly healthy while rendering to nothing.

### Testing note

The SSE route cannot be driven through httpx's `ASGITransport`, which buffers a response body to
completion before returning it — an endless event generator yields nothing and the request hangs.
That is a limitation of in-process ASGI testing, not of the route. So the sink's pub/sub, replay,
backpressure, and completion routing are unit-tested directly against `VisualSink`, and the wire
framing is checked by hand against a real uvicorn.

## The claude sink

Pull, buffered, bounded. Items go into a deque of 8 and the `next_visual` MCP tool drains it when
the model happens to call one. A turn may pick up three items or none; both are correct.

The buffer drops the **oldest** on overflow, because in a conversation the newest image is almost
always the relevant one and a backlog from four turns ago is noise.

Payload size is enforced here rather than discovered downstream: past 4 MB an item is refused with a
reason rather than truncated, and video is accepted but warned about because images work far better.
