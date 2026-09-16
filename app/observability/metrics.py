from __future__ import annotations

from collections.abc import Awaitable, Callable
from time import perf_counter

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware

# ── Generic HTTP metrics ────────────────────────────────────────────────────

REQUEST_COUNT = Counter(
    "agent_trust_http_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"],
)

REQUEST_LATENCY = Histogram(
    "agent_trust_http_request_duration_seconds",
    "HTTP request latency in seconds",
    ["method", "path"],
)

# ── Agent Trust-specific metrics ────────────────────────────────────────────

AGENT_TRUST_REQUESTS = Counter(
    "agent_trust_requests_total",
    "Total agent trust score requests",
    ["status"],
)

AGENT_TRUST_DURATION = Histogram(
    "agent_trust_scoring_duration_ms",
    "Agent trust score calculation duration in milliseconds",
)


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        method = request.method
        path = request.url.path
        start = perf_counter()

        response = await call_next(request)

        elapsed = perf_counter() - start

        REQUEST_LATENCY.labels(method=method, path=path).observe(elapsed)
        REQUEST_COUNT.labels(
            method=method,
            path=path,
            status=str(response.status_code),
        ).inc()

        return response


def metrics_response() -> Response:
    return Response(
        generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )
