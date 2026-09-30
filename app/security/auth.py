import hmac

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.infra.settings import get_settings

service_token_scheme = HTTPBearer(auto_error=False)


class Principal:
    def __init__(self, service_token: str):
        self.service_token = service_token


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(service_token_scheme),
) -> Principal:
    settings = get_settings()

    token = credentials.credentials if credentials is not None else ""
    expected = settings.laravel_service_token

    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid service token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return Principal(service_token=token)


metrics_token_scheme = HTTPBearer(auto_error=False)


def get_metrics_viewer(
    credentials: HTTPAuthorizationCredentials | None = Depends(metrics_token_scheme),
) -> None:
    """Gate the Prometheus /metrics endpoint.

    When METRICS_TOKEN is configured the bearer token is required and verified
    with a constant-time compare. When it is unset the endpoint stays open only
    in the development/local/test environments (where startup validation does
    not require the token); production/staging deny instead of failing open.
    """
    settings = get_settings()
    token = credentials.credentials if credentials is not None else ""

    if settings.metrics_token:
        if not hmac.compare_digest(token, settings.metrics_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid metrics token",
                headers={"WWW-Authenticate": "Bearer"},
            )
        return None

    if settings.app_env not in ("development", "local", "test"):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Metrics endpoint is restricted",
        )
    return None
