.PHONY: help install install-hooks run mcp test lint format coverage secrets \
        sonar-scan sonar-gate check reload-buses voices tones clean

help:
	@echo "Common targets:"
	@echo "  make install       — sync deps via uv"
	@echo "  make run           — run the playback server on :8770 with reload"
	@echo "  make mcp           — run the stdio MCP server (the Claude Code notification surface)"
	@echo "  make test          — run pytest (never opens a device, never makes noise)"
	@echo "  make lint          — ruff check"
	@echo "  make format        — ruff format"
	@echo "  make coverage      — pytest with coverage, writes coverage.xml"
	@echo "  make secrets       — scan the tree for committed credentials"
	@echo "  make check         — the whole local gate: lint + secrets + coverage"
	@echo "  make install-hooks — install the pre-commit / pre-push gates"
	@echo "  make sonar-scan    — scan + quality gate against local Watchtower"
	@echo "  make voices        — list the macOS system voices \`say\` can use"
	@echo "  make reload-buses  — SIGUSR1 the server to reload config/buses.yaml without a restart"
	@echo "  make clean         — drop the warm cache (blobs + index). Rebuildable."

install:
	uv sync

run:
	uv run uvicorn main:app --reload --host 127.0.0.1 --port 8770

mcp:
	uv run perform-mcp

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format .

# Cobertura XML for SonarQube to ingest, plus a terminal summary. The
# `fail_under` floor in pyproject.toml is enforced here, so this target is the
# coverage gate as well as the report.
coverage:
	uv run pytest --cov --cov-report=xml --cov-report=term -q

# Scan every tracked + untracked file for credentials. This repo is public, so a
# leaked token is a real incident and rotating it is the only remedy — removing
# the line is not enough once it is in history.
secrets:
	uv run python scripts/check_secrets.py

# Everything the pre-push hook runs except Sonar, which needs Watchtower up.
# Handy before opening a pull request, or when a hook was bypassed.
check: lint secrets coverage
	@echo "local gate passed"

# Symlink rather than copy, so editing scripts/hooks/* takes effect immediately
# and the hooks stay in version control where they can be reviewed.
install-hooks:
	@mkdir -p .git/hooks
	@for hook in pre-commit pre-push; do \
		ln -sf ../../scripts/hooks/$$hook .git/hooks/$$hook; \
		echo "installed .git/hooks/$$hook -> scripts/hooks/$$hook"; \
	done
	@echo "Bypass a gate once with --no-verify."

# Scan against the local Watchtower SonarQube, then wait for the verdict.
# `sonar-gate` is a separate step because the gate is computed asynchronously
# after upload: reading it immediately returns the *previous* run's result,
# which is the kind of green-when-actually-red that makes a gate worse than
# none at all.
sonar-scan: coverage
	@if [ -z "$$SONAR_TOKEN" ] && ! grep -q '^SONAR_TOKEN=' .env 2>/dev/null; then \
		echo "Set SONAR_TOKEN in .env (generate one at http://localhost:9000)"; exit 1; \
	fi
	@set -a; [ -f .env ] && . ./.env; set +a; \
	docker run --rm \
		-e SONAR_HOST_URL=http://host.docker.internal:9000 \
		-e SONAR_TOKEN=$$SONAR_TOKEN \
		-v "$$(pwd):/usr/src" \
		sonarsource/sonar-scanner-cli:latest
	@$(MAKE) --no-print-directory sonar-gate

sonar-gate:
	@uv run python scripts/sonar_gate.py

# The `say` voices installed on this machine. PERFORM_SAY_VOICE must name one
# of these exactly; an unknown name fails the boot rather than falling back to
# the default, because a silent fallback means two participants sound identical
# and the listener has no idea why (SPEC §8).
voices:
	@say -v '?'

# Reload config/buses.yaml — bus definitions, sink routing, the duck matrix —
# without dropping what is currently playing. SIGUSR1 and not SIGHUP because
# uvicorn steals SIGHUP; conduct's pricing reload made the same choice.
#
# `pgrep -n` (newest match) rather than `pkill -f`: `make run` is `uv run
# uvicorn ...`, so the pattern matches both the uv wrapper and the python child
# it spawned. uv forwards the signal on, so pkill delivers it twice and the
# reload logs twice. Harmless — a reload is idempotent — but the doubled log
# line reads like a bug. The newest match is always the child.
reload-buses:
	@pgrep -n -f 'uvicorn main:app' | xargs kill -USR1 && \
		echo "SIGUSR1 sent — check the log for 'buses reloaded'"

# The warm cache is content-addressed and rebuildable from packs and from
# whatever Wander hands over. Deleting it costs you the next warm, nothing more.
clean:
	rm -rf blobs perform-library.sqlite
	@echo "Warm cache dropped."
