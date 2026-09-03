"""`POST /speak` — the day-one surface, end to end.

SPEC §2 argues this is what earns Perform a permanent place in the setup: a
Claude Code session that can say "the migration finished" out loud with Wander
not running and Conduct not involved. It was the least-covered route in the
repo, which is a poor showing for the one people actually use.

`say` itself is replaced with a recorder that writes a real WAV — the suite must
never make noise (SPEC §8) and `say` is macOS-only. Everything downstream of it
is the genuine article: ingest, the warm cache, the mixer, the bus.
"""

from __future__ import annotations

import hashlib

import numpy as np
import pytest
import soundfile as sf

from audio import speech
from tests.conftest import SAMPLE_RATE, requires_ffmpeg
from tests.integration.conftest import drain

pytestmark = requires_ffmpeg

#: NOT "FLOAT". libsndfile writes a PEAK chunk into float-subtype WAVs, and that
#: chunk carries a UNIX timestamp — so writing byte-identical audio either side
#: of a second boundary produces two different files, two different content
#: hashes, and a cache miss where the test expects a hit. It failed roughly one
#: run in ten before this was tracked down. Integer subtypes carry no PEAK
#: chunk and are byte-stable.
#:
#: Only the *fixtures* need this. Production never re-encodes a source file: it
#: hashes whatever `say` produced, and `say` output for identical text is
#: identical, which is what makes a repeated notification a cache hit.
DETERMINISTIC_SUBTYPE = "PCM_16"


