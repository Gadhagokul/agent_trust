# app/observability/metrics.py
"""Sprint 5 - Prometheus instrumentation for the Agent Trust service.

Optimized for the senior primitives: service tone (requests/duration) plus
ML Target Programme counters. The ML counters are OBSERVABLE regardless of
whether the ML gate is enabled: NOT_READY/fallback/rejection/promotion are
recorded so operators can trace the readiness lifecycle even while DISABLED.
"""

from __future__ import annotations

from typing import Any

from fastapi import Request, Response
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
)
from starlette.middleware.base import BaseHTTPMiddleware

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


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Any
    ) -> Response:
        response = await call_next(request)
        AGENT_TRUST_REQUESTS.labels(status=str(response.status_code)).inc()
        return response


def metrics_response() -> Response:
    from fastapi.responses import Response as FastResponse
    from prometheus_client import generate_latest as _generate_latest

    return FastResponse(content=_generate_latest(), media_type=CONTENT_TYPE_LATEST)