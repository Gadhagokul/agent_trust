from fastapi import Depends, HTTPException, status
from fastapi.security import APIKeyHeader

from app.infra.settings import get_settings

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


class Principal:
    def __init__(self, api_key: str):
        self.api_key = api_key


def get_current_principal(
    api_key: str = Depends(api_key_header),
) -> Principal:
    settings = get_settings()

    # Bypass in development for easier testing
    if settings.app_env in ("development", "local"):
        return Principal(api_key=api_key or "dev-key")

    if not api_key or api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid API Key",
        )
    return Principal(api_key=api_key)