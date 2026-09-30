"""Phase 2 B7 security hardening: request-ID sanitization, /metrics auth,
Redis URL validation, and the startup schema guard."""

import re

import pytest
from fastapi.testclient import TestClient

from app.infra.settings import Settings, get_settings
from app.main import _run_startup_schema_check, app, startup_schema_check_required


@pytest.fixture
def client():
    return TestClient(app)


def _prod_settings(**overrides) -> Settings:
    base = {
        "app_env": "production",
        "db_host": "db",
        "db_database": "dummy_afine",
        "db_username": "root",
        "db_password": "secret",
        "laravel_service_token": "s" * 40,
        "cors_origins": "https://example.com",
        "webhook_secret": "w" * 16,
        "metrics_token": "m" * 8,
    }
    base.update(overrides)
    return Settings(**base)


# --------------------------------------------------------------------------- #
# Request-ID sanitization (senior §38: sanitized request IDs)                #
# --------------------------------------------------------------------------- #


class TestRequestIdSanitization:
    def _get_request_id(self, client, value: str) -> str:
        return client.get(
            "/v1/health/live", headers={"X-Request-Id": value}
        ).headers.get("x-request-id")

    def test_valid_request_id_passes_through(self, client):
        assert self._get_request_id(client, "abc-123_XYZ-def") == "abc-123_XYZ-def"

    def test_max_length_request_id_passes_through(self, client):
        value = "a" * 64
        assert self._get_request_id(client, value) == value

    @pytest.mark.parametrize(
        "evil",
        [
            "a\nb",
            "../etc/passwd",
            "with space",
            '"quote"',
            "a;b",
            "x" * 65,
        ],
    )
    def test_invalid_request_ids_fall_back_to_uuid(self, client, evil):
        request_id = self._get_request_id(client, evil)
        assert request_id != evil
        assert re.fullmatch(r"[0-9a-f-]{36}", request_id)


# --------------------------------------------------------------------------- #
# /metrics authentication (senior §38: API key auth)                          #
# --------------------------------------------------------------------------- #


def test_metrics_open_in_dev_environment_without_token(client):
    response = client.get("/metrics")
    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_metrics_requires_token_when_configured(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_token", "metrics-secret")

    assert client.get("/metrics").status_code == 401
    assert (
        client.get("/metrics", headers={"Authorization": "Bearer wrong"}).status_code == 401
    )

    authorised = client.get("/metrics", headers={"Authorization": "Bearer metrics-secret"})
    assert authorised.status_code == 200
    assert "text/plain" in authorised.headers["content-type"]


def test_metrics_denied_in_production_when_token_unset(monkeypatch):
    from fastapi import HTTPException

    from app.security.auth import get_metrics_viewer

    settings = get_settings()
    monkeypatch.setattr(settings, "metrics_token", "")
    monkeypatch.setattr(settings, "app_env", "production")

    with pytest.raises(HTTPException) as exc_info:
        get_metrics_viewer(None)
    assert exc_info.value.status_code == 401


# --------------------------------------------------------------------------- #
# Settings validation: REDIS_URL scheme/TLS (senior §38)                      #
# --------------------------------------------------------------------------- #


def test_redis_url_rejects_http_scheme():
    with pytest.raises(RuntimeError, match="REDIS_URL scheme"):
        _prod_settings(redis_url="http://127.0.0.1:6379/0")._validate_startup()


def test_redis_url_rejects_schemeless_url():
    with pytest.raises(RuntimeError, match="REDIS_URL scheme"):
        _prod_settings(redis_url="127.0.0.1:6379/0")._validate_startup()


def test_redis_url_rejects_missing_host():
    with pytest.raises(RuntimeError, match="REDIS_URL must include a host"):
        _prod_settings(redis_url="redis://")._validate_startup()


def test_redis_url_accepts_redis_and_rediss():
    _prod_settings(redis_url="redis://127.0.0.1:6379/0")._validate_startup()
    _prod_settings(redis_url="rediss://cache.example.com:6380/0")._validate_startup()


def test_metrics_token_required_in_production():
    with pytest.raises(RuntimeError, match="METRICS_TOKEN"):
        _prod_settings(metrics_token="")._validate_startup()


def test_metrics_token_satisfies_production_validation():
    _prod_settings(metrics_token="token-here")._validate_startup()


# --------------------------------------------------------------------------- #
# Startup schema guard (senior §39: startup validation)                       #
# --------------------------------------------------------------------------- #


def _fake_session():
    class _FakeSession:
        def close(self):
            pass

    return _FakeSession()


def test_startup_schema_check_raises_on_drift(monkeypatch):
    monkeypatch.setattr("app.main.SessionLocal", lambda: _fake_session())
    monkeypatch.setattr("app.main.validate_schema", lambda db: {"agents": ["email"]})

    with pytest.raises(RuntimeError, match="Database schema drift"):
        _run_startup_schema_check(get_settings())


def test_startup_schema_check_passes_when_clean(monkeypatch):
    monkeypatch.setattr("app.main.SessionLocal", lambda: _fake_session())
    monkeypatch.setattr("app.main.validate_schema", lambda db: {})

    _run_startup_schema_check(get_settings())


def test_startup_schema_check_gated_by_environment(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "app_env", "production")
    assert startup_schema_check_required(settings) is True
    monkeypatch.setattr(settings, "app_env", "staging")
    assert startup_schema_check_required(settings) is True
    for env in ("development", "local", "test"):
        monkeypatch.setattr(settings, "app_env", env)
        assert startup_schema_check_required(settings) is False