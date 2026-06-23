# app/api/v1/endpoints/agent_trust.py
from fastapi import APIRouter, Depends, Query
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import enforce_rate_limit, get_db
from app.api.v1.schemas import AgentTrustScoreResponse
from app.domain.errors import DomainError
from app.services.agent_trust_scorer import AgentTrustScorer

router = APIRouter(tags=["agent-trust"])


@router.get("/trust-score", response_model=AgentTrustScoreResponse)
def get_agent_trust_score(
    agent_id: int = Query(..., ge=1, description="Agent ID"),
    db: Session = Depends(get_db),
    _=Depends(enforce_rate_limit),
):
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
    