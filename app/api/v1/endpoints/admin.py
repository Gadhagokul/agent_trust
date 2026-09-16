# app/api/v1/endpoints/admin.py
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.v1.deps import enforce_rate_limit, get_db, require_admin
from app.api.v1.schemas import AdminTrustScoreItem, AdminTrustScoreListResponse
from app.infra.db.repository import AgentRepository
from app.security.identity import Identity
from app.services.agent_trust_scorer import AgentTrustScorer

router = APIRouter(tags=["admin"])

DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 100


@router.get("/trust-scores", response_model=AdminTrustScoreListResponse)
def list_agent_trust_scores(
    q: str | None = Query(None, max_length=120, description="Search name/email/id"),
    page: int = Query(1, ge=1),
    page_size: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE),
    db: Session = Depends(get_db),
    _identity: Identity = Depends(require_admin),
    _=Depends(enforce_rate_limit),
):
    """Admin-only filtered, paginated list of agent trust scores (full detail per item)."""
    agents, total = AgentRepository().search_agents(db, q=q, page=page, page_size=page_size)

    items: list[AdminTrustScoreItem] = []
    scorer = AgentTrustScorer()
    for agent in agents:
        result = scorer.calculate(db=db, agent_id=agent["id"])
        db.commit()
        data = result.model_dump()
        data["calculated_at"] = result.calculated_at.isoformat()
        data["establishment_name"] = agent["establishment_name"] or ""
        items.append(AdminTrustScoreItem(**data))

    return AdminTrustScoreListResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
