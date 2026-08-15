"""OpenTelemetry tracer setup. Pushes OTLP gRPC to Watchtower's Alloy.

Copied in shape from conduct's `observability/tracing.py` and wander's
`harness/telemetry.py`, which is deliberate — see the attribute-key block below.

## Trace continuity across the three services

Wander submits a generation job to Conduct, waits, and then hands Perform a
finished file. Those are three processes and, at the moment the file is handed
over, one trace: Wander's client instrumentation injects `traceparent` on the
POST, so a Perform span parents onto the Wander span that placed the item, which
in turn parents onto whatever Conduct span produced the audio.

That only works if the resource attributes and the shared attribute keys match
across all three. They are not "ours to tidy" — see ATTRIBUTE KEYS.
"""

from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger(__name__)

_initialized = False


# --- ATTRIBUTE KEYS ---------------------------------------------------------
#
# The first block is shared vocabulary. `job.id` and `model.used` are copied
# verbatim from conduct's worker/executor.py and wander's harness/telemetry.py.
# Do NOT "clean these up" into perform-flavoured names — identical keys across
# all three services are the entire mechanism by which a perform span, a wander
# span, and a conduct worker span can be joined on the same piece of work.
JOB_ID = "job.id"
MODEL_USED = "model.used"

# perform-owned keys — no conduct or wander counterpart, so they're free to be
# ours. Namespaced `perform.*` for the same reason wander namespaces `bench.*`.
BUS = "perform.bus"
ITEM_ID = "perform.item_id"
ITEM_LABEL = "perform.label"  # opaque; we log it, we never interpret it
SINK = "perform.sink"
TREATMENT = "perform.treatment"
DURATION_KIND = "perform.duration_kind"
ASSET_KEY = "perform.asset_key"
MEDIA = "perform.media"
CACHE_HIT = "perform.cache_hit"
DUCK_DEPTH_DB = "perform.duck_db"
OUTCOME = "perform.outcome"  # played | stopped | replaced | dropped


def init_tracing(*, service_name: str, otlp_endpoint: str, role: str = "api") -> None:
    """Idempotent tracer setup. Call once per process at startup.

    `role` distinguishes Perform's processes in Tempo the same way conduct
    splits `api` from `worker`. Perform is single-process today, so this is
    `api` for the server and `mcp` for the stdio MCP surface.
    """
    global _initialized
    if _initialized:
        return

    # Wander checks this by hand rather than relying on the SDK, because it
    # builds its provider explicitly. Same reasoning here: an operator who sets
    # OTEL_SDK_DISABLED expects nothing to be exported, including from a
    # hand-built provider.
    if os.environ.get("OTEL_SDK_DISABLED", "").lower() == "true":
        log.info("OTEL_SDK_DISABLED=true — skipping tracer setup")
        _initialized = True
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            "service.namespace": "perform",
            "service.instance.role": role,
        }
    )
    provider = TracerProvider(resource=resource)
    try:
        exporter = OTLPSpanExporter(endpoint=otlp_endpoint, insecure=True)
        provider.add_span_processor(BatchSpanProcessor(exporter))
    except Exception as e:  # noqa: BLE001 - never let telemetry break playback
        log.warning("OTLP exporter setup failed (%s) — traces will not export", e)

    trace.set_tracer_provider(provider)
    _initialized = True
    log.info(
        "OTel tracing initialized: service=%s role=%s endpoint=%s pid=%s",
        service_name,
        role,
        otlp_endpoint,
        os.getpid(),
    )


def get_tracer(name: str = "perform"):
    return trace.get_tracer(name)


def shutdown() -> None:
    """Flush pending spans. The MCP surface is a short-lived stdio process, so
    without an explicit flush its spans die with it."""
    provider = trace.get_tracer_provider()
    if hasattr(provider, "shutdown"):
        try:
            provider.shutdown()
        except Exception as e:  # noqa: BLE001 - never let telemetry break shutdown
            log.warning("tracer shutdown failed: %s", e)
