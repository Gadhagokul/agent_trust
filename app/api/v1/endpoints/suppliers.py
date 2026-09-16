# app/api/v1/endpoints/suppliers.py
"""
Site-wide supplier request quota endpoints (Feature A).

The search service (Laravel) calls these before issuing a supplier request.
`available_to_search` is the decision flag for whether a new created search
request to that supplier may proceed: once the site-wide created-only request
count reaches `suppliers.search_limit`, the quota is exhausted for the entire
app and no further supplier requests are taken.
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app.api.v1.deps import (
    enforce_rate_limit,
    get_db,
    get_identity_optional,
)
from app.api.v1.schemas import SupplierQuotaStatus, SupplierQuotaStatusResponse
from app.domain.errors import DomainError
from app.infra.db.repository import AgentRepository
from app.infra.settings import get_settings
from app.security.identity import Identity

router = APIRouter(tags=["suppliers"])


def _get_quota_response(
    db: Session,
    period_type: str | None,
    days: int | None,
) -> SupplierQuotaStatusResponse | JSONResponse:
    try:
        settings = get_settings()
        supplier_rows = AgentRepository().get_supplier_quota_status(
            db, period_type=period_type, period_days=days
        )
        return SupplierQuotaStatusResponse(
            period_type=period_type or settings.supplier_quota_period_type,
            period_days=days if days is not None else settings.supplier_quota_period_days,
            computed_at=datetime.now(timezone.utc).isoformat(),
            suppliers=[SupplierQuotaStatus(**row) for row in supplier_rows],
        )
    except ValueError as exc:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": {"code": "invalid_period_type", "message": str(exc)}},
        )
    except DomainError as exc:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": {"code": exc.code, "message": exc.message}},
        )


@router.get("/quota-status", response_model=SupplierQuotaStatusResponse)
def get_all_supplier_quota(
    period_type: str | None = Query(
        default=None,
        description="Overrides the configured quota window: lifetime|daily|monthly|rolling",
    ),
    days: int | None = Query(default=None, ge=1, description="Window days for 'rolling'"),
    db: Session = Depends(get_db),
    _=Depends(enforce_rate_limit),
    _identity: Identity | None = Depends(get_identity_optional),
):
    """Site-wide supplier request quota for every supplier (decision layer)."""
    response = _get_quota_response(db, period_type, days)
    if isinstance(response, JSONResponse):
        return response
    return response


@router.get("/{code}/quota-status", response_model=SupplierQuotaStatusResponse)
def get_supplier_quota(
    code: str,
    period_type: str | None = Query(
        default=None,
        description="Overrides the configured quota window: lifetime|daily|monthly|rolling",
    ),
    days: int | None = Query(default=None, ge=1, description="Window days for 'rolling'"),
    db: Session = Depends(get_db),
    _=Depends(enforce_rate_limit),
    _identity: Identity | None = Depends(get_identity_optional),
):
    """Site-wide deposit quota for a single supplier code."""
    response = _get_quota_response(db, period_type, days)
    if isinstance(response, JSONResponse):
        return response

    matches = [s for s in response.suppliers if s.code.upper() == code.upper()]
    if not matches:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown supplier code: {code}",
        )
    return SupplierQuotaStatusResponse(
        period_type=response.period_type,
        period_days=response.period_days,
        computed_at=response.computed_at,
        suppliers=matches,
    )
