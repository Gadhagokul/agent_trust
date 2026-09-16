from functools import lru_cache

from fastapi import Depends, Header, HTTPException, Request, status  # ⭐ ADDED Header
from sqlalchemy.orm import Session

from app.infra.db.session import get_db
from app.infra.settings import get_settings
from app.security.auth import Principal, get_current_principal
from app.security.identity import Identity, resolve_identity
from app.security.rate_limit import (
    RedisSlidingWindowRateLimiter,
    SlidingWindowRateLimiter,
    get_rate_limiter,
)


@lru_cache(maxsize=1)
def get_settings_dep():
    return get_settings()


def get_identity_optional(
    request: Request,
    db: Session = Depends(get_db),
    # ⭐ ADDED: expose X-User-Id in Swagger
    _x_user_id: str | None = Header(
        default=None,
        alias="X-User-Id",
    ),
) -> Identity | None:
    """Resolve the caller's identity when an X-User-Id header is present."""

    if request.headers.get("x-user-id") is None:
        return None

    return resolve_identity(request, db)


def get_identity(
    request: Request,
    db: Session = Depends(get_db),
    # ⭐ ADDED: expose X-User-Id in Swagger
    _x_user_id: str | None = Header(
        default=None,
        alias="X-User-Id",
    ),
    # ⭐ ADDED: expose X-Agent-Id in Swagger
    _x_agent_id: str | None = Header(
        default=None,
        alias="X-Agent-Id",
    ),
    # ⭐ ADDED: expose X-User-Role in Swagger for local testing
    _x_user_role: str | None = Header(
        default=None,
        alias="X-User-Role",
    ),
) -> Identity:
    """Require the caller to present a valid X-User-Id identity."""

    return resolve_identity(request, db)


def require_agent(identity: Identity = Depends(get_identity)) -> Identity:
    if identity.role != "agent":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Agent access required",
        )

    return identity


def require_admin(identity: Identity = Depends(get_identity)) -> Identity:
    if identity.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    return identity


def enforce_rate_limit(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    identity: Identity | None = Depends(get_identity_optional),
    limiter: SlidingWindowRateLimiter | RedisSlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> Principal:
    key = principal.service_token or (request.client.host if request.client else "unknown")

    if identity is not None:
        key = f"{key}:{identity.user_id}"

    if not limiter.allow(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )

    return principal
