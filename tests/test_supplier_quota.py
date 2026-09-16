# tests/test_supplier_quota.py
"""Feature A (site-wide supplier quota) + Feature B repo-helper tests."""

from unittest.mock import MagicMock, patch

import pytest

from app.api.v1.deps import enforce_rate_limit, get_db
from app.infra.db.repository import (
    AgentRepository,
    _bind_placeholders,
    _window_sql,
)


# ──────────────────────────────────────────────
# Pure helpers: _window_sql / _bind_placeholders
# ──────────────────────────────────────────────
class TestWindowSql:
    def test_lifetime_has_no_date_filter(self):
        sql, params = _window_sql("lifetime", 30)
        assert sql == "TRUE"
        assert params == {}

    def test_daily_starts_today(self):
        sql, params = _window_sql("daily", 30)
        assert sql == "CURDATE()"
        assert params == {}

    def test_monthly_is_rolling_30_days(self):
        sql, params = _window_sql("monthly", 30)
        assert sql == "DATE_SUB(CURDATE(), INTERVAL 30 DAY)"
        assert params == {}

    def test_rolling_uses_provided_days(self):
        sql, params = _window_sql("rolling", 7)
        assert sql == "DATE_SUB(NOW(), INTERVAL :rolling_days DAY)"
        assert params == {"rolling_days": 7}

    def test_rolling_rejects_invalid_days(self):
        with pytest.raises(ValueError):
            _window_sql("rolling", 0)

    def test_unsupported_period_raises(self):
        with pytest.raises(ValueError):
            _window_sql("fortnightly", 30)


class TestBindPlaceholders:
    def test_single_value(self):
        ph, params = _bind_placeholders("s", ["success"])
        assert ph == "(:s0)"
        assert params == {"s0": "success"}

    def test_multiple_values(self):
        ph, params = _bind_placeholders("s", ["confirmed", "ticketed"])
        assert ph == "(:s0, :s1)"
        assert params == {"s0": "confirmed", "s1": "ticketed"}


# ──────────────────────────────────────────────
# Repository behavior (stubbed db)
# ──────────────────────────────────────────────
class StubResult:
    def __init__(self, rows=None, row=None, scalar=0):
        self._rows = rows or []
        self._row = row
        self._scalar = scalar

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._row

    def scalar(self):
        return self._scalar


class StubDB:
    def __init__(self, rows=None, row=None, scalar=0):
        self._rows = rows
        self._row = row
        self._scalar = scalar
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(str(sql))
        self.params.append(params)
        return StubResult(rows=self._rows, row=self._row, scalar=self._scalar)


def _supplier_row(id_, code, name, is_active, health, limit, min_booking, consumed):
    return (id_, code, name, is_active, health, limit, min_booking, consumed)


class TestGetSupplierQuotaStatus:
    def _status_for(self, row):
        db = StubDB(rows=[row])
        return AgentRepository().get_supplier_quota_status(db)[0]

    def test_monitoring_only_for_inactive(self):
        row = _supplier_row(6, "LUFTHANSA", "Lufthansa", 0, "unknown", 3000, 150, 500)
        s = self._status_for(row)
        assert s["status"] == "monitoring_only"
        assert s["available_to_search"] is False

    def test_unused_when_no_consumption(self):
        row = _supplier_row(1, "AEGEAN", "Aegean Airlines", 1, "healthy", 4000, 200, 0)
        s = self._status_for(row)
        assert s["status"] == "unused"
        assert s["consumed"] == 0
        assert s["remaining"] == 4000
        assert s["available_to_search"] is True

    def test_available_below_limit(self):
        row = _supplier_row(2, "EMIRATES", "Emirates", 1, "healthy", 5000, 250, 3000)
        s = self._status_for(row)
        assert s["status"] == "available"
        assert s["remaining"] == 2000
        assert s["available_to_search"] is True

    def test_exhausted_at_limit(self):
        row = _supplier_row(3, "QATAR", "Qatar Airways", 1, "healthy", 4500, 225, 4500)
        s = self._status_for(row)
        assert s["status"] == "exhausted"
        assert s["remaining"] == 0
        assert s["available_to_search"] is False

    def test_exhausted_over_limit_clamps_remaining(self):
        row = _supplier_row(4, "ETIHAD", "Etihad Airways", 1, "warning", 3500, 175, 4000)
        s = self._status_for(row)
        assert s["status"] == "exhausted"
        assert s["remaining"] == 0
        assert s["available_to_search"] is False

    def test_sql_enforces_created_only_consumption(self):
        rows = [_supplier_row(2, "EMIRATES", "Emirates", 1, "healthy", 5000, 250, 100)]
        db = StubDB(rows=rows)
        AgentRepository().get_supplier_quota_status(db)

        sql = db.sqls[0]
        assert "COUNT(DISTINCT r.search_session_id)" in sql
        assert "first_access_type = 'created'" in sql
        assert "EXISTS (" in sql
        assert "status IN (:s0)" in sql

    def test_period_type_and_days_forwarded(self):
        rows = [_supplier_row(2, "EMIRATES", "Emirates", 1, "healthy", 5000, 250, 100)]
        db = StubDB(rows=rows)
        AgentRepository().get_supplier_quota_status(db, period_type="rolling", period_days=7)

        assert "DATE_SUB(NOW(), INTERVAL :rolling_days DAY)" in db.sqls[0]
        assert db.params[0]["rolling_days"] == 7

    def test_invalid_period_type_raises_before_query(self):
        db = StubDB()
        with pytest.raises(ValueError):
            AgentRepository().get_supplier_quota_status(db, period_type="fortnightly")


