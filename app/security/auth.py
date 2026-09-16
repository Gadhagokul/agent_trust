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