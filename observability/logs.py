"""Ship stdlib log records to Alloy → Loki, stamped with the active span.

Same shape as conduct's `observability/logs.py`. The payoff is that a log line
in Loki links straight to its trace in Tempo, which is how you answer "why did
this play at the wrong moment" without correlating timestamps by eye.

Perform logs are plain stdlib `logging` with %-style lazy args, not JSON.
Structure comes from span attributes (see `tracing.py`), same as the other two
services.
"""

from __future__ import annotations

import logging

from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, Resource

log = logging.getLogger(__name__)

_initialized = False
_console_initialized = False

# Same format wander's bench runner uses, so a line from any of the three
# services reads the same way in a terminal.
CONSOLE_FORMAT = "%(asctime)s %(levelname)s %(name)s — %(message)s"


def init_console_logging(level: int = logging.INFO) -> None:
    """Give app loggers a handler, at INFO.

    Without this Perform's own log lines are invisible. uvicorn configures only
    the `uvicorn*` loggers, so everything else falls through to logging's
    last-resort handler, which is fixed at WARNING — which means the entire
    startup sequence (device opened at this rate, these buses loaded, these
    packs discovered, this voice validated) silently does not print.

    That is unacceptable here specifically. SPEC §8 asks Perform to *"validate at
    startup and fail loudly"*, and half of failing loudly is having said what you
    were doing when it worked. Conduct and wander both work around the same
    behaviour by logging notable startup facts at `warning`; Perform keeps that
    idiom for the genuinely notable ones and fixes the underlying cause too, so
    `--log-level` means what it says.
    """
    global _console_initialized
    if _console_initialized:
        return
    root = logging.getLogger()
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter(CONSOLE_FORMAT))
        root.addHandler(handler)
    root.setLevel(level)
    _console_initialized = True


def init_logging(*, service_name: str, otlp_endpoint: str, role: str = "api") -> None:
    """Idempotent. Attaches an OTLP handler to the root logger."""
    global _initialized
    if _initialized:
        return

    resource = Resource.create(
        {
            SERVICE_NAME: service_name,
            "service.namespace": "perform",
            "service.instance.role": role,
        }
    )
    try:
        provider = LoggerProvider(resource=resource)
        provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=otlp_endpoint, insecure=True))
        )
        set_logger_provider(provider)
        logging.getLogger().addHandler(LoggingHandler(logger_provider=provider))
    except Exception as e:  # noqa: BLE001 - never let telemetry break playback
        log.warning("OTLP log exporter setup failed (%s) — logs stay local", e)

    _initialized = True
