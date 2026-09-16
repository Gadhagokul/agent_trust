import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from app.infra.settings import Settings
from app.security.auth import Principal, get_current_principal

VALID_TOKEN = "test-service-token-0123456789abcdef"


def _probe_app() -> FastAPI:
    app = FastAPI()

    @app.get("/probe")
    def probe(_: Principal = Depends(get_current_principal)) -> dict:
        return {"ok": True}

    return app


def test_missing_bearer_token_returns_401():
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid service token"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_wrong_bearer_token_returns_401():
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe", headers={"Authorization": "Bearer wrong-token"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "Invalid service token"
    assert resp.headers["WWW-Authenticate"] == "Bearer"


def test_correct_bearer_token_allows_request():
    with TestClient(_probe_app()) as client:
        resp = client.get("/probe", headers={"Authorization": f"Bearer {VALID_TOKEN}"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_empty_service_token_fails_startup_validation():
    settings = Settings(
        laravel_service_token="",
        db_host="h",
        db_database="d",
        db_username="u",
        db_password="p",
    )
    with pytest.raises(RuntimeError, match="LARAVEL_SERVICE_TOKEN must be set"):
        settings._validate_startup()


def test_short_service_token_fails_startup_validation():
    settings = Settings(
        laravel_service_token="too-short",
        db_host="h",
        db_database="d",
        db_username="u",
        db_password="p",
    )
    with pytest.raises(RuntimeError, match="at least 32 characters"):
        settings._validate_startup()