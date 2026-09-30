import hmac
import logging

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel

from app.api.v1.deps import enforce_rate_limit
from app.api.v1.schemas import DomainEventPayload
from app.infra.settings import get_settings
from app.observability.metrics import WEBHOOKS
from app.security.auth import Principal
from app.services.cache_adapter import CacheAdapter

logger = logging.getLogger(__name__)

router = APIRouter()


class CacheInvalidateResponse(BaseModel):
    status: str
    message: str
    agent_id: int
    event_type: str


@router.post("/domain-event", response_model=CacheInvalidateResponse)
def handle_domain_event(
    payload: DomainEventPayload,
    x_webhook_secret: str = Header(..., alias="X-Webhook-Secret"),
    _: Principal = Depends(enforce_rate_limit),
):
    """
    Webhook meant to be called by external Core Services (e.g., Booking Engine, Credit Engine)
    whenever an agent's fundamental state drastically changes (new booking, missed payment).
    This guarantees Trust Scores recalculate dynamically on the next fetch.
    """
    settings = get_settings()

    if not settings.webhook_secret:
        WEBHOOKS.labels(outcome="no_secret_configured").inc()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Webhook secret not configured",
        )

    if not hmac.compare_digest(x_webhook_secret, settings.webhook_secret):
        WEBHOOKS.labels(outcome="invalid_secret").inc()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid webhook secret",
        )

    if payload.agent_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Valid agent_id must be provided.",
        )

    cache = CacheAdapter()
    cache_key = f"trust:agent:{payload.agent_id}:conversion"

    cache.invalidate(cache_key)

    WEBHOOKS.labels(outcome="received").inc()

    logger.info(
        "[Domain Event] Dropped active cache for Agent %s due to %s",
        payload.agent_id,
        payload.event_type.value,
    )

    return CacheInvalidateResponse(
        status="success",
        message="Agent cache successfully invalidated via Domain Event.",
        agent_id=payload.agent_id,
        event_type=payload.event_type.value,
    )
