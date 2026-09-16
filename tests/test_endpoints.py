import pytest
from fastapi.testclient import TestClient

from app.infra.settings import get_settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


class TestRootEndpoint:
    def test_root_returns_service_info(self, client):
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "version" in data
        assert "env" in data

    def test_root_includes_app_name(self, client):
        settings = get_settings()
        response = client.get("/")
        assert response.json()["service"] == settings.app_name


class TestHealthLiveEndpoint:
    def test_live_returns_ok(self, client):
        response = client.get("/v1/health/live")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert "service" in data
        assert "version" in data

    def test_live_includes_service_name(self, client):
        settings = get_settings()
        response = client.get("/v1/health/live")
        assert response.json()["service"] == settings.app_name


class TestOpenApiDocs:
    def test_openapi_schema(self, client):
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert schema["info"]["title"] == get_settings().app_name

    def test_trust_score_endpoint_documented(self, client):
        response = client.get("/openapi.json")
        paths = response.json()["paths"]
        assert "/v1/trust-score" in paths

    def test_health_endpoints_documented(self, client):
        response = client.get("/openapi.json")
        paths = response.json()["paths"]
        assert "/v1/health/live" in paths
        assert "/v1/health/ready" in paths

    def test_supplier_quota_endpoints_documented(self, client):
        response = client.get("/openapi.json")
        paths = response.json()["paths"]
        assert "/v1/suppliers/quota-status" in paths
        assert "/v1/suppliers/{code}/quota-status" in paths

    def test_trust_score_schema_includes_search_to_booking(self, client):
        response = client.get("/openapi.json")
        schema = response.json()
        scores_schema = schema["components"]["schemas"]["AgentTrustScores"]
        features_schema = schema["components"]["schemas"]["AgentTrustFeatures"]
        assert "search_to_booking_score" in scores_schema["properties"]
        assert "search_activity" in features_schema["properties"]

    def test_supplier_quota_schema(self, client):
        response = client.get("/openapi.json")
        schema = response.json()["components"]["schemas"]
        supplier = schema["SupplierQuotaStatus"]["properties"]
        assert "available_to_search" in supplier
        assert "consumed" in supplier
        assert "search_limit" in supplier
