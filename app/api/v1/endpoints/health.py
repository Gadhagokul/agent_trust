from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.api.v1.deps import get_settings_dep
from app.infra.db.schema_guard import validate_schema
from app.infra.db.session import get_db
from app.infra.redis_provider import RedisProvider, get_redis_provider
from app.infra.settings import Settings

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    service: str
    version: str


class ReadinessResponse(BaseModel):
    status: str
    checks: dict


@router.get("/health/live", response_model=HealthResponse)
def live(settings: Settings = Depends(get_settings_dep)) -> HealthResponse:
    return HealthResponse(
        status="ok",
        service=settings.app_name,
        version=settings.app_version,
    )


@router.get("/health/ready", response_model=ReadinessResponse)
def ready(
    redis: RedisProvider = Depends(get_redis_provider),
    db: Session = Depends(get_db),
) -> ReadinessResponse:

    # Check DB connectivity
    db_reachable = False
    try:
        db.execute(text("SELECT 1"))
        db_reachable = True
    except Exception:
        db_reachable = False

    # Check DB schema
    schema_missing = validate_schema(db)
    is_schema_ok = len(schema_missing) == 0

    checks: dict[str, object] = {
        "redis": redis.ping(),
        "database_reachable": db_reachable,
        "database_schema_ok": is_schema_ok,
    }

    if not is_schema_ok:
        checks["database_missing_schema"] = schema_missing

    status_str = (
        "ok"
        if all(v for k, v in checks.items() if k not in ("database_missing_schema",))
        else "degraded"
    )

    return ReadinessResponse(status=status_str, checks=checks)
