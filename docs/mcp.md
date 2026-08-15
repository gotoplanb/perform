# Driving Perform from Claude Code

Perform exposes MCP for one consumer: a coding session that wants to make a noise. Wander talks to
Perform as a *service* over HTTP — that is service-to-service and has different needs.

## Setup

```bash
claude mcp add perform -- uv run --directory /path/to/perform perform-mcp
```

The MCP server talks to a *running* Perform over HTTP rather than importing the engine, because two
processes must not both open the audio device and the server is the one that owns it. So `make run`
has to be up. If it is not, every tool returns a plain error saying so and how to fix it, rather
than a stack trace:

```json
{
  "error": "perform is not running at http://127.0.0.1:8770",
  "remediation": "start it with `make run` in the perform repo, or set PERFORM_BASE_URL"
}
```

`PERFORM_BASE_URL` and `PERFORM_TOKEN` are read from the environment.

## Tools

| tool | what it does |
|---|---|
| `speak(text, voice?, bus?)` | say something out loud through the OS voice |
| `play(asset, bus?, label?)` | play a named asset — a built-in tone or anything a pack declares |
| `stop(bus?)` | shut up. Empty bus means everything |
| `status()` | what is playing, what is queued, and why it might be quiet |
| `list_assets()` | every name Perform can play, and whether it is warm |
| `voices()` | the system voices `say` can use |

Errors are **returned**, not raised — a raised exception in an MCP tool is a protocol error, where a
returned one is something the model can act on. That is wander's convention in `app/mcp_server.py`
and it is followed here.

`status()` returns a trimmed payload rather than the full `/status` document: the per-item detail
that is right for a dashboard is noise in a conversation. `quiet_because` is the first field for the
same reason it exists at all.

## What this is for

```
"run the migration, then tell me out loud when it's done"
"play tone.error if the build fails"
"say 'tests are green' when they pass"
```

The point is the notification case. A long build, a slow migration, a test suite you walked away
from. SPEC §2 argues this is what earns the service a permanent place in the setup — the scenario
case is the interesting one, but folded into Wander it would only exist when Wander is running and
therefore would not get used.

Repeated notifications are free: `say` output lands in the warm cache keyed by content, so the
hundredth "tests passed" is a dictionary lookup.

## What this is not for

Placing an item with a treatment, a duration policy, and a transition; replacing a placeholder;
warming a theme before a scene. That is Wander's job, and Wander speaks HTTP. The MCP surface is
deliberately not a mirror of the API — keeping it small is what keeps it usable in a conversation.
