"""The system voice.

Following the precedent conduct set in `tests/unit/test_tts.py` — where the real
dependency cannot be faked, test the validation surface instead and say so. Here
that means: the length limit, the empty-text case, and voice validation all get
tested, and actual synthesis does not, because running `say` in a test suite
would make noise on someone's machine.
"""

from __future__ import annotations

import pytest

from audio import speech
from core.errors import SpeechError, VoiceNotFound


def test_empty_text_is_refused(tmp_path):
    with pytest.raises(SpeechError, match="nothing to say"):
        speech.synthesize("   ", tmp_path)


def test_long_text_is_refused_and_points_at_conduct(tmp_path):
    """The system voice is a notification surface. Long-form belongs in
    conduct's TTS, which was built for it."""
    with pytest.raises(SpeechError, match="conduct"):
        speech.synthesize("x" * (speech.MAX_CHARS + 1), tmp_path)


def test_an_empty_voice_is_always_valid():
    """Empty means the system default, which always exists."""
    speech.validate_voice("")


def test_an_unknown_voice_fails_loudly(monkeypatch):
    """SPEC §8: 'a silent fallback to a default voice means two participants
    sound identical and the listener has no idea why.' Conduct gets this wrong
    today — a bad voice is accepted with a 202 and fails later in the worker."""
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech, "list_voices", lambda: ["Samantha", "Daniel"])

    with pytest.raises(VoiceNotFound, match="not installed"):
        speech.validate_voice("Nonexistent")


def test_an_unknown_voice_suggests_near_misses(monkeypatch):
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech, "list_voices", lambda: ["Samantha", "Daniel"])

    with pytest.raises(VoiceNotFound, match="Samantha"):
        speech.validate_voice("Sam")


def test_a_known_voice_passes(monkeypatch):
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech, "list_voices", lambda: ["Samantha", "Daniel"])
    speech.validate_voice("Daniel")


def test_no_say_binary_is_explained_rather_than_crashing(monkeypatch):
    """Perform's system voice is macOS-only, and the error should say so plus
    point at the path that does work everywhere."""
    monkeypatch.setattr(speech, "say_available", lambda: False)

    with pytest.raises(SpeechError, match="macOS-only"):
        speech.validate_voice("Samantha")


def test_validation_is_lenient_when_the_voice_list_is_unavailable(monkeypatch):
    """If `say -v '?'` fails we cannot prove the voice is wrong, and refusing to
    boot on a hunch would be worse than letting `say` reject it later."""
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech, "list_voices", lambda: [])
    speech.validate_voice("Whatever")
