from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.v1.deps import (
    enforce_rate_limit,
    get_db,
    get_identity,
    get_identity_optional,
)
from app.domain.models import (
    AgentTrustFeatures,
    AgentTrustResult,
    AgentTrustScores,
    ConversionMetrics,
)
from app.main import app
from app.security.identity import Identity


def _canned_result(agent_id: int = 47, overall: float = 78.0) -> AgentTrustResult:
    daily = ConversionMetrics(
        searches=0,
        bookstep_failed=0,
        adjusted_bookstep_failed=0,
        other_step_failed=0,
        effective_searches=0,
        bookings=0,
        booking_volume=0.0,
        avg_booking_value=0.0,
        revenue_consistency=0.0,
    )
    return AgentTrustResult(
        agent_id=agent_id,
        agent_name="Test Agent",
        features=AgentTrustFeatures(
            current_max_delay_days=0,
            current_overdue_ratio=0.0,
            current_overdue_count=0,
            daily=daily,
            weekly=daily,
            monthly=daily,
            yearly=daily,
        ),
        scores=AgentTrustScores(overall_score=int(overall)),
        tier="Silver",
        badges=[],
        high_risk_flag=False,
        high_risk_reasons=[],
        calculated_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def client():
    test_app = app
    test_app.dependency_overrides[enforce_rate_limit] = lambda: MagicMock()
    test_app.dependency_overrides[get_db] = lambda: MagicMock()
    c = TestClient(test_app)
    yield c
    test_app.dependency_overrides.clear()


def _set_identity(identity):
    app.dependency_overrides[get_identity] = lambda: identity
    app.dependency_overrides[get_identity_optional] = lambda: identity


def _no_identity():
    app.dependency_overrides[get_identity_optional] = lambda: None

    def _raise_401():
        raise HTTPException(status_code=401, detail="Identity required")

    app.dependency_overrides[get_identity] = _raise_401


def _patch_scorer():
    return patch(
        "app.api.v1.endpoints.agent_trust.AgentTrustScorer",
        autospec=False,
    )


class TestTrustScoreEndpoint:
    def test_agent_can_view_own_score(self, client):
        _set_identity(Identity(user_id=25, role="agent", agent_id=47))
        with _patch_scorer() as mock_cls:
            mock_cls.return_value.calculate.return_value = _canned_result(agent_id=47)
            resp = client.get("/v1/trust-score", params={"agent_id": 47})
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == 47

    def test_agent_cannot_view_other_agent_score(self, client):
        _set_identity(Identity(user_id=25, role="agent", agent_id=47))
        resp = client.get("/v1/trust-score", params={"agent_id": 48})
        assert resp.status_code == 403

    def test_agent_without_agent_id_param_is_unprocessable(self, client):
        _set_identity(Identity(user_id=25, role="agent", agent_id=47))
        resp = client.get("/v1/trust-score")
        assert resp.status_code == 422

    def test_admin_can_view_any_agent_score(self, client):
        _set_identity(Identity(user_id=1, role="admin", agent_id=None))
        with _patch_scorer() as mock_cls:
            mock_cls.return_value.calculate.return_value = _canned_result(agent_id=48)
            resp = client.get("/v1/trust-score", params={"agent_id": 48})
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == 48

    def test_legacy_key_without_identity_can_view_(self, client):
        _no_identity()
        with _patch_scorer() as mock_cls:
            mock_cls.return_value.calculate.return_value = _canned_result(agent_id=48)
            resp = client.get("/v1/trust-score", params={"agent_id": 48})
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == 48


class TestMeEndpoint:
    def test_agent_own_score(self, client):
        _set_identity(Identity(user_id=25, role="agent", agent_id=47))
        with _patch_scorer() as mock_cls:
            mock_cls.return_value.calculate.return_value = _canned_result(agent_id=47)
            resp = client.get("/v1/me/trust-score")
        assert resp.status_code == 200
        assert resp.json()["agent_id"] == 47

    def test_admin_cannot_access_me_endpoint(self, client):
        _set_identity(Identity(user_id=1, role="admin", agent_id=None))
        resp = client.get("/v1/me/trust-score")
        assert resp.status_code == 403

    def test_missing_identity_not_allowed(self, client):
        _no_identity()
        resp = client.get("/v1/me/trust-score")
        assert resp.status_code == 401


class TestAdminListEndpoint:
    def _patch_admin_deps(self):
        repo_patch = patch(
            "app.api.v1.endpoints.admin.AgentRepository",
            autospec=False,
        )
        scorer_patch = patch(
            "app.api.v1.endpoints.admin.AgentTrustScorer",
            autospec=False,
        )
        return repo_patch, scorer_patch

    def test_admin_can_list_agents(self, client):
        _set_identity(Identity(user_id=1, role="admin", agent_id=None))
        repo_patch, scorer_patch = self._patch_admin_deps()
        with repo_patch as mock_repo, scorer_patch as mock_scorer:
            mock_repo.return_value.search_agents.return_value = (
                [{"id": 1, "establishment_name": "Acme Corp"}],
                1,
            )
            mock_scorer.return_value.calculate.return_value = _canned_result(agent_id=1)
            resp = client.get("/v1/admin/trust-scores")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 1
        assert body["page"] == 1
        assert body["page_size"] == 25
        assert body["items"][0]["agent_id"] == 1
        assert body["items"][0]["establishment_name"] == "Acme Corp"
        assert "scores" in body["items"][0]
        assert "features" in body["items"][0]
        assert "high_risk_reasons" in body["items"][0]
        assert body["items"][0]["agent_name"] == "Test Agent"

    def test_agent_forbidden_from_admin_list(self, client):
        _set_identity(Identity(user_id=25, role="agent", agent_id=47))
        resp = client.get("/v1/admin/trust-scores")
        assert resp.status_code == 403

    def test_legacy_key_forbidden_from_admin_list(self, client):
        _no_identity()
        resp = client.get("/v1/admin/trust-scores")
        assert resp.status_code == 401

    def test_search_parameters_passed(self, client):
        _set_identity(Identity(user_id=1, role="admin", agent_id=None))
        repo_patch, scorer_patch = self._patch_admin_deps()
        with repo_patch as mock_repo, scorer_patch as mock_scorer:
            mock_repo.return_value.search_agents.return_value = ([], 0)
            mock_scorer.return_value.calculate.side_effect = AssertionError("should not be called")
            resp = client.get(
                "/v1/admin/trust-scores",
                params={"q": "acme", "page": 2, "page_size": 40},
            )
        assert resp.status_code == 200
        _, kwargs = mock_repo.return_value.search_agents.call_args
        assert kwargs["q"] == "acme"
        assert kwargs["page"] == 2
        assert kwargs["page_size"] == 40

    def test_page_size_capped(self, client):
        _set_identity(Identity(user_id=1, role="admin", agent_id=None))
        resp = client.get("/v1/admin/trust-scores", params={"page_size": 1000})
        assert resp.status_code == 422
