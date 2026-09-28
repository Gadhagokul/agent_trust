from types import SimpleNamespace

import pytest

from app.infra.db import repository
from app.infra.db.schema_guard import REQUIRED_SCHEMA
from app.infra.settings import Settings


class _Result:
    def __init__(self, value, many=False):
        self.value = value
        self.many = many

    def fetchone(self):
        return self.value

    def fetchall(self):
        return self.value


class _FakeDb:
    def __init__(self, calls):
        self.calls = calls

    def execute(self, *args, **kwargs):
        return self.calls.pop(0)


class _CaptureDb:
    """Captures executed SQL for assertion; returns a single stubbed result."""

    def __init__(self, row):
        self.row = row
        self.sqls = []
        self.params = []

    def execute(self, sql, params=None):
        self.sqls.append(str(sql))
        self.params.append(params)
        return _Result(self.row)


def _make_fake_db(search_row=(100, 100, 100, 100)):
    return _FakeDb(
        [
            _Result(search_row),
            _Result((0, 0, 0, 0)),
            _Result((0, 0, 0, 0)),
            _Result([], many=True),
        ]
    )


def _threshold_stub(thresholds):
    return lambda: SimpleNamespace(conversion_thresholds=thresholds)


def test_conversion_thresholds_default(monkeypatch):
    monkeypatch.setattr(
        repository,
        "get_settings",
        _threshold_stub({1: 15, 7: 50, 30: 150, 365: 500}),
    )
    stats = repository.AgentRepository().get_multi_timeframe_stats(_make_fake_db(), 1)

    for days in (1, 7, 30, 365):
        assert stats[days]["effective_searches"] == 100
        assert stats[days]["low_confidence"] is False


@pytest.mark.parametrize(
    "thresholds,expected_365_low_confidence",
    [
        ({1: 15, 7: 50, 30: 150, 365: 200}, False),
        ({1: 15, 7: 50, 30: 150, 365: 600}, True),
    ],
)
def test_conversion_thresholds_override_365(monkeypatch, thresholds, expected_365_low_confidence):
    monkeypatch.setattr(repository, "get_settings", _threshold_stub(thresholds))
    stats = repository.AgentRepository().get_multi_timeframe_stats(_make_fake_db(), 1)

    for days in (1, 7, 30):
        assert stats[days]["low_confidence"] is False
    assert stats[365]["low_confidence"] is expected_365_low_confidence


def test_conversion_thresholds_validation_empty():
    settings = Settings(_env_file=None)
    settings.conversion_thresholds = {}

    with pytest.raises(RuntimeError, match="conversion_thresholds must be non-empty"):
        settings._validate_startup()


def test_conversion_thresholds_validation_below_min():
    settings = Settings(_env_file=None)
    settings.conversion_thresholds = {1: 0}

    with pytest.raises(RuntimeError, match="conversion_thresholds values must be at least 1"):
        settings._validate_startup()


def test_credit_overdue_boundary_validation_rejects_invalid():
    settings = Settings(_env_file=None)
    settings.credit_overdue_boundary = ">"

    with pytest.raises(RuntimeError, match="credit_overdue_boundary"):
        settings._validate_startup()


def test_credit_consecutive_cycles_validation_below_min():
    settings = Settings(_env_file=None)
    settings.credit_max_consecutive_overdue_cycles = 0

    with pytest.raises(RuntimeError, match="credit_max_consecutive_overdue_cycles"):
        settings._validate_startup()


def test_schema_guard_covers_supplier_l2b_columns():
    assert "provider" in REQUIRED_SCHEMA["bookings"]
    assert {"search_session_id", "supplier_code"}.issubset(
        REQUIRED_SCHEMA["search_supplier_runs"]
    )
    assert {"search_session_id", "agent_id", "first_accessed_at"}.issubset(
        REQUIRED_SCHEMA["search_session_accesses"]
    )
    assert {"code", "name", "minimum_booking", "search_limit"}.issubset(
        REQUIRED_SCHEMA["suppliers"]
    )


