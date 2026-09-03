"""The `say` subprocess path.

conduct's `tests/unit/test_tts.py` sets the precedent: where the real dependency
cannot be faked, test the validation surface and say so. But "cannot be faked"
was doing too much work here — the *argv construction* and the *failure
handling* are ordinary logic, and leaving them untested meant the day-one
surface sat at 40%.

What is still not tested is `say` actually producing audio. That needs macOS and
would make noise on the machine running the suite, which SPEC §8 forbids.
"""

from __future__ import annotations

import subprocess

import pytest

from audio import speech
from core.errors import SpeechError, VoiceNotFound


@pytest.fixture
def fake_say(monkeypatch, tmp_path):
    """Replace the `say` binary with a recorder that writes a plausible file."""
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # `say -o <path>` writes the file; mimic that so the caller's existence
        # and size checks see what they would really see.
        out = cmd[cmd.index("-o") + 1]
        with open(out, "wb") as fh:
            fh.write(b"FORM....AIFF")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    return calls


def test_it_writes_a_file_and_returns_the_path(fake_say, tmp_path):
    path = speech.synthesize("hello", tmp_path)
    assert path.exists()
    assert path.suffix == ".aiff"


def test_the_text_is_passed_after_a_double_dash(fake_say, tmp_path):
    """Without `--`, a line starting with a hyphen would be parsed as a flag —
    and "-- the migration finished" is exactly the kind of thing a notification
    contains."""
    speech.synthesize("-v is not a flag here", tmp_path)
    cmd = fake_say[0]
    assert cmd[-2] == "--"
    assert cmd[-1] == "-v is not a flag here"


def test_a_voice_is_passed_through(fake_say, tmp_path):
    speech.synthesize("hi", tmp_path, voice="Samantha")
    assert "-v" in fake_say[0] and "Samantha" in fake_say[0]


def test_no_voice_means_no_flag(fake_say, tmp_path):
    """An empty `-v ''` is not the same as leaving the system default alone."""
    speech.synthesize("hi", tmp_path)
    assert "-v" not in fake_say[0]


def test_a_rate_is_passed_through(fake_say, tmp_path):
    speech.synthesize("hi", tmp_path, rate=200)
    assert "-r" in fake_say[0] and "200" in fake_say[0]


def test_a_zero_rate_means_no_flag(fake_say, tmp_path):
    speech.synthesize("hi", tmp_path, rate=0)
    assert "-r" not in fake_say[0]


def test_the_destination_directory_is_created(fake_say, tmp_path):
    nested = tmp_path / "blobs" / "speech"
    speech.synthesize("hi", nested)
    assert nested.is_dir()


# --- failures -----------------------------------------------------------


def test_a_nonzero_exit_raises_and_cleans_up(monkeypatch, tmp_path):
    """A half-written temp file left behind on every failure would fill the
    cache directory with garbage nobody looks at."""
    written: list[str] = []

    def failing_run(cmd, **kwargs):
        out = cmd[cmd.index("-o") + 1]
        written.append(out)
        open(out, "wb").close()
        return subprocess.CompletedProcess(cmd, 1, "", "say: something went wrong")

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", failing_run)

    with pytest.raises(SpeechError, match="say failed"):
        speech.synthesize("hi", tmp_path)
    assert not any(__import__("os").path.exists(p) for p in written)


def test_a_voice_rejection_is_raised_as_voice_not_found(monkeypatch, tmp_path):
    """So the caller can tell 'bad voice name' from 'say is broken' — the first
    is a config error with a fix, the second is not."""

    def failing_run(cmd, **kwargs):
        open(cmd[cmd.index("-o") + 1], "wb").close()
        return subprocess.CompletedProcess(cmd, 1, "", "Voice 'Nope' not found")

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", failing_run)

    with pytest.raises(VoiceNotFound):
        speech.synthesize("hi", tmp_path, voice="Nope")


def test_a_timeout_is_raised_as_a_speech_error(monkeypatch, tmp_path):
    def timing_out(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, speech.SAY_TIMEOUT_S)

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", timing_out)

    with pytest.raises(SpeechError, match="timed out"):
        speech.synthesize("hi", tmp_path)


def test_an_empty_output_file_is_an_error_not_a_silent_success(monkeypatch, tmp_path):
    """`say` can exit 0 having written nothing. Passing that through would put a
    zero-length blob in the cache and a silent item on the bus."""

    def empty_run(cmd, **kwargs):
        open(cmd[cmd.index("-o") + 1], "wb").close()
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", empty_run)

    with pytest.raises(SpeechError, match="no audio"):
        speech.synthesize("hi", tmp_path)


# --- listing voices -----------------------------------------------------


def test_list_voices_parses_the_say_output(monkeypatch):
    sample = (
        "Samantha           en_US    # Hello, my name is Samantha.\n"
        "Daniel             en_GB    # Hello, my name is Daniel.\n"
        "\n"
    )
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(
        speech.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, sample, ""),
    )
    assert speech.list_voices() == ["Samantha", "Daniel"]


def test_list_voices_is_empty_without_the_binary(monkeypatch):
    monkeypatch.setattr(speech, "say_available", lambda: False)
    assert speech.list_voices() == []


def test_list_voices_survives_a_failing_say(monkeypatch):
    """Validation is lenient when the list is unavailable — refusing to boot on
    a hunch would be worse than letting `say` reject the name later."""
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(
        speech.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", "boom"),
    )
    assert speech.list_voices() == []


def test_list_voices_survives_a_subprocess_error(monkeypatch):
    def boom(*a, **k):
        raise subprocess.SubprocessError("no")

    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech.subprocess, "run", boom)
    assert speech.list_voices() == []
