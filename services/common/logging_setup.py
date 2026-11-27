import json
import logging
import os
import sys
from opentelemetry import trace
from datetime import datetime, timezone

SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown")

# Built-in fields of every log record; anything else came from extra={...}
_RESERVED = set(vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()) | {"message"}


class JsonFormatter(logging.Formatter):
    def format(self, record):
        log = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "service": SERVICE_NAME,
            "level": record.levelname,
            "msg": record.getMessage(),
        }
                # Attach the trace_id if this log happens inside a request
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            log["trace_id"] = format(ctx.trace_id, "032x")
            log["span_id"] = format(ctx.span_id, "016x")
        for key, value in vars(record).items():
            if key not in _RESERVED:
                log[key] = value
        if record.exc_info:
            log["error"] = self.formatException(record.exc_info)
        return json.dumps(log, default=str)


def get_logger(name=SERVICE_NAME):
    logger = logging.getLogger(name)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)  # Docker collects stdout
        handler.setFormatter(JsonFormatter())
        logger.addHandler(handler)
        logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
        logger.propagate = False
    return logger

