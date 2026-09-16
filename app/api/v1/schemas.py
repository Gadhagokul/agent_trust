# app/api/v1/schemas.py
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from app.domain.models import AgentTrustFeatures as AgentFeatures
from app.domain.models import AgentTrustScores as AgentScores


class DomainEventType(str, Enum):
    BOOKING_CREATED = "booking_created"
    BOOKING_CANCELLED = "booking_cancelled"
    CREDIT_ISSUED = "credit_issued"
    CREDIT_DEFAULTED = "credit_defaulted"
    CREDIT_REPAID = "credit_repaid"


class DomainEventPayload(BaseModel):
    agent_id: int
    event_type: DomainEventType
    event_data: dict[str, Any] = Field(default_factory=dict)


class AgentTrustScoreResponse(BaseModel):
    agent_id: int
    agent_name: str
    features: AgentFeatures
    scores: AgentScores
    tier: str
    badges: list[str] = Field(default_factory=list)
    high_risk_flag: bool
    high_risk_reasons: list[str] = Field(default_factory=list)
    calculated_at: str


class SupplierQuotaStatus(BaseModel):
    code: str
    name: str
    is_active: bool
    health_status: str
    search_limit: int
    minimum_booking: int
    consumed: int
    remaining: int
    status: str  # unused | available | exhausted | monitoring_only
    available_to_search: bool


class SupplierQuotaStatusResponse(BaseModel):
    period_type: str
    period_days: int
    computed_at: str
    suppliers: list[SupplierQuotaStatus]


class AdminTrustScoreItem(AgentTrustScoreResponse):
    establishment_name: str = ""


class AdminTrustScoreListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: list[AdminTrustScoreItem]
