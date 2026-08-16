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
_access_filtered = False

# Paths whose *successful* access-log lines are dropped. Both are polled: the
# status page meta-refreshes every 2s and a health check is typically on a
# timer, so between them they emit a line every second or two forever and bury
# the events you actually opened the log to see.
#
# This is a log-noise fix and nothing more — see `_QuietPolls` for the two
# things it deliberately does not do.
QUIET_PATHS: tuple[str, ...] = ("/ui", "/health")

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


class _QuietPolls(logging.Filter):
    """Drop uvicorn access lines for successful polls of the status endpoints.

    Two deliberate limits, because a log filter that hides a real failure is
    worse than a noisy log:

    - **Only 2xx and 3xx are dropped.** A `/ui` throwing a 500 or a `/health`
      returning 503 is precisely what you want to see, and those still print.
    - **Only the access line is dropped.** Anything the application itself logs
      while serving these routes — a warning, a traceback — goes through a
      different logger and is untouched.

    uvicorn formats access records as
    `'%s - "%s %s HTTP/%s" %d' % (client, method, path, http_version, status)`,
    so the path is `args[2]` and the status is `args[4]`. That shape is uvicorn's
    private detail rather than a public API, hence the defensive unpacking: if a
    future version changes it, this filter stops matching and every line prints,
    which is the safe direction to fail.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if not isinstance(args, tuple) or len(args) < 5:
            return True
        path, status = args[2], args[4]
        if not isinstance(path, str) or not isinstance(status, int):
            return True
        if status >= 400:
            return True
        # Compare on the path alone; `/ui?x=1` should be just as quiet, and
        # `/uic` should not be.
        bare = path.split("?", 1)[0].rstrip("/") or "/"
        return bare not in QUIET_PATHS


def quiet_access_logs(paths: tuple[str, ...] = QUIET_PATHS) -> None:
    """Install the access-log filter. Idempotent.

    Attached to the `uvicorn.access` logger, which only exists when uvicorn is
    serving — under pytest or the MCP entrypoint this is a no-op that costs one
    dictionary lookup.
    """
    global _access_filtered
    if _access_filtered:
        return
    logging.getLogger("uvicorn.access").addFilter(_QuietPolls())
    _access_filtered = True
    log.info("access log: suppressing successful %s polls", " and ".join(paths))


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
