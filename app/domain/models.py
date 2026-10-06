# app/domain/models.py
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ConversionMetrics(BaseModel):
    searches: int
    bookstep_failed: int
    adjusted_bookstep_failed: int
    other_step_failed: int
    effective_searches: int
    no_activity: bool = False
    bookings: int
    booking_volume: float
    avg_booking_value: float
    revenue_consistency: float
    low_confidence: bool = False


class AgentTrustFeatures(BaseModel):
    current_max_delay_days: int = Field(...)
    current_overdue_ratio: float = Field(..., ge=0, le=100)
    current_overdue_count: int = Field(..., ge=0)
    outstanding_amount: float = Field(default=0.0, ge=0)
    historical_late_payment_count: int = Field(default=0, ge=0)
    historical_late_payment_ratio: float = Field(default=0.0, ge=0, le=100)
    average_payment_delay_days: float = Field(default=0.0, ge=0)
    historical_max_payment_delay_days: int = Field(default=0, ge=0)
    no_activity: bool = False

    daily: ConversionMetrics
    weekly: ConversionMetrics
    monthly: ConversionMetrics
    yearly: ConversionMetrics

    # Search-to-Booking (Feature B) — created+reused search intents, 365d
    search_activity: dict = {}


class ReliabilityDetail(BaseModel):
    """
    Per-sub-component small-sample evidence (senior sec6.3).

    raw_rate is the legacy score/100 and is the regression anchor: with
    confidence disabled this object still reports it, and the component score is
    arithmetically identical to the pre-A1 behaviour.

    confidence is an EVIDENCE-COMPLETENESS INDICATOR in [0, 1] -- not a
    probability, not a P-value, and not an interval level. It is exactly
    1 - prior_weight = min(1, n / min_observations), so it can never disagree
    with the shrinkage actually applied. 1.0 means the evidence threshold was
    met, the prior weight is 0, and adjusted_rate == wilson_lower_bound. The
    interval level is carried separately by reliability_wilson_z.
    """

    n: int = Field(..., ge=0)
    successes: int = Field(..., ge=0)
    raw_rate: float | None = None
    wilson_lower_bound: float | None = None
    prior_weight: float = Field(..., ge=0.0, le=1.0)
    adjusted_rate: float | None = None
    confidence: float = Field(..., ge=0.0, le=1.0)
    score: float
    no_evidence: bool = False
    confidence_applied: bool = False


class AgentTrustScores(BaseModel):
    operational_score: int = Field(default=0, ge=0, le=100)

    reliability_score: float | None = Field(default=None)
    financial_score: float = Field(default=0.0)
    experience_score: float = Field(default=0.0)
    composite_trust_score: float = Field(default=0.0)
    # None when the ML programme is disabled or the model is unavailable
    # (NOT_READY). Never a substitute value -- a non-ML score must not occupy
    # the ML share (senior review #6).
    ml_calibration_score: float | None = None
    overall_score: int = Field(..., ge=0, le=100)
    # None when the agent has no search behavior data (component excluded)
    search_to_booking_score: float | None = None
    # Per-sub-component small-sample evidence; None while confidence is disabled
    # is NOT implied -- populated whenever reliability is computable, so the raw
    # rate stays auditable.
    reliability_detail: dict[str, ReliabilityDetail] | None = None


class AgentTrustResult(BaseModel):
    agent_id: int
    agent_name: str
    features: AgentTrustFeatures
    scores: AgentTrustScores
    tier: str
    badges: list[str] = Field(default_factory=list)
    high_risk_flag: bool = Field(default=False)
    high_risk_reasons: list[str] = Field(default_factory=list)
    calculated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