@pytest.fixture
def fake_say(monkeypatch):
    """Stand in for `say`, writing a real (silent-ish) recording.

    A quiet sine rather than digital silence: an all-zero file would make
    "did anything reach the mixer" unanswerable, since silence is also what a
    broken path produces.
    """
    calls: list[dict] = []

    def fake_synthesize(text, dest_dir, *, voice="", rate=0):
        calls.append({"text": text, "voice": voice, "rate": rate})
        dest_dir.mkdir(parents=True, exist_ok=True)
        # The *audio* has to differ per text, not just the filename. Keying the
        # length on len(text) was not enough — "tests passed" and "tests failed"
        # are both twelve characters, so they produced byte-identical files and
        # the second one was a legitimate cache hit. Pitch is derived from a
        # stable digest of the text so distinct utterances are distinct audio.
        digest = int(hashlib.sha256(text.encode()).hexdigest()[:8], 16)
        seconds = 0.05 + 0.001 * len(text)
        freq = 220 + digest % 400
        t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
        pcm = (0.25 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
        path = dest_dir / f"fake-{digest}.wav"
        sf.write(str(path), pcm, SAMPLE_RATE, subtype=DETERMINISTIC_SUBTYPE)
        return path

    monkeypatch.setattr(speech, "synthesize", fake_synthesize)
    return calls


async def test_speak_places_an_item_and_makes_sound(client, engine, device, fake_say):
    r = await client.post("/speak", json={"text": "the migration finished"})
    assert r.status_code == 202
    body = r.json()
    assert body["bus"] == "voice"
    assert body["duration_s"] > 0

    await drain(engine, device, 0.05)
    assert float(np.max(np.abs(device.pull_seconds(0.05)))) > 0.01


async def test_the_text_becomes_the_label_when_none_is_given(client, engine, device, fake_say):
    """Perform never interprets a label, but a human reading /status at 2am
    should not have to correlate ids to know what was said."""
    r = await client.post("/speak", json={"text": "tests are green"})
    await drain(engine, device, 0.05)

    detail = (await client.get(f"/items/{r.json()['item_id']}")).json()
    assert detail["label"] == "tests are green"


async def test_a_long_text_label_is_truncated(client, engine, device, fake_say):
    long_text = "word " * 40
    r = await client.post("/speak", json={"text": long_text})
    detail = (await client.get(f"/items/{r.json()['item_id']}")).json()
    assert len(detail["label"]) <= 48
    assert detail["label"].endswith("…")


async def test_an_explicit_label_wins(client, engine, device, fake_say):
    r = await client.post("/speak", json={"text": "hello", "label": "greeting"})
    detail = (await client.get(f"/items/{r.json()['item_id']}")).json()
    assert detail["label"] == "greeting"


# Each cache test uses text unique to itself. The library is per-test (a fresh
# tmp_path), but the integration `app` fixture hands out the *module-level*
# FastAPI object with its state swapped, so two tests sharing an utterance would
# be relying on that swap being perfectly clean. Unique text removes the
# question rather than answering it.
async def test_the_second_identical_utterance_is_a_cache_hit(client, engine, device, fake_say):
    """The property that makes a repeated notification free: keyed on content,
    and the content did not change."""
    first = await client.post("/speak", json={"text": "repeat-case utterance one"})
    second = await client.post("/speak", json={"text": "repeat-case utterance one"})

    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True


async def test_a_different_utterance_is_not_a_hit(client, engine, device, fake_say):
    await client.post("/speak", json={"text": "distinct-case alpha"})
    other = await client.post("/speak", json={"text": "distinct-case bravo entirely"})
    assert other.json()["cache_hit"] is False


async def test_the_cache_is_keyed_on_the_audio_not_the_text(
    client, engine, device, monkeypatch
):
    """Worth pinning down, because it is the surprising half of the design and
    this test only exists because a fixture accidentally demonstrated it.

    Two different strings that synthesize to identical bytes share one blob.
    That is correct — the cache is content-addressed, and re-normalizing
    identical audio under a second key would waste the entry. It also means a
    change in `say`'s output (a voice update, say) invalidates the cache
    automatically, which is what you want.
    """
    def constant_audio(text, dest_dir, *, voice="", rate=0):
        dest_dir.mkdir(parents=True, exist_ok=True)
        t = np.arange(int(0.05 * SAMPLE_RATE)) / SAMPLE_RATE
        pcm = (0.25 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        path = dest_dir / f"same-{abs(hash(text)) % 10**8}.wav"
        sf.write(str(path), pcm, SAMPLE_RATE, subtype=DETERMINISTIC_SUBTYPE)
        return path

    monkeypatch.setattr(speech, "synthesize", constant_audio)

    first = await client.post("/speak", json={"text": "alpha"})
    second = await client.post("/speak", json={"text": "bravo"})

    assert first.json()["cache_hit"] is False
    assert second.json()["cache_hit"] is True, (
        "identical audio under two names should share one blob — if this fails, "
        "check that the fixture's WAV encoder is byte-deterministic "
        f"(subtype={DETERMINISTIC_SUBTYPE})"
    )


async def test_speaking_onto_another_bus(client, engine, device, fake_say):
    r = await client.post("/speak", json={"text": "over here", "bus": "ambience"})
    assert r.status_code == 202
    assert r.json()["bus"] == "ambience"


async def test_an_unknown_bus_is_a_404(client, fake_say):
    r = await client.post("/speak", json={"text": "hello", "bus": "nope"})
    assert r.status_code == 404


async def test_the_voice_is_reported_back(client, engine, device, fake_say):
    r = await client.post("/speak", json={"text": "hello", "voice": "Samantha"})
    assert r.json()["voice"] == "Samantha"
    assert fake_say[0]["voice"] == "Samantha"


async def test_no_voice_reports_the_system_default(client, engine, device, fake_say):
    assert (await client.post("/speak", json={"text": "hi"})).json()["voice"] == (
        "(system default)"
    )


async def test_empty_text_is_rejected_by_validation(client, fake_say):
    r = await client.post("/speak", json={"text": ""})
    assert r.status_code == 422


async def test_a_speech_failure_becomes_a_422_not_a_500(client, monkeypatch):
    """A `say` that cannot speak is a caller-facing problem with a fix, not an
    internal error."""
    from core.errors import SpeechError

    def boom(*args, **kwargs):
        raise SpeechError("say is not available on this machine")

    monkeypatch.setattr(speech, "synthesize", boom)

    r = await client.post("/speak", json={"text": "hello"})
    assert r.status_code == 422
    assert "not available" in r.json()["detail"]


async def test_the_temp_recording_does_not_linger(client, engine, device, fake_say, app):
    """The AIFF is normalized into the cache under its content hash; the
    recording itself is not the artifact and should not accumulate."""
    from pathlib import Path

    await client.post("/speak", json={"text": "hello"})
    speech_dir = Path(app.state.library.blobs_dir) / "speech"
    leftovers = list(speech_dir.glob("*")) if speech_dir.exists() else []
    assert not leftovers, f"left behind: {leftovers}"


async def test_voices_endpoint_reports_what_is_installed(client, monkeypatch):
    monkeypatch.setattr(speech, "say_available", lambda: True)
    monkeypatch.setattr(speech, "list_voices", lambda: ["Samantha", "Daniel"])

    body = (await client.get("/voices")).json()
    assert body["available"] is True
    assert body["count"] == 2
    assert "Samantha" in body["voices"]
