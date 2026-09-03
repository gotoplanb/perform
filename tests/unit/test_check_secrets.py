"""The secret scanner.

A scanner nobody has seen catch anything is indistinguishable from one that
matches nothing, and it fails open — a broken regex reports "clean" forever and
the hook it guards becomes decoration. So every rule gets a positive case, and
every placeholder form that must *not* fire gets a negative one.

This file necessarily contains secret-shaped strings, so it is also the working
demonstration of the allowlist marker: rather than exempting `tests/` wholesale
— which would blind the scanner to a real key pasted into a fixture — each
fixture line carries `# pragma: allowlist secret`. If the marker ever stops
working, this file starts failing the very hook it tests, loudly.
"""

from __future__ import annotations

import pytest

from scripts.check_secrets import ALLOW_MARKER, RULES, _should_scan, main, scan_text

# Assembled at runtime so the *shape* of a real key never appears as a literal.
FAKE_HEX40 = "a" * 40
FAKE_GH = "gh" + "p_" + "b" * 36
FAKE_CDT = "cdt_" + "kJ8sPq2mNv5xR7wTz3Yb1Ac"
FAKE_TOKEN = "a3f9c2e18b7d4a6f90e2c5b8d1f4a7c3"
FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9." + "c" * 20


@pytest.mark.parametrize(
    "line",
    [
        f"PERFORM_TOKEN={FAKE_TOKEN}",  # pragma: allowlist secret
        f'token = "{FAKE_CDT}"',
        f"SONAR_TOKEN=sqp_{FAKE_HEX40}",
        # pragma: allowlist secret
        "postgresql+asyncpg://user:hunter2@localhost:5432/db",
        # pragma: allowlist secret
        "-----BEGIN OPENSSH PRIVATE KEY-----",
        "AKIAIOSFODNN7EXAMPLE",  # pragma: allowlist secret
        FAKE_GH,
        f'headers = {{"Authorization": "Bearer {FAKE_JWT}"}}',  # pragma: allowlist secret
    ],
)
def test_real_looking_credentials_are_flagged(line):
    assert scan_text(line, "probe"), f"missed: {line[:40]}"


@pytest.mark.parametrize(
    "line",
    [
        "PERFORM_TOKEN=",  # the .env.example template form
        "PERFORM_TOKEN=changeme",
        "PERFORM_TOKEN=your-token-here",
        "PERFORM_HOST=127.0.0.1",
        "# Optional shared secret. Unset means no auth, which is fine on loopback.",
        # A DSN with no password — crossover's own CI uses exactly this shape.
        "postgresql+asyncpg://crossover@localhost:5432/crossover",
        "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317",
    ],
)
def test_placeholders_and_ordinary_config_are_not_flagged(line):
    assert not scan_text(line, "probe"), f"false positive: {line[:40]}"


def test_the_allowlist_marker_suppresses_a_finding():
    line = f"PERFORM_TOKEN={FAKE_TOKEN}  # {ALLOW_MARKER}"  # pragma: allowlist secret
    assert not scan_text(line, "probe")


def test_the_marker_also_works_on_the_line_above():
    """A long fixture plus an inline marker often exceeds the line-length limit,
    and forcing a choice between two linters is how markers end up omitted."""
    text = f"# {ALLOW_MARKER}\nPERFORM_TOKEN={FAKE_TOKEN}"  # pragma: allowlist secret
    assert not scan_text(text, "probe")


def test_a_conduct_key_is_flagged_even_though_perform_never_uses_one():
    """Perform does not talk to conduct by design, but the three repos get wired
    together by hand — which is exactly when a sibling's key lands in the wrong
    file. A leaked key is leaked regardless of which service minted it."""
    findings = scan_text(f'CONDUCT_TOKEN = "{FAKE_CDT}"', "probe")
    assert findings and "conduct-client-key" in findings[0]


def test_the_finding_names_the_file_and_line():
    text = f"x = 1\ny = 2\nPERFORM_TOKEN={FAKE_TOKEN}"  # pragma: allowlist secret
    assert scan_text(text, "config/settings.py")[0].startswith("config/settings.py:3:")


def test_every_rule_has_a_name_and_an_explanation():
    """The message is the whole interface when this fires inside a git hook."""
    for name, pattern, why in RULES:
        assert name and why and pattern.pattern
        assert why[0].islower(), f"{name}: message should read as a clause, not a sentence"


def test_media_and_cache_paths_are_skipped(tmp_path, monkeypatch):
    """Binary media would be slow to scan and cannot hold a credential in any
    form these regexes would recognize."""
    import scripts.check_secrets as mod

    monkeypatch.setattr(mod, "REPO", tmp_path)
    for name in ("blobs/x.wav", "packs/p/a.mp3", "uv.lock", "coverage.xml"):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("noise")
        assert not _should_scan(path), f"should skip {name}"

    source = tmp_path / "routes" / "speak.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("x = 1")
    assert _should_scan(source)


def test_main_returns_zero_on_a_clean_tree():
    """The repo itself must pass, or the hook blocks every commit — including
    this file, which is why the markers above have to work."""
    assert main([]) == 0


# --- the CLI ------------------------------------------------------------


def test_main_reports_findings_and_exits_nonzero(tmp_path, monkeypatch, capsys):
    """The hook reads the exit code; the human reads the message. Both matter."""
    import scripts.check_secrets as mod

    leaky = tmp_path / "config.py"
    leaky.write_text(f'TOKEN = "{FAKE_CDT}"\n')

    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "_tracked_files", lambda staged: [leaky])

    assert mod.main([]) == 1

    err = capsys.readouterr().err
    assert "config.py:1" in err
    assert "conduct-client-key" in err
    # The remedy, not just the diagnosis.
    assert "rotate it" in err
    assert ALLOW_MARKER in err


def test_main_passes_the_staged_flag_through(tmp_path, monkeypatch):
    import scripts.check_secrets as mod

    seen = []
    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "_tracked_files", lambda staged: seen.append(staged) or [])

    mod.main(["--staged"])
    mod.main([])
    assert seen == [True, False]


def test_unreadable_files_are_skipped_not_fatal(tmp_path, monkeypatch):
    """A binary that slipped past the suffix filter must not crash the hook."""
    import scripts.check_secrets as mod

    binary = tmp_path / "weird.dat"
    binary.write_bytes(b"\xff\xfe\x00\x01\x80")

    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod, "_tracked_files", lambda staged: [binary])
    assert mod.main([]) == 0


def test_tracked_files_shells_out_to_git(tmp_path, monkeypatch):
    import scripts.check_secrets as mod

    calls = []

    class Result:
        stdout = "a.py\nb.py\n"

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return Result()

    monkeypatch.setattr(mod, "REPO", tmp_path)
    monkeypatch.setattr(mod.subprocess, "run", fake_run)

    assert [p.name for p in mod._tracked_files(staged_only=False)] == ["a.py", "b.py"]
    assert "ls-files" in calls[0]

    mod._tracked_files(staged_only=True)
    assert "--cached" in calls[1] and "diff" in calls[1]
