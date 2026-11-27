# Span: one step in a request, such as "INSERT into orders, 4 ms."
# Trace: all the spans of one request, across all services.
# Counter: a number that only goes up, like total requests.
# Histogram: a distribution of values, used for latency percentiles like "95% of requests are under 80 ms."
# Labels: dimensions you can filter metrics by, e.g.status="500".
"""
Traces + metrics for every web service.
TRACES  -> Jaeger:     one checkout = one trace, showing every hop and its duration.
METRICS -> Prometheus: numbers over time. Prometheus downloads GET /metrics every 5 s.
"""
import os
import time

from fastapi import FastAPI, Request, Response
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest

SERVICE_NAME = os.getenv("SERVICE_NAME", "unknown")

# ---- Metrics every service exposes ----
HTTP_REQUESTS = Counter(
    "http_requests_total", "Incoming HTTP requests", ["method", "route", "status"]
)
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds", "Incoming HTTP request latency", ["method", "route"],
    buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10),
)
# Our code increments this whenever a call to postgres/redis/another service fails.
DEPENDENCY_ERRORS = Counter(
    "dependency_errors_total", "Failed calls to a dependency", ["dependency", "error_type"]
)

_NOT_MEASURED = {"/metrics", "/health"}


def setup_tracing():
    provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
    # The destination comes from the env var OTEL_EXPORTER_OTLP_ENDPOINT.
    # BatchSpanProcessor sends spans in the background, in batches.
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)


def setup_observability(app: FastAPI):
    setup_tracing()
    # Creates a span for every incoming request automatically.
    FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")

    # Middleware = code that runs around every request.
    @app.middleware("http")
    async def record_metrics(request: Request, call_next):
        path = request.url.path
        if path in _NOT_MEASURED:
            return await call_next(request)
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            HTTP_REQUESTS.labels(request.method, path, str(status)).inc()
            HTTP_LATENCY.labels(request.method, path).observe(time.perf_counter() - start)

    @app.get("/metrics", include_in_schema=False)
    async def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)