from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status

from app.infra.db.session import get_db  # noqa: F401
from app.infra.settings import get_settings
from app.security.auth import Principal, get_current_principal
from app.security.rate_limit import SlidingWindowRateLimiter, get_rate_limiter


@lru_cache(maxsize=1)
def get_settings_dep():
    return get_settings()


def enforce_rate_limit(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    limiter: SlidingWindowRateLimiter = Depends(get_rate_limiter),
) -> Principal:
    key = principal.api_key or (request.client.host if request.client else "unknown")
    if not limiter.allow(key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
        )
    return principal