#!/usr/bin/env python3
"""Secret scanner for the pre-commit / pre-push hooks.

Adapted from crossover's `scripts/check_secrets.py` — same structure, same
allowlist convention, same bias. The rule set is different because the
credentials differ: Perform handles one bearer token of its own and otherwise
touches nothing, so most of the value here is catching a *sibling* service's
key pasted in while wiring the three together.

This repo is public, so a leaked token is a real incident and rotating it is the
only remedy — removing the line is not enough once it is in history. No
third-party scanner is assumed to be installed; this is a small, dependency-free
check deliberately biased toward false positives (which cost one allowlist
comment) over false negatives.

Usage:
    check_secrets.py            # scan every tracked + untracked file
    check_secrets.py --staged   # scan only what is staged, for the commit hook
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

#: `# pragma: allowlist secret` marks a deliberate placeholder or a test
#: fixture. The detect-secrets convention rather than a `noqa:`-style marker,
#: which ruff would try to parse as one of its own codes.
#:
#: Honored on the flagged line *or* the line above it — a long fixture plus an
#: inline marker often exceeds the line-length limit, and forcing a choice
#: between two linters is how markers end up omitted.
ALLOW_MARKER = "pragma: allowlist secret"

#: Paths whose whole purpose is to talk about credentials without holding one,
#: plus generated and vendored trees.
SKIPPED_PATHS = (
    ".venv/",
    ".git/",
    "__pycache__/",
    ".pytest_cache/",
    ".ruff_cache/",
    ".scannerwork/",
    "htmlcov/",
    "coverage.xml",
    ".coverage",
    "uv.lock",
    # The warm cache and user-supplied packs are media, not source. blobs/ is
    # gitignored so it should never reach the scanner anyway, but a stray
    # `git add -f` should not then dump a WAV through every regex.
    "blobs/",
    "packs/",
)

SKIPPED_SUFFIXES = (
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".pdf", ".ico", ".woff2",
    # Perform's own media types, for the same reason.
    ".wav", ".mp3", ".aiff", ".ogg", ".flac", ".mp4", ".webm", ".mov", ".sqlite",
)

#: Each rule is (name, pattern, why it matters here).
RULES: list[tuple[str, re.Pattern[str], str]] = [
    (
        "perform-token-assignment",
        # The only credential Perform mints. Unset by default (loopback needs no
        # auth), so a literal here means someone hardcoded the tunnel secret.
        re.compile(r"PERFORM_TOKEN\s*[:=]\s*[\"']?([^\s\"'#]{8,})"),
        "a PERFORM_TOKEN literal — keep it in .env",
    ),
    (
        "conduct-client-key",
        # cdt_<random> is conduct's client API key format. Perform does not talk
        # to conduct by design, but these three repos sit side by side and get
        # wired together by hand, which is exactly when one gets pasted into the
        # wrong file. A sibling's leaked key is still a leaked key.
        re.compile(r"\bcdt_[A-Za-z0-9_-]{16,}"),
        "a Conduct client API key (cdt_…) — it belongs in conduct's .env, not here",
    ),
    (
        "sonar-token",
        re.compile(r"\bsq[pua]_[0-9a-f]{40}\b"),
        "a SonarQube token",
    ),
    (
        "dsn-with-password",
        re.compile(
            r"(?:postgres(?:ql)?|mysql|redis|mongodb)(?:\+\w+)?://[^\s:/@\"']+:[^\s:/@\"']+@"
        ),
        "a connection string with an embedded password",
    ),
    (
        "private-key-block",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----"),
        "a private key block",
    ),
    (
        "aws-access-key",
        re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
        "an AWS access key id",
    ),
    (
        "github-token",
        re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
        "a GitHub token",
    ),
    (
        "generic-bearer",
        re.compile(r"Authorization[\"']?\s*[:=]\s*[\"']Bearer\s+[A-Za-z0-9._~+/-]{20,}"),
        "a hardcoded bearer token",
    ),
]


def _tracked_files(staged_only: bool) -> list[Path]:
    if staged_only:
        cmd = ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"]
    else:
        cmd = ["git", "ls-files", "--cached", "--others", "--exclude-standard"]
    out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, check=False)
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


def _should_scan(path: Path) -> bool:
    rel = path.relative_to(REPO).as_posix() if path.is_absolute() else path.as_posix()
    if any(rel.startswith(s) or f"/{s}" in f"/{rel}" for s in SKIPPED_PATHS):
        return False
    if path.suffix.lower() in SKIPPED_SUFFIXES:
        return False
    return path.is_file()


#: Values that are obviously not real credentials.
_PLACEHOLDERS = re.compile(
    r"^(?:$|x+$|y+$|changeme$|change-me$|your[-_]|<|\$\{|placeholder|example|redacted"
    r"|test[-_]?key|fake|dummy|pytest|local-dev|secret$|token$)",
    re.IGNORECASE,
)


def _is_placeholder(value: str) -> bool:
    return bool(_PLACEHOLDERS.match(value.strip().strip("\"'")))


def scan_text(text: str, rel: str) -> list[str]:
    findings: list[str] = []
    lines = text.splitlines()
    for number, line in enumerate(lines, start=1):
        previous = lines[number - 2] if number >= 2 else ""
        if ALLOW_MARKER in line or ALLOW_MARKER in previous:
            continue
        for name, pattern, why in RULES:
            match = pattern.search(line)
            if not match:
                continue
            # An empty assignment (`PERFORM_TOKEN=`) is a template, not a leak.
            captured = next((g for g in match.groups() if g), match.group(0))
            if _is_placeholder(captured):
                continue
            findings.append(f"{rel}:{number}: [{name}] {why}")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="scan staged changes only")
    args = parser.parse_args(argv)

    findings: list[str] = []
    for path in _tracked_files(args.staged):
        if not _should_scan(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        findings += scan_text(text, path.relative_to(REPO).as_posix())

    if findings:
        print("Possible secrets found:\n", file=sys.stderr)
        for finding in findings:
            print(f"  {finding}", file=sys.stderr)
        print(
            "\nIf one is a deliberate placeholder or fixture, append "
            f"`# {ALLOW_MARKER}` to that line.\n"
            "If a real credential has already been committed, rotate it — removing "
            "the line is not enough once it is in history.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
