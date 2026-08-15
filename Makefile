.PHONY: help install run mcp test lint format coverage reload-buses voices tones clean

help:
	@echo "Common targets:"
	@echo "  make install       — sync deps via uv"
	@echo "  make run           — run the playback server on :8770 with reload"
	@echo "  make mcp           — run the stdio MCP server (the Claude Code notification surface)"
	@echo "  make test          — run pytest (never opens a device, never makes noise)"
	@echo "  make lint          — ruff check"
	@echo "  make format        — ruff format"
	@echo "  make coverage      — pytest with coverage, writes coverage.xml"
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

# Cobertura XML for whatever wants to ingest it, plus a terminal summary.
coverage:
	uv run pytest --cov --cov-report=xml --cov-report=term -q

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
