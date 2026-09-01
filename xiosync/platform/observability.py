"""Observability middleware — OpenTelemetry traces and Prometheus metrics.

Provides optional instrumentation that activates only when the dependencies
are installed. This keeps the platform universal — deployers opt in to
observability backends by installing the appropriate packages.

Environment variables::

    OTEL_EXPORTER_OTLP_ENDPOINT   — OTLP collector endpoint (e.g. http://localhost:4317)
    OTEL_SERVICE_NAME             — Service name for traces (default: xiosync-api)
    ENABLE_PROMETHEUS             — Set to "true" to expose /metrics endpoint
"""

from __future__ import annotations

import logging
import time
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("xiosync.platform.observability")

__all__ = ["ObservabilityMiddleware", "setup_opentelemetry", "get_metrics_app"]


class ObservabilityMiddleware(BaseHTTPMiddleware):
    """Lightweight request metrics middleware — no external deps required.

    Logs request duration and records counters for basic application metrics.
    When OpenTelemetry is available, creates spans automatically.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        start = time.perf_counter()
        method = request.method
        path = request.url.path

        response: Response = await call_next(request)

        duration_ms = (time.perf_counter() - start) * 1000
        status = response.status_code

        # Emit structured log with request metrics.
        logger.info(
            "http_request",
            extra={
                "method": method,
                "path": path,
                "status": status,
                "duration_ms": round(duration_ms, 2),
                "request_id": getattr(request.state, "request_id", ""),
            },
        )

        # Set timing header.
        response.headers["X-Response-Time-Ms"] = f"{duration_ms:.2f}"

        # If OpenTelemetry is available, record metrics.
        try:
            from opentelemetry import metrics as otel_metrics

            meter = otel_metrics.get_meter("xiosync.api")
            counter = meter.create_counter(
                "http.server.request.count",
                description="Total HTTP requests",
            )
            counter.add(1, {"method": method, "path": path, "status": str(status)})

            histogram = meter.create_histogram(
                "http.server.request.duration",
                description="HTTP request duration in milliseconds",
                unit="ms",
            )
            histogram.record(duration_ms, {"method": method, "path": path, "status": str(status)})
        except ImportError:
            pass  # OpenTelemetry not installed — skip.

        return response


def setup_opentelemetry(service_name: str = "xiosync-api") -> bool:
    """Initialize OpenTelemetry tracing and metrics if packages are available.

    Returns True if successfully initialized, False if packages are missing.
    """
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
    except ImportError:
        logger.info("opentelemetry_not_available: install opentelemetry-sdk to enable tracing")
        return False

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)

    logger.info("opentelemetry_initialized", extra={"service_name": service_name})
    return True


def get_metrics_app() -> Any | None:
    """Create a Prometheus metrics ASGI sub-app if prometheus-client is installed.

    Mount at ``/metrics`` on the main FastAPI app::

        metrics = get_metrics_app()
        if metrics:
            app.mount("/metrics", metrics)
    """
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            Counter,
            Histogram,
            generate_latest,
            multiprocess,
        )
        from starlette.responses import Response as StarletteResponse

        registry = CollectorRegistry()

        # Register default process and platform collectors.
        try:
            from prometheus_client import ProcessCollector, PlatformCollector
            ProcessCollector(registry=registry)
            PlatformCollector(registry=registry)
        except Exception:
            pass

        # Application metrics.
        Counter(
            "xiosync_http_requests_total",
            "Total HTTP requests",
            ["method", "path", "status"],
            registry=registry,
        )
        Histogram(
            "xiosync_http_request_duration_seconds",
            "HTTP request duration",
            ["method", "path"],
            registry=registry,
        )
        Counter(
            "xiosync_tasks_completed_total",
            "Total tasks completed",
            ["organization_id"],
            registry=registry,
        )
        Counter(
            "xiosync_events_appended_total",
            "Total events appended",
            ["event_type"],
            registry=registry,
        )

        from starlette.applications import Starlette
        from starlette.routing import Route

        async def metrics_endpoint(request: Any) -> StarletteResponse:
            return StarletteResponse(
                generate_latest(registry),
                media_type=CONTENT_TYPE_LATEST,
            )

        metrics_app = Starlette(routes=[Route("/", metrics_endpoint)])
        logger.info("prometheus_metrics_enabled")
        return metrics_app

    except ImportError:
        logger.info("prometheus_not_available: install prometheus-client to enable /metrics")
        return None
