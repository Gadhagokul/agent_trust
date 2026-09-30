"""Phase 3 B8 observability: HTTP telemetry labels, access log, cache signals,
rate-limit/webhook/schema-drift counters, and the scoring-latency unit fix."""

import logging
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from app.api.v1.deps import enforce_rate_limit
from app.infra.db.schema_guard import validate_schema
from app.infra.settings import get_settings
from app.main import app
from app.services.agent_trust_scorer import AgentTrustScorer


def _sample(name: str, **labels) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return 0.0 if value is None else float(value)


@pytest.fixture
def client():
    return TestClient(app)


def _cached_stats():
    conv = {
        "searches": 0,
        "bookstep_failed": 0,
        "adjusted_bookstep_failed": 0,
        "other_step_failed": 0,
        "effective_searches": 0,
        "no_activity": True,
        "bookings": 0,
        "booking_volume": 0.0,
        "avg_booking_value": 0.0,
        "revenue_consistency": 0.0,
        "low_confidence": True,
    }
    return {
        1: conv,
        7: conv,
        30: conv,
        365: conv,
    }


# --------------------------------------------------------------------------- #
# HTTP telemetry                                                              #
# --------------------------------------------------------------------------- #


def test_http_counter_uses_route_template(client):
    before = _sample(
        "agent_trust_http_requests_total",
        status="200",
        method="GET",
        route="/v1/health/live",
    )
    assert client.get("/v1/health/live").status_code == 200
    assert (
        _sample(
            "agent_trust_http_requests_total",
            status="200",
            method="GET",
            route="/v1/health/live",
        )
        == before + 1.0
    )


def test_unmatched_routes_use_static_label_not_raw_path(client):
    before = _sample(
        "agent_trust_http_requests_total",
        status="404",
        method="GET",
        route="unmatched",
    )
    assert client.get("/totally-not-a-real-path").status_code == 404
    assert (
        _sample(
            "agent_trust_http_requests_total",
            status="404",
            method="GET",
            route="unmatched",
        )
        == before + 1.0
    )


def test_http_duration_histogram_observed(client):
    before = _sample("agent_trust_http_duration_seconds_count")
    assert client.get("/v1/health/live").status_code == 200
    assert _sample("agent_trust_http_duration_seconds_count") == before + 1.0


def test_legacy_service_tone_counter_kept(client):
    before = _sample("agent_trust_requests_total", status="200")
    assert client.get("/v1/health/live").status_code == 200
    assert _sample("agent_trust_requests_total", status="200") == before + 1.0


# --------------------------------------------------------------------------- #
# Access log                                                                  #
# --------------------------------------------------------------------------- #


