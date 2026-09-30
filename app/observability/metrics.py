# app/observability/metrics.py
"""Sprint 5/Phase 3 - Prometheus instrumentation for the Agent Trust service.

Optimized for the senior primitives: service tone (requests/duration) plus
ML Target Programme counters. The ML counters are OBSERVABLE regardless of
whether the ML gate is enabled: NOT_READY/fallback/rejection/promotion are
recorded so operators can trace the readiness lifecycle even while DISABLED.

Phase 3 (B8) adds HTTP request telemetry, cache/dependency/webhook/rate-limit
signals, and a per-request access log. All HTTP/cache counters use route
TEMPLATES (e.g. /v1/trust-score/{agent_id}) as labels, never raw URLs.

The scoring-latency histogram is observed in SECONDS: the default Prometheus
buckets are second-based (0.005..10). The metric name keeps the legacy `_ms`
suffix so existing dashboards keep resolving; only the recorded unit changed.
"""

from __future__ import annotations

import logging
from time import perf_counter
from typing import Any

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
)
from starlette.middleware.base import BaseHTTPMiddleware

from app.observability.request_context import request_id_context

logger = logging.getLogger(__name__)
access_logger = logging.getLogger("agent_trust.access")

# --- Service tone ---

AGENT_TRUST_REQUESTS: Counter = Counter(
    "agent_trust_requests_total",
    "Agent trust scoring requests",
    ["status"],
)
AGENT_TRUST_LATENCY: Histogram = Histogram(
    "agent_trust_scoring_duration_ms",
    "Agent trust scoring latency in milliseconds",
)
AGENT_TRUST_DURATION: Histogram = AGENT_TRUST_LATENCY

# --- HTTP request telemetry (B8) ---
# Labels use route templates (request.scope["route"].path); requests that never
# match a route (e.g. 404s) are bucketed under the literal "unmatched" label.

AGENT_TRUST_HTTP_REQUESTS: Counter = Counter(
    "agent_trust_http_requests_total",
    "HTTP requests by status, method and route template",
    ["status", "method", "route"],
)
AGENT_TRUST_HTTP_DURATION: Histogram = Histogram(
    "agent_trust_http_duration_seconds",
    "HTTP request duration in seconds, by route template",
)

# --- Cache tier signals (B8) ---
# The stale-hit counter is the most important operational signal: it records
# every time the service served a 24h fallback because the DB was unavailable
# or its schema had drifted.

CACHE_HITS: Counter = Counter(
    "agent_trust_cache_hits_total",
    "Primary cache reads that returned a value",
)
CACHE_MISSES: Counter = Counter(
    "agent_trust_cache_misses_total",
    "Primary cache reads that returned nothing",
)
CACHE_STALE_HITS: Counter = Counter(
    "agent_trust_cache_stale_hits_total",
    "Stale 24h fallback served (DB unavailable or schema changed)",
)
CACHE_INVALIDATIONS: Counter = Counter(
    "agent_trust_cache_invalidations_total",
    "Cache invalidations (webhook-triggered)",
)
CACHE_ERRORS: Counter = Counter(
    "agent_trust_cache_errors_total",
    "Cache operations that failed (Redis errors)",
)

# --- Dependency / integration signals (B8) ---

DATABASE_FAILURES: Counter = Counter(
    "agent_trust_database_failures_total",
    "Trust-score requests that hit a broken or drifted external DB",
)
SCHEMA_DRIFT: Counter = Counter(
    "agent_trust_schema_drift_total",
    "Schema guard detections of a drifted external DB schema",
)
WEBHOOKS: Counter = Counter(
    "agent_trust_webhooks_total",
    "Domain-event webhook attempts by outcome",
    ["outcome"],
)
RATE_LIMIT_REJECTIONS: Counter = Counter(
    "agent_trust_ratelimit_rejections_total",
    "Requests rejected by the rate limiter (HTTP 429)",
)