class TestGetSupplierExpectedRatio:
    def test_active_supplier_ratio(self):
        db = StubDB(scalar=0.05)
        assert AgentRepository().get_supplier_expected_ratio(db) == 0.05

    def test_zero_when_no_active_limits(self):
        db = StubDB(scalar=0)
        assert AgentRepository().get_supplier_expected_ratio(db) == 0.0


class TestGetAgentSearchActivity:
    def test_returns_created_reused_searches_and_bookings(self):
        db = StubDB(row=(12, 8, 4), scalar=3)
        result = AgentRepository().get_agent_search_activity(db, agent_id=7)

        assert result == {"searches": 12, "created": 8, "reused": 4, "bookings": 3}
        assert len(db.sqls) == 2
        assert "COUNT(DISTINCT search_session_id)" in db.sqls[0]
        assert "first_accessed_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)" in db.sqls[0]

    def test_bookings_filtered_by_success_statuses(self):
        db = StubDB(row=(0, 0, 0), scalar=0)
        AgentRepository().get_agent_search_activity(db, agent_id=7)

        booking_sql = db.sqls[1]
        assert "status IN (:s0, :s1)" in booking_sql
        assert db.params[1]["s0"] == "confirmed"
        assert db.params[1]["s1"] == "ticketed"


# ──────────────────────────────────────────────
# Endpoint contract
# ──────────────────────────────────────────────
@pytest.fixture
def client():
    from app.main import app

    app.dependency_overrides[enforce_rate_limit] = lambda: MagicMock()
    app.dependency_overrides[get_db] = lambda: MagicMock()

    from fastapi.testclient import TestClient

    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


_SUPPLIER = {
    "code": "EMIRATES",
    "name": "Emirates",
    "is_active": True,
    "health_status": "healthy",
    "search_limit": 5000,
    "minimum_booking": 250,
    "consumed": 5000,
    "remaining": 0,
    "status": "exhausted",
    "available_to_search": False,
}


class TestSupplierQuotaEndpoint:
    def test_quota_status_returns_suppliers(self, client):
        with patch("app.api.v1.endpoints.suppliers.AgentRepository") as mock_cls:
            mock_cls.return_value.get_supplier_quota_status.return_value = [_SUPPLIER]
            resp = client.get("/v1/suppliers/quota-status")

        assert resp.status_code == 200
        body = resp.json()
        assert body["period_type"] == "lifetime"
        assert isinstance(body["computed_at"], str)
        assert body["suppliers"][0]["code"] == "EMIRATES"
        assert body["suppliers"][0]["status"] == "exhausted"
        assert body["suppliers"][0]["available_to_search"] is False

    def test_quota_status_forwards_period_overrides(self, client):
        with patch("app.api.v1.endpoints.suppliers.AgentRepository") as mock_cls:
            mock_cls.return_value.get_supplier_quota_status.return_value = [_SUPPLIER]
            resp = client.get(
                "/v1/suppliers/quota-status",
                params={"period_type": "rolling", "days": 7},
            )

        assert resp.status_code == 200
        _, kwargs = mock_cls.return_value.get_supplier_quota_status.call_args
        assert kwargs["period_type"] == "rolling"
        assert kwargs["period_days"] == 7
        assert resp.json()["period_type"] == "rolling"
        assert resp.json()["period_days"] == 7

    def test_single_supplier_filters_by_code_match(self, client):
        with patch("app.api.v1.endpoints.suppliers.AgentRepository") as mock_cls:
            mock_cls.return_value.get_supplier_quota_status.return_value = [_SUPPLIER]
            resp = client.get("/v1/suppliers/emirates/quota-status")

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["suppliers"]) == 1
        assert body["suppliers"][0]["code"] == "EMIRATES"

    def test_unknown_supplier_returns_404(self, client):
        with patch("app.api.v1.endpoints.suppliers.AgentRepository") as mock_cls:
            mock_cls.return_value.get_supplier_quota_status.return_value = [_SUPPLIER]
            resp = client.get("/v1/suppliers/XXX/quota-status")

        assert resp.status_code == 404

    def test_invalid_period_type_returns_400(self, client):
        with patch("app.api.v1.endpoints.suppliers.AgentRepository") as mock_cls:
            mock_cls.return_value.get_supplier_quota_status.side_effect = ValueError(
                "Unsupported period_type: 'fortnightly'"
            )
            resp = client.get("/v1/suppliers/quota-status", params={"period_type": "fortnightly"})

        assert resp.status_code == 400
        assert resp.json()["detail"]["code"] == "invalid_period_type"