class _RecordCollector(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_access_log_line_carries_request_metadata_without_client_ip(client):
    collector = _RecordCollector()
    access_logger = logging.getLogger("agent_trust.access")
    access_logger.setLevel(logging.INFO)
    access_logger.addHandler(collector)
    try:
        assert client.get("/v1/health/live").status_code == 200
    finally:
        access_logger.removeHandler(collector)

    record_fields = collector.records[-1].__dict__
    assert record_fields["status"] == "200"
    assert record_fields["method"] == "GET"
    assert record_fields["path"] == "/v1/health/live"
    assert record_fields["duration_ms"] >= 0.0
    assert record_fields["request_id"]
    assert "client_ip" not in record_fields


# --------------------------------------------------------------------------- #
# Cache tier signals                                                          #
# --------------------------------------------------------------------------- #


class _FakeRedis:
    def __init__(self):
        self.data = {}
        self.deleted = []

    def get(self, key):
        return self.data.get(key)

    def delete(self, *keys):
        self.deleted.extend(keys)
        return len(keys)


def _patch_redis(monkeypatch, fake):
    monkeypatch.setattr(
        "app.services.cache_adapter.get_redis_provider",
        lambda: SimpleNamespace(client=fake),
    )


def test_cache_hit_counter(monkeypatch):
    fake = _FakeRedis()
    fake.data["trust:k"] = b'{"value": 1}'
    _patch_redis(monkeypatch, fake)

    from app.services.cache_adapter import CacheAdapter

    before = _sample("agent_trust_cache_hits_total")
    assert CacheAdapter().get("trust:k") == {"value": 1}
    assert _sample("agent_trust_cache_hits_total") == before + 1.0


def test_cache_miss_counter(monkeypatch):
    _patch_redis(monkeypatch, _FakeRedis())

    from app.services.cache_adapter import CacheAdapter

    before = _sample("agent_trust_cache_misses_total")
    assert CacheAdapter().get("trust:missing") is None
    assert _sample("agent_trust_cache_misses_total") == before + 1.0


def test_cache_stale_hit_counter(monkeypatch):
    fake = _FakeRedis()
    fake.data["stale:trust:k"] = b'{"value": 42}'
    _patch_redis(monkeypatch, fake)

    from app.services.cache_adapter import CacheAdapter

    before = _sample("agent_trust_cache_stale_hits_total")
    assert CacheAdapter().get_stale("trust:k") == {"value": 42}
    assert _sample("agent_trust_cache_stale_hits_total") == before + 1.0


def test_cache_invalidation_counter(monkeypatch):
    _patch_redis(monkeypatch, _FakeRedis())

    from app.services.cache_adapter import CacheAdapter

    before = _sample("agent_trust_cache_invalidations_total")
    CacheAdapter().invalidate("trust:k")
    assert _sample("agent_trust_cache_invalidations_total") == before + 1.0


def test_cache_error_counter(monkeypatch):
    class _BrokenRedis:
        def get(self, key):
            raise RuntimeError("redis down")

    _patch_redis(monkeypatch, _BrokenRedis())

    from app.services.cache_adapter import CacheAdapter

    before = _sample("agent_trust_cache_errors_total")
    assert CacheAdapter().get("trust:k") is None
    assert _sample("agent_trust_cache_errors_total") == before + 1.0


# --------------------------------------------------------------------------- #
# Rate limit, webhooks, schema drift                                          #
# --------------------------------------------------------------------------- #


def test_rate_limit_rejection_counter():
    class _Deny:
        def allow(self, key):
            return False

    before = _sample("agent_trust_ratelimit_rejections_total")
    request = SimpleNamespace(client=SimpleNamespace(host="10.0.0.1"))
    principal = SimpleNamespace(service_token="s" * 40)

    with pytest.raises(HTTPException) as exc_info:
        enforce_rate_limit(request=request, principal=principal, identity=None, limiter=_Deny())

    assert exc_info.value.status_code == 429
    assert _sample("agent_trust_ratelimit_rejections_total") == before + 1.0


@pytest.fixture
def webhook_client():
    test_app = app
    test_app.dependency_overrides[enforce_rate_limit] = lambda: MagicMock()
    yield TestClient(test_app)
    test_app.dependency_overrides.clear()


def _post_webhook(webhook_client, secret):
    return webhook_client.post(
        "/v1/webhooks/domain-event",
        json={"agent_id": 1, "event_type": "booking_created"},
        headers={"X-Webhook-Secret": secret},
    )


def test_webhook_invalid_secret_counter(webhook_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "webhook_secret", "known-secret")

    before = _sample("agent_trust_webhooks_total", outcome="invalid_secret")
    assert _post_webhook(webhook_client, "wrong-secret").status_code == 401
    assert _sample("agent_trust_webhooks_total", outcome="invalid_secret") == before + 1.0


def test_webhook_no_secret_configured_counter(webhook_client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "webhook_secret", "")

    before = _sample("agent_trust_webhooks_total", outcome="no_secret_configured")
    assert _post_webhook(webhook_client, "anything").status_code == 503
    assert (
        _sample("agent_trust_webhooks_total", outcome="no_secret_configured") == before + 1.0
    )


def test_webhook_received_counter(webhook_client, monkeypatch):

    class _StubCache:
        def invalidate(self, key):
            return None

    monkeypatch.setattr("app.api.v1.endpoints.webhooks.CacheAdapter", _StubCache)
    settings = get_settings()
    monkeypatch.setattr(settings, "webhook_secret", "known-secret")

    before = _sample("agent_trust_webhooks_total", outcome="received")
    assert _post_webhook(webhook_client, "known-secret").status_code == 200
    assert _sample("agent_trust_webhooks_total", outcome="received") == before + 1.0


def test_schema_drift_counter_increments_on_missing():
    class _FakeNoColumns:
        def execute(self, *args, **kwargs):
            return SimpleNamespace(scalar=lambda: 0)

    before = _sample("agent_trust_schema_drift_total")
    assert validate_schema(_FakeNoColumns())
    assert _sample("agent_trust_schema_drift_total") == before + 1.0


# --------------------------------------------------------------------------- #
# Scoring latency reported in seconds (unit fix)                              #
# --------------------------------------------------------------------------- #


class _RecordingHistogram:
    def __init__(self):
        self.observations = []

    def observe(self, value):
        self.observations.append(value)


@patch("app.services.agent_trust_scorer.CacheAdapter")
@patch("app.services.agent_trust_scorer.AuditRepository")
@patch("app.services.agent_trust_scorer.TrustModelPredictor")
@patch("app.services.agent_trust_scorer.AgentRepository")
@patch("app.services.agent_trust_scorer.AGENT_TRUST_DURATION")
def test_scoring_duration_observed_in_seconds(
    mock_duration, mock_repo_cls, mock_ml_cls, mock_audit_cls, mock_cache_cls
):
    mock_cache = MagicMock()
    mock_cache_cls.return_value = mock_cache
    mock_cache.get.return_value = None
    mock_cache.acquire_lock.return_value = "lock-token-abc"

    mock_repo = MagicMock()
    mock_repo_cls.return_value = mock_repo
    mock_repo.get_agent.return_value = (1, "Test Agent")
    mock_repo.get_credit_stats.return_value = SimpleNamespace(
        current_overdue_count=0,
        current_overdue_ratio=0.0,
        current_max_delay_days=0,
        outstanding_amount=0.0,
        historical_late_payment_count=0,
        historical_late_payment_ratio=0.0,
        average_payment_delay_days=0.0,
        maximum_payment_delay_days=0,
        consecutive_unpaid_cycles=0,
    )
    mock_repo.get_multi_timeframe_stats.return_value = _cached_stats()
    mock_repo.get_experience_stats.return_value = {
        "created_at": datetime.now(timezone.utc),
        "lifetime_bookings": 0,
        "lifetime_revenue": 0.0,
        "lifetime_cancelled": 0,
    }
    mock_repo.get_agent_search_activity.return_value = {
        "searches": 0,
        "created": 0,
        "reused": 0,
        "bookings": 0,
    }
    mock_repo.get_supplier_l2b_targets.return_value = []
    mock_repo.get_agent_supplier_searches.return_value = []
    mock_repo.get_agent_booking_counts_by_provider.return_value = []

    mock_ml = MagicMock()
    mock_ml_cls.return_value = mock_ml
    mock_ml.predict.return_value = 80.0

    result = AgentTrustScorer().calculate(db=MagicMock(), agent_id=1)
    assert result.agent_id == 1

    assert len(mock_duration.observe.call_args_list) == 1
    observed = mock_duration.observe.call_args_list[0].args[0]
    # Seconds, not the raw millisecond figure: a real sub-10s score must never
    # land at a value that implies seconds-bucket semantics were violated.
    assert 0.0 < observed < 10.0