# --- Component evidence (senior sec5: a component with no evidence is excluded
# from the composite, not scored as 0) ---

COMPONENT_UNAVAILABLE: Counter = Counter(
    "agent_trust_component_unavailable_total",
    "Component excluded from the composite for lack of evidence",
    ["component"],
)

# --- ML Target Programme (Sprint 5, senior sec15-16) ---
# Counters are defined ALWAYS but only increment while the ML gate is enabled,
# so a disabled system still shows NOT_READY/first-predict transitions clearly.

ML_PREDICTIONS: Counter = Counter(
    "agent_trust_ml_predictions_total",
    "ML calibration predictions attempted (gate-ON only)",
)
ML_NOT_READY: Counter = Counter(
    "agent_trust_ml_not_ready_total",
    "ML programme DISABLED or readiness-gated OFF on a request",
)
ML_FALLBACKS: Counter = Counter(
    "agent_trust_ml_fallbacks_total",
    "ML programme ready but a target fell back to rules due to errors",
)
MODEL_REJECTION: Counter = Counter(
    "agent_trust_model_rejections_total",
    "Challenger rejected on evaluation (champion kept)",
)
MODEL_PROMOTION: Counter = Counter(
    "agent_trust_model_promotions_total",
    "Challenger promoted to PRODUCTION on evaluation",
)

# --- Training lifecycle (Sprint 6, senior sec19-24) ---
# Truthful accounting: TRAINING_RUNS counts runs that actually started (triggered
# AND ready), so runs == success + failure. A tick that decides "nothing to do"
# is NOT counted as a success - it is a skip and increments nothing.
TRAINING_RUNS: Counter = Counter(
    "agent_trust_training_runs_total",
    "Training runs that actually started (triggered and ready)",
)
TRAINING_SUCCESS: Counter = Counter(
    "agent_trust_training_success_total",
    "Training runs that completed training and candidate evaluation",
)
TRAINING_FAILURE: Counter = Counter(
    "agent_trust_training_failures_total",
    "Training runs that ended in an error",
)
MODEL_ROLLBACK: Counter = Counter(
    "agent_trust_model_rollbacks_total",
    "Champion rolled back to the previous known-good production version",
)


def _route_label(request: Request) -> str:
    """Route TEMPLATE for labeling, never the raw URL/query string."""
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if path else "unmatched"


def _emit_access_log(request: Request, status_code: str, duration_ms: float) -> None:
    access_logger.info(
        "http request",
        extra={
            "request_id": request_id_context.get(),
            "method": request.method,
            "path": _route_label(request),
            "status": status_code,
            "duration_ms": round(duration_ms, 3),
        },
    )


def _record_http_request(request: Request, status_code: str, duration_ms: float) -> None:
    AGENT_TRUST_HTTP_REQUESTS.labels(
        status=status_code,
        method=request.method,
        route=_route_label(request),
    ).inc()
    # Observed in seconds: default Prometheus histogram buckets are second-based.
    AGENT_TRUST_HTTP_DURATION.observe(duration_ms / 1000.0)
    # Legacy service-tone counter, kept for backward compatibility.
    AGENT_TRUST_REQUESTS.labels(status=status_code).inc()
    _emit_access_log(request, status_code, duration_ms)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Per-request telemetry + JSON access log.

    In production the outer ServerErrorMiddleware turns handler exceptions into
    500 responses, so dispatch usually sees a response. The except branch guards
    the rare raised-from-below case so 5xx traffic is still observed.
    """

    async def dispatch(self, request: Request, call_next: Any) -> Response:
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            _record_http_request(
                request, "500", (perf_counter() - started) * 1000.0
            )
            raise
        _record_http_request(
            request, str(response.status_code), (perf_counter() - started) * 1000.0
        )
        return response


def metrics_response() -> Response:
    from fastapi.responses import Response as FastResponse
    from prometheus_client import generate_latest as _generate_latest

    return FastResponse(content=_generate_latest(), media_type=CONTENT_TYPE_LATEST)