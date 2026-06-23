# app/domain/models.py
from datetime import datetime, timezone

from pydantic import BaseModel, Field


class ConversionMetrics(BaseModel):
    # Search breakdown
    searches: int                    # Raw total from search_sessions
    bookstep_failed: int             # BookStep+FAILED (system/payment failures)
    adjusted_bookstep_failed: int    # Capped at 70% of searches (abuse prevention)
    other_step_failed: int           # Non-BookStep failures (agent-caused signals)
    effective_searches: int          # searches - adjusted_bookstep_failed (floor: 1)
    no_activity: bool = False        # True when effective_searches == 0
    # Booking outcome
    bookings: int
    booking_volume: float
    avg_booking_value: float         # Revenue normalization: per-booking average
    revenue_consistency: float       # Stddev of booking amounts (lower = more stable)         
    low_confidence: bool=False     # True if effective_searches < 5 (statistically unreliable)


class AgentTrustFeatures(BaseModel):
    current_credit_delay_days: int = Field(...)
    unpaid_ratio: float = Field(..., ge=0, le=100)
    unpaid_count: int = Field(..., ge=0)
    no_activity: bool = False
    
    daily: ConversionMetrics
    weekly: ConversionMetrics
    monthly: ConversionMetrics
    yearly: ConversionMetrics


class AgentTrustScores(BaseModel):
    # Old legacy scores (kept for backwards compatibility during transition if needed)
    operational_score: int = Field(default=0, ge=0, le=100)   
    
    # New Transparent Trust Components
    reliability_score: float = Field(default=0.0)
    financial_score: float = Field(default=0.0)
    experience_score: float = Field(default=0.0)
    # Combined Totals
    composite_trust_score: float = Field(default=0.0)
    ml_calibration_score: float = Field(default=0.0)
    overall_score: int = Field(..., ge=0, le=100)


class AgentTrustResult(BaseModel):
    """Domain model returned by Scorer"""
    agent_id: int
    agent_name: str
    features: AgentTrustFeatures
    scores: AgentTrustScores
    tier: str
    badges: list[str] = Field(default_factory=list)
    high_risk_flag: bool = Field(default=False)
    high_risk_reasons: list[str] = Field(default_factory=list)
    calculated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))