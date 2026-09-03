"""Guard the unit/integration split.

The `checks` CI job runs `pytest tests/unit` with no system packages installed,
deliberately — a lint slip or a committed secret should fail in seconds rather
than wait behind an apt install. That only holds if unit tests genuinely need
nothing but Python.

It stopped holding once, silently on every developer machine (ffmpeg is always
there) and loudly in CI. This is the tripwire so it fails at the desk instead.
"""

from __future__ import annotations

from pathlib import Path

UNIT = Path(__file__).resolve().parent

#: Binaries a unit test must not reach for. Each is installed in the
#: integration job and absent from the fast one.
FORBIDDEN = ("ffmpeg", "ffprobe", "requires_ffmpeg", "say")


def _unit_test_sources() -> list[Path]:
    return sorted(p for p in UNIT.glob("test_*.py") if p.name != Path(__file__).name)


def test_no_unit_test_depends_on_a_system_binary():
    offenders: list[str] = []
    for path in _unit_test_sources():
        text = path.read_text()
        for token in FORBIDDEN:
            # A mocked reference is fine — the point is not *invoking* the
            # binary. Invocation shows up as a subprocess call or the marker.
            if f'"{token}"' in text and "subprocess.run" in text and "monkeypatch" not in text:
                offenders.append(f"{path.name}: invokes {token}")
            if f"import {token}" in text or "import requires_ffmpeg" in text:
                offenders.append(f"{path.name}: imports {token}")

    assert not offenders, (
        "these belong in tests/integration/ — the `checks` CI job installs no "
        f"system packages: {offenders}"
    )


def test_the_shared_ffmpeg_marker_is_only_used_by_integration_tests():
    """`requires_ffmpeg` fails rather than skips under CI, so a unit test
    carrying it turns the fast job red the moment it runs on a clean runner."""
    users = [p.name for p in _unit_test_sources() if "requires_ffmpeg" in p.read_text()]
    assert not users, f"move these to tests/integration/: {users}"
