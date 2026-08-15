"""The system voice: `say`, and nothing more than `say`.

SPEC §6 step 1 is *"local system voice, one bus, stop — no dependency on Conduct
or Wander. Useful immediately."* This module is that, and the reason it is only
forty lines of real work is that Perform is a player, not a synthesizer.

## Why this is not a generation pipeline

SPEC §2 is unambiguous: *"No generation, and no knowledge that generation
exists. It receives assets."* `say` does not violate that, and the distinction
is worth being precise about rather than hand-waving:

- Perform does not know what a voice is *for*. There is no cast list here, no
  participant mapping, no persona. `say` takes a string and gives back a file.
- There is no job, no id, no polling, no provider, no model. It is a subprocess
  that writes an AIFF, in the same category as the ffmpeg call in ingest.py.
- The real path — the one Wander drives — hands over a *file* that Conduct
  already synthesized. Perform never asks anyone to make audio for it.

What `say` buys is the case SPEC §2 says earns the service its permanent place:
a Claude Code session that can say "the migration finished" out loud, with
Wander not running and Conduct not involved. Folded into Wander that only exists
when Wander does, and therefore doesn't get used.

## The output goes straight into the warm cache

`say` writes AIFF; the mixer wants normalized PCM. Rather than a special path,
the recording is handed to `Library.resolve()` as an ordinary local file — the
same entry point Wander's handovers use. The consequence is that a repeated
notification is a *cache hit*: "tests passed" spoken for the hundredth time
costs a dictionary lookup, because the cache is keyed on content and the content
did not change.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from core.errors import SpeechError, VoiceNotFound

log = logging.getLogger(__name__)

# `say` is not slow, but a wedged subprocess would hang the bus that is waiting
# on it. Generous enough for a paragraph, short enough to notice.
SAY_TIMEOUT_S = 60

# Long text belongs in a file that something else generated. This is a
# notification surface: past a couple of paragraphs the right answer is Conduct,
# whose TTS was built for long-form work.
MAX_CHARS = 2000


def say_available() -> bool:
    return shutil.which("say") is not None


def list_voices() -> list[str]:
    """The voices installed on this machine, per `say -v '?'`.

    Used at startup to validate the configured voice, which is the whole point:
    SPEC §8 wants a bad voice to fail the boot, because *"a silent fallback to a
    default voice means two participants sound identical and the listener has no
    idea why."*
    """
    if not say_available():
        return []
    try:
        result = subprocess.run(
            ["say", "-v", "?"], capture_output=True, text=True, timeout=15
        )
    except subprocess.SubprocessError as e:
        log.warning("could not list say voices: %s", e)
        return []
    if result.returncode != 0:
        return []
    # Lines look like: "Samantha           en_US    # Hello, my name is Samantha."
    return [line.split()[0] for line in result.stdout.splitlines() if line.strip()]


def validate_voice(voice: str) -> None:
    """Raise unless `voice` is installed. Empty means the system default, which
    always exists."""
    if not voice:
        return
    if not say_available():
        raise SpeechError(
            "`say` not found — Perform's system voice is macOS-only. Unset "
            "PERFORM_SAY_VOICE and use POST /buses/{bus}/items with a file "
            "instead, which is the path Wander drives anyway."
        )
    voices = list_voices()
    if voices and voice not in voices:
        near = [v for v in voices if v.lower().startswith(voice[:3].lower())][:5]
        hint = f" Did you mean {', '.join(near)}?" if near else ""
        raise VoiceNotFound(
            f"say voice {voice!r} is not installed on this machine.{hint} "
            f"Run `make voices` for the full list, or leave PERFORM_SAY_VOICE "
            f"empty for the system default."
        )


def synthesize(
    text: str,
    dest_dir: Path,
    *,
    voice: str = "",
    rate: int = 0,
) -> Path:
    """Speak `text` into an AIFF under `dest_dir` and return the path.

    The caller hands the result to the library as an ordinary local file. AIFF
    because it is what `say -o` writes natively; converting here would duplicate
    the ffmpeg call that ingest is about to make anyway.
    """
    if not text.strip():
        raise SpeechError("nothing to say — text was empty")
    if len(text) > MAX_CHARS:
        raise SpeechError(
            f"text is {len(text)} chars, over the {MAX_CHARS} limit. The system "
            f"voice is a notification surface; long-form belongs in conduct's "
            f"TTS, whose output you can hand to POST /buses/{{bus}}/items."
        )
    if not say_available():
        raise SpeechError(
            "`say` not found — Perform's system voice is macOS-only. "
            "Hand it a file instead: POST /buses/{bus}/items."
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    # mkstemp rather than NamedTemporaryFile: `say` writes this file itself, so
    # we want the name and not an open handle, and a handle left open on macOS
    # would just be one more thing to leak on the error paths below.
    handle, name = tempfile.mkstemp(suffix=".aiff", dir=dest_dir)
    os.close(handle)
    out = Path(name)

    cmd = ["say", "-o", str(out)]
    if voice:
        cmd += ["-v", voice]
    if rate:
        cmd += ["-r", str(rate)]
    cmd += ["--", text]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=SAY_TIMEOUT_S)
    except subprocess.TimeoutExpired as e:
        out.unlink(missing_ok=True)
        raise SpeechError(f"say timed out after {SAY_TIMEOUT_S}s") from e

    if result.returncode != 0:
        out.unlink(missing_ok=True)
        stderr = result.stderr.strip()[:200]
        if "voice" in stderr.lower():
            raise VoiceNotFound(f"say rejected voice {voice!r}: {stderr}")
        raise SpeechError(f"say failed: {stderr}")

    if not out.is_file() or out.stat().st_size == 0:
        out.unlink(missing_ok=True)
        raise SpeechError("say produced no audio")

    return out
