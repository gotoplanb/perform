# Perform — Claude Code guidance

## Commit & push workflow

Once a change is green — the pre-push gate passes and, where SonarQube is
reachable, its quality gate returns `OK` — you are authorized to commit and push
to `origin/main` without stopping to ask first.

The standing bar before commit/push:

1. `uv run ruff check .` is clean
2. `uv run python scripts/check_secrets.py` finds nothing
3. `uv run pytest` passes **and** meets the coverage floor (`fail_under` in
   `pyproject.toml`)
4. `make sonar-scan` gate returns `OK`, when Watchtower is running

If any of those fail, fix the root cause and re-run — do not push red.

`make install-hooks` wires 1–4 into `pre-commit` (fast subset) and `pre-push`
(everything). The same bar runs in GitHub Actions, so `--no-verify` buys a local
shortcut and not an exemption.

## Where the gate lives

| Check | pre-commit | pre-push | CI |
|---|---|---|---|
| secret scan | staged only | full tree | full tree |
| `.env` / media not staged | ✓ | — | — |
| ruff | staged `.py` | whole tree | whole tree |
| unit tests | ✓ | — | ✓ (fast job) |
| full suite + coverage floor | — | ✓ | ✓ |
| SonarQube quality gate | — | if reachable | if `SONAR_HOST_URL` is set |

Sonar is skipped rather than failed when unreachable, in both the hook and CI. A
local observability stack being down should not block a push, and a missing
optional integration should not turn every pull request red.

## Things that are easy to get wrong here

**The suite must never open an audio device or a window.** SPEC §8 is a hard
constraint, not a preference. `NullDevice` renders through the real mix graph
and discards it; `NullSink` + `ManualClock` drive sequencing by hand. If you
find yourself wanting to `pkill afplay` after a test run, the test is wrong.

**A skipped test is not a passing test.** `tests/conftest.py::requires_ffmpeg`
skips locally but *fails* under `CI=1`, because eleven surface tests silently
vanishing while the run reports green is worse than a red build. Any new skip
condition should follow the same shape, and CI fails if anything skips at all.

**`sonar.sources` is an explicit list**, so a new top-level module can escape
static analysis silently. `tests/unit/test_sonar_config.py` is the tripwire —
it also asserts that Sonar's coverage exclusions and pyproject's `omit` agree,
because when those drift the local floor and the Sonar gate start arguing about
the same code.

**FastMCP's `@mcp.tool` replaces the module-level name with a `FunctionTool`.**
`mcp_server.speak` is an object, not the function; `.fn` is the callable. The
failure mode is `TypeError: 'FunctionTool' object is not callable`, which does
not obviously point at the decorator.

**The repo ships no media** (SPEC §5). The pre-commit hook enforces it — the
built-in tones are synthesized numerically, and anything else belongs in a pack
under `packs/`, which is gitignored.

## Coverage: what the floor does and does not claim

`fail_under = 95` locks in what the suite achieves and stops regressions. Raise
it when the number rises. Never lower it to make a red run green.

Two things that looked untestable and were not, in case the instinct recurs:

- **`PortAudioDevice`** does not need a sound card. The native import is lazy —
  `import sounddevice` lives inside `start()` — so a stub in `sys.modules`
  covers the stream construction, the callback, the xrun counter and teardown.
  Those are our logic; PortAudio's is not ours to test.
- **The SSE generator** cannot be driven through httpx's `ASGITransport`, which
  buffers a response body to completion and so hangs forever on an endless
  stream. Calling the route and consuming `response.body_iterator` has neither
  problem. Only the HTTP framing around it is still checked by hand.

What remains uncovered is defensive branches and a few paths that only occur
under a race. If you are about to write `# pragma: no cover`, say why in the
same breath — the existing ones all name a reason.

## Running it

Perform owns a machine's speakers, so it does not belong in a harness-managed
background task — it never exits, and it will still be running days later. Start
it detached:

```bash
nohup uv run uvicorn main:app --host 127.0.0.1 --port 8770 > perform.log 2>&1 &
```

`perform.log` is gitignored. Stop it with `pkill -f "uvicorn main:app"`.
