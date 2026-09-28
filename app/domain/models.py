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


class AgentTrustScores(BaseModel):
    operational_score: int = Field(default=0, ge=0, le=100)

    reliability_score: float | None = Field(default=None)
    financial_score: float = Field(default=0.0)
    experience_score: float = Field(default=0.0)
    composite_trust_score: float = Field(default=0.0)
    ml_calibration_score: float = Field(default=0.0)
    overall_score: int = Field(..., ge=0, le=100)
    # None when the agent has no search behavior data (component excluded)
    search_to_booking_score: float | None = None


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
