import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.v1.schemas import DomainEventPayload
from app.services.cache_adapter import CacheAdapter

logger = logging.getLogger(__name__)

router = APIRouter()
cache = CacheAdapter()

class CacheInvalidateResponse(BaseModel):
    status: str
    message: str
    agent_id: int
    event_type: str

@router.post("/domain-event", response_model=CacheInvalidateResponse)
def handle_domain_event(payload: DomainEventPayload):
    """
    Webhook meant to be called by external Core Services (e.g., Booking Engine, Credit Engine)
    whenever an agent's fundamental state drastically changes (new booking, missed payment).
    This guarantees Trust Scores recalculate dynamically on the next fetch.
    """
    if payload.agent_id <= 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, 
            detail="Valid agent_id must be provided."
        )
        
    cache_key = f"trust:agent:{payload.agent_id}:conversion"
    
    # We only delete the primary fast-cache, ensuring the stale 24h fallback
    # survives in the event the database goes offline simultaneously.
    cache.invalidate(cache_key)
    
    logger.info("[Domain Event] Dropped active cache for Agent %s due to %s", payload.agent_id, payload.event_type.value)
    
    return CacheInvalidateResponse(
        status="success",
        message="Agent cache successfully invalidated via Domain Event.",
        agent_id=payload.agent_id,
        event_type=payload.event_type.value
    )
