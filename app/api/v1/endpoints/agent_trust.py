# app/api/v1/endpoints/agent_trust.py
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    enforce_rate_limit,
    get_db,
    get_identity_optional,
    require_agent,
)
from app.api.v1.schemas import AgentTrustScoreResponse
from app.domain.errors import DomainError
from app.security.identity import Identity
from app.services.agent_trust_scorer import AgentTrustScorer

router = APIRouter(tags=["agent-trust"])


def _compute_score_response(db: Session, agent_id: int) -> AgentTrustScoreResponse | JSONResponse:
    scorer = AgentTrustScorer()

    try:
        result = scorer.calculate(db=db, agent_id=agent_id)

        db.commit()  # Commit any pending transactions (e.g., audit logs)

        response_data = result.model_dump()
        response_data["calculated_at"] = result.calculated_at.isoformat()

        return AgentTrustScoreResponse(**response_data)

    except DomainError as exc:
        db.rollback()  # Rollback any pending transactions in case of domain errors
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )
    except Exception:
        db.rollback()
        raise


@router.get("/me/trust-score", response_model=AgentTrustScoreResponse)
def get_my_trust_score(
    db: Session = Depends(get_db),
    _=Depends(enforce_rate_limit),
    identity: Identity = Depends(require_agent),
):
    """Return the authenticated agent's own trust score and details."""
    agent_id = identity.agent_id
    if agent_id is None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No agent account linked to this user",
        )
    return _compute_score_response(db, agent_id)


@router.get("/trust-score", response_model=AgentTrustScoreResponse)
def get_agent_trust_score(
    agent_id: int = Query(..., ge=1, description="Agent ID"),
    db: Session = Depends(get_db),
    _=Depends(enforce_rate_limit),
    identity: Identity | None = Depends(get_identity_optional),
):
    """
    Return a trust score for an agent.

    Admin identities (or legacy callers presenting a valid internal key with
    no X-User-Id) may request any agent. Agent identities may only request
    their own agent_id.
    """
    if identity is not None and identity.role != "admin":
        if identity.role != "agent" or identity.agent_id != agent_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Not authorized to view this agent's score",
            )
    return _compute_score_response(db, agent_id)
