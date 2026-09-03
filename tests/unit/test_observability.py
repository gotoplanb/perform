"""Telemetry setup.

Wiring, but wiring with a specific contract: it must never break playback. Every
exporter path is wrapped for that reason, and an untested `except` around a
network call is exactly where that promise quietly stops being true.

Nothing here reaches the network — the exporters are stubbed. What is asserted
is our own behavior: the resource attributes that let a Perform span join a
Wander span, the idempotency, the disable switch, and the never-raise guarantee.
"""

from __future__ import annotations

import logging

import pytest

from observability import logs, tracing


class StubExporter:
    """Minimal exporter. Needs the real surface — a BatchSpanProcessor calls
    `shutdown()` and `force_flush()` on whatever it wraps, including at
    interpreter exit, so a bare object() blows up long after the test."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def export(self, spans):
        from opentelemetry.sdk.trace.export import SpanExportResult

        return SpanExportResult.SUCCESS

    def shutdown(self):
        return None

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return True


@pytest.fixture(autouse=True)
def reset_module_state(monkeypatch):
    """These modules are deliberately idempotent via module globals, so each
    test has to start from a known state.

    `OTEL_SDK_DISABLED` is cleared for the same reason: `init_tracing` honours it
    by returning early, so a test asserting the exporter was built reads whatever
    the ambient environment happens to say. CI sets it to "true" — correctly,
    there is no collector on a runner — and these tests failed there while
    passing on every developer machine. A test that depends on ambient env is
    not a test of the code.
    """
    monkeypatch.delenv("OTEL_SDK_DISABLED", raising=False)
    monkeypatch.setattr(tracing, "_initialized", False)
    monkeypatch.setattr(logs, "_initialized", False)
    monkeypatch.setattr(logs, "_console_initialized", False)
    root = logging.getLogger()
    original = list(root.handlers)
    yield
    root.handlers = original


# --- tracing ------------------------------------------------------------


def test_init_tracing_sets_a_provider(monkeypatch):
    captured = {}

    def make(**kwargs):
        captured.update(kwargs)
        return StubExporter(**kwargs)

    monkeypatch.setattr(tracing, "OTLPSpanExporter", make)
    tracing.init_tracing(
        service_name="perform", otlp_endpoint="http://localhost:4317", role="api"
    )

    assert tracing._initialized is True
    assert captured["endpoint"] == "http://localhost:4317"
    assert captured["insecure"] is True


def test_init_tracing_is_idempotent(monkeypatch):
    """Called from both the server lifespan and the MCP entrypoint; a second
    call must not stack a second exporter."""
    calls = []
    monkeypatch.setattr(
        tracing, "OTLPSpanExporter", lambda **k: (calls.append(k), StubExporter(**k))[1]
    )

    tracing.init_tracing(service_name="perform", otlp_endpoint="x", role="api")
    tracing.init_tracing(service_name="perform", otlp_endpoint="x", role="api")

    assert len(calls) == 1


def test_otel_sdk_disabled_skips_setup_entirely(monkeypatch):
    """An operator who sets OTEL_SDK_DISABLED expects nothing exported, and this
    module builds its provider by hand rather than letting the SDK check."""
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    called = []
    monkeypatch.setattr(
        tracing, "OTLPSpanExporter", lambda **k: (called.append(k), StubExporter(**k))[1]
    )

    tracing.init_tracing(service_name="perform", otlp_endpoint="x")

    assert called == []
    assert tracing._initialized is True  # marked done, so it is not retried


def test_a_failing_exporter_does_not_break_startup(monkeypatch, caplog):
    """The whole point of the wrap: telemetry must never break playback."""

    def boom(**kwargs):
        raise RuntimeError("collector unreachable")

    monkeypatch.setattr(tracing, "OTLPSpanExporter", boom)

    with caplog.at_level(logging.WARNING):
        tracing.init_tracing(service_name="perform", otlp_endpoint="x")

    assert tracing._initialized is True
    assert any("exporter setup failed" in r.message for r in caplog.records)


def test_get_tracer_returns_something_usable():
    tracer = tracing.get_tracer("perform")
    with tracer.start_as_current_span("perform.test") as span:
        span.set_attribute(tracing.BUS, "voice")


def test_shutdown_flushes(monkeypatch):
    """The MCP surface is a short-lived stdio process — without an explicit
    flush its spans die with it."""
    flushed = []

    class StubProvider:
        def shutdown(self):
            flushed.append(True)

    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: StubProvider())
    tracing.shutdown()
    assert flushed == [True]


def test_shutdown_survives_a_failing_provider(monkeypatch, caplog):
    class StubProvider:
        def shutdown(self):
            raise RuntimeError("already closed")

    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: StubProvider())
    with caplog.at_level(logging.WARNING):
        tracing.shutdown()
    assert any("shutdown failed" in r.message for r in caplog.records)


def test_shutdown_on_a_provider_without_the_method(monkeypatch):
    monkeypatch.setattr(tracing.trace, "get_tracer_provider", lambda: object())
    tracing.shutdown()  # must not raise


def test_the_shared_attribute_keys_are_not_renamed():
    """Copied verbatim from conduct and wander. Identical keys across the three
    services are the entire mechanism by which their spans join on the same
    work — renaming them here silently breaks the waterfall."""
    assert tracing.JOB_ID == "job.id"
    assert tracing.MODEL_USED == "model.used"


def test_perform_owned_keys_are_namespaced():
    """New keys are ours to name, and namespacing them keeps them from
    colliding with a future conduct or wander key."""
    for key in (
        tracing.BUS, tracing.ITEM_ID, tracing.ITEM_LABEL, tracing.SINK,
        tracing.TREATMENT, tracing.DURATION_KIND, tracing.ASSET_KEY,
        tracing.MEDIA, tracing.CACHE_HIT, tracing.DUCK_DEPTH_DB, tracing.OUTCOME,
    ):
        assert key.startswith("perform."), key


# --- console logging ----------------------------------------------------


def test_console_logging_adds_a_handler_at_info():
    """Without this Perform's own log lines are invisible: uvicorn configures
    only the `uvicorn*` loggers and everything else falls through to logging's
    last-resort handler, which is fixed at WARNING."""
    root = logging.getLogger()
    root.handlers = []

    logs.init_console_logging()

    assert any(isinstance(h, logging.StreamHandler) for h in root.handlers)
    assert root.level == logging.INFO


def test_console_logging_is_idempotent():
    root = logging.getLogger()
    root.handlers = []

    logs.init_console_logging()
    before = len(root.handlers)
    logs.init_console_logging()

    assert len(root.handlers) == before


def test_console_logging_respects_an_existing_handler():
    """A caller that already configured logging should not get a duplicate,
    which would print every line twice."""
    root = logging.getLogger()
    existing = logging.StreamHandler()
    root.handlers = [existing]

    logs.init_console_logging()

    assert root.handlers == [existing]


def test_console_logging_accepts_a_level():
    root = logging.getLogger()
    root.handlers = []
    logs.init_console_logging(logging.DEBUG)
    assert root.level == logging.DEBUG


# --- OTLP log export ----------------------------------------------------


def test_init_logging_attaches_an_otlp_handler(monkeypatch):
    monkeypatch.setattr(logs, "OTLPLogExporter", lambda **k: StubExporter(**k))
    root = logging.getLogger()
    before = len(root.handlers)

    logs.init_logging(service_name="perform", otlp_endpoint="x", role="api")

    assert logs._initialized is True
    assert len(root.handlers) > before


def test_init_logging_is_idempotent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        logs, "OTLPLogExporter", lambda **k: (calls.append(k), StubExporter(**k))[1]
    )

    logs.init_logging(service_name="perform", otlp_endpoint="x")
    logs.init_logging(service_name="perform", otlp_endpoint="x")

    assert len(calls) == 1


def test_a_failing_log_exporter_leaves_logs_local(monkeypatch, caplog):
    def boom(**kwargs):
        raise RuntimeError("no collector")

    monkeypatch.setattr(logs, "OTLPLogExporter", boom)

    with caplog.at_level(logging.WARNING):
        logs.init_logging(service_name="perform", otlp_endpoint="x")

    assert logs._initialized is True
    assert any("logs stay local" in r.message for r in caplog.records)