# ──────────────────────────────────────────────
# get_credit_stats: overdue semantics + current/historical split
# ──────────────────────────────────────────────
class TestGetCreditStats:
    def _default_row(self):
        # current_overdue_count, ratio, max_delay, outstanding, late_count,
        # late_ratio, avg_delay, max_delay_historical, consecutive_unpaid_cycles
        return (2, 20.0, 15, 500.0, 3, 10.0, 8.0, 30, 4)

    def test_maps_all_fields_separating_current_from_historical(self):
        stats = repository.AgentRepository().get_credit_stats(_CaptureDb(self._default_row()), 7)

        assert stats.current_overdue_count == 2
        assert stats.current_overdue_ratio == 20.0
        assert stats.current_max_delay_days == 15
        assert stats.outstanding_amount == 500.0
        assert stats.historical_late_payment_count == 3
        assert stats.historical_late_payment_ratio == 10.0
        assert stats.average_payment_delay_days == 8.0
        assert stats.maximum_payment_delay_days == 30
        assert stats.consecutive_unpaid_cycles == 4

    def test_default_boundary_is_exclusive_due_today_not_overdue(self):
        db = _CaptureDb(self._default_row())
        repository.AgentRepository().get_credit_stats(db, 7)

        sql = db.sqls[0]
        assert "due_date < CURRENT_DATE()" in sql
        assert "due_date <= CURRENT_DATE()" not in sql

    def test_inclusive_boundary_after_business_confirmation(self, monkeypatch):
        monkeypatch.setattr(
            repository,
            "get_settings",
            lambda: SimpleNamespace(credit_overdue_boundary="<="),
        )
        db = _CaptureDb(self._default_row())
        repository.AgentRepository().get_credit_stats(db, 7)

        assert "due_date <= CURRENT_DATE()" in db.sqls[0]

    def test_historical_max_delay_sql_present(self):
        db = _CaptureDb(self._default_row())
        repository.AgentRepository().get_credit_stats(db, 7)

        sql = db.sqls[0]
        assert "maximum_payment_delay_days" in sql
        assert "DATEDIFF(ct.payment_date, ct.due_date)" in sql


# ──────────────────────────────────────────────
# Supplier-specific L2B (senior §10-13)
# ──────────────────────────────────────────────
def test_l2b_policy_validation_rejects_unknown_policy():
    settings = Settings(_env_file=None)
    settings.l2b_not_configured_policy = "fallback_global"

    with pytest.raises(RuntimeError, match="l2b_not_configured_policy"):
        settings._validate_startup()


def test_l2b_max_supplier_share_validation_out_of_range():
    settings = Settings(_env_file=None)
    settings.l2b_max_supplier_share = 1.5

    with pytest.raises(RuntimeError, match="l2b_max_supplier_share"):
        settings._validate_startup()


class TestGetSupplierL2bTargets:
    def test_computes_site_benchmarks(self):
        db = _CaptureDb([
            ("EK", "Emirates", 10, 200),
            ("QR", "Qatar", 10, 100),
        ])
        targets = repository.AgentRepository().get_supplier_l2b_targets(db)

        assert targets == [
            {"code": "EK", "name": "Emirates", "target": 0.05},
            {"code": "QR", "name": "Qatar", "target": 0.1},
        ]

    def test_missing_or_invalid_columns_give_none(self):
        db = _CaptureDb([
            ("EK", "Emirates", None, 200),
            ("QR", "Qatar", 10, None),
            ("EY", "Etihad", 10, 0),
        ])
        targets = repository.AgentRepository().get_supplier_l2b_targets(db)

        assert all(target["target"] is None for target in targets)

    def test_sql_reads_suppliers_site_level(self):
        db = _CaptureDb([])
        repository.AgentRepository().get_supplier_l2b_targets(db)

        assert "FROM suppliers" in db.sqls[0]
        assert "is_active" in db.sqls[0]


class TestGetAgentSupplierSearches:
    def test_sums_access_count_per_supplier(self):
        db = _CaptureDb([("EK", 33), ("QR", 22)])
        rows = repository.AgentRepository().get_agent_supplier_searches(db, 7)

        assert rows == [
            {"code": "EK", "searches": 33},
            {"code": "QR", "searches": 22},
        ]

    def test_sql_sums_access_count_without_access_type_filter(self):
        db = _CaptureDb([])
        repository.AgentRepository().get_agent_supplier_searches(db, 7)

        sql = db.sqls[0]
        assert "SUM(a.access_count)" in sql
        assert "search_supplier_runs" in sql
        assert "first_access_type" not in sql


class TestGetAgentBookingCountsByProvider:
    def test_counts_bookings_per_provider(self):
        db = _CaptureDb([("Emirates", 2), ("Qatar", 1)])
        rows = repository.AgentRepository().get_agent_booking_counts_by_provider(db, 7)

        assert rows == [
            {"provider": "Emirates", "bookings": 2},
            {"provider": "Qatar", "bookings": 1},
        ]

    def test_sql_groups_by_provider_and_filters_statuses(self):
        db = _CaptureDb([])
        repository.AgentRepository().get_agent_booking_counts_by_provider(db, 7)

        sql = db.sqls[0]
        assert "provider" in sql
        assert "GROUP BY provider" in sql
        assert "status IN" in sql