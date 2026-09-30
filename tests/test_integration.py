"""
Integration tests: real SQL against the live external MySQL database.

Unit tests mock the database entirely and therefore cannot catch the failure
mode that matters most here -- a query that runs but returns subtly wrong
numbers (a join that multiplies rows, inverted NULL handling, an off-by-one at
the overdue boundary). Those bugs produce plausible scores and never raise.

Run with:  pytest -m integration
Skipped by default (see addopts in pyproject.toml).

Safety properties, enforced rather than assumed:
  * The application is read-only against MySQL -- there is no INSERT/UPDATE/
    DELETE/commit anywhere in app/ -- so these tests can only read.
  * The only rows created are tagged with a sentinel id and removed in
    teardown; a module-scoped autouse fixture fails the run on any row-count
    delta across all monitored tables.
  * Redis runs on database 15, never 0.
  * The JSONL audit log is redirected to a temp file.
"""

import time
from datetime import date, timedelta

import pytest

from app.infra.db.repository import AgentRepository
from app.infra.db.schema_guard import REQUIRED_SCHEMA, validate_schema
from app.services.cache_adapter import CacheAdapter

pytestmark = pytest.mark.integration

# Sentinel id used to tag rows the integration tests create, so teardown can
# remove exactly what the test added and nothing else. It must be a valid
# unsigned bigint: agents/credit_transactions use bigint UNSIGNED, and the live
# database tops out at agent id 1000, so this cannot collide with real data.
SENTINEL_AGENT_ID = 999_000_001

_MONITORED_TABLES = (
    "agents",
    "users",
    "bookings",
    "booking_processes",
    "search_sessions",
    "search_session_accesses",
    "search_supplier_runs",
    "suppliers",
    "credit_transactions",
)


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def _row_counts(db) -> dict[str, int]:
    from sqlalchemy import text

    counts = {}
    for table in _MONITORED_TABLES:
        counts[table] = int(db.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar())
    return counts


def _pick_agent_with_activity(integration_db) -> int:
    """An agent that actually has completed bookings, so queries return real rows."""
    from sqlalchemy import text

    row = integration_db.execute(
        text(
            """
            SELECT agent_id FROM bookings
            WHERE status NOT IN ('pending', 'failed', 'cancelled', 'rejected')
            GROUP BY agent_id
            ORDER BY COUNT(*) DESC
            LIMIT 1
            """
        )
    ).first()
    if row is None:
        pytest.skip("No agent with completed bookings in the live database")
    return int(row[0])


def _redis_provider_on(client):
    """
    Minimal RedisProvider bound to the integration client.

    CacheAdapter reads the client off the provider singleton, so this reuses the
    real class and swaps only the client, keeping the tested code path identical
    to production.
    """
    from app.infra.redis_provider import RedisProvider

    provider = RedisProvider.__new__(RedisProvider)
    provider.client = client
    return provider


class _Result:
    """Stand-in for a SQLAlchemy Result exposing only .scalar()."""

    def __init__(self, value: int) -> None:
        self._value = value

    def scalar(self) -> int:
        return self._value


class _ExplodingRedis:
    """Stands in for a Redis client that raises on every operation."""

    def get(self, *a, **k):
        raise RuntimeError("redis down")

    def setex(self, *a, **k):
        raise RuntimeError("redis down")

    def delete(self, *a, **k):
        raise RuntimeError("redis down")

    def set(self, *a, **k):
        raise RuntimeError("redis down")

    def register_script(self, *a, **k):
        raise RuntimeError("redis down")


# --------------------------------------------------------------------------- #
# Safety net                                                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def row_count_baseline(integration_db):
    """Snapshot table sizes before this module runs."""
    return _row_counts(integration_db)


@pytest.fixture(scope="module", autouse=True)
def assert_database_untouched(integration_db, row_count_baseline):
    """
    Fail the run if this module changed any monitored table.

    The application never writes to MySQL, so the only legitimate delta is
    zero. This converts "the tests do not touch your data" from a claim into a
    mechanical check that fails loudly if it ever stops being true.
    """
    yield
    after = _row_counts(integration_db)
    drifted = {
        t: (row_count_baseline[t], after[t])
        for t in after
        if after[t] != row_count_baseline[t]
    }
    assert not drifted, f"Integration tests mutated the database: {drifted}"


@pytest.fixture
def synthetic_rows(integration_db):
    """
    Insert rows tagged with the sentinel id, then delete them in teardown.

    Only used to fabricate an exact boundary case (e.g. a credit transaction
    due today). The application itself never writes to MySQL.
    """
    from sqlalchemy import text

    created: list[tuple[str, str]] = []

    def _insert(table: str, key_col: str, rows: list[dict]) -> None:
        if not rows:
            return
        cols = list(rows[0].keys())
        col_sql = ", ".join(f"`{c}`" for c in cols)
        marks = ", ".join([f":{c}" for c in cols])
        # A list of dicts lets each row bind its own named params. A list of
        # tuples makes SQLAlchemy sort them, which fails on mixed types.
        integration_db.execute(
            text(f"INSERT INTO `{table}` ({col_sql}) VALUES ({marks})"), rows
        )
        integration_db.commit()
        created.append((table, key_col))

    try:
        yield _insert
    finally:
        for table, key_col in reversed(created):
            integration_db.execute(
                text(f"DELETE FROM `{table}` WHERE `{key_col}` = :sid"),
                {"sid": SENTINEL_AGENT_ID},
            )
        integration_db.commit()


# --------------------------------------------------------------------------- #
# Schema guard                                                                 #
# --------------------------------------------------------------------------- #


def test_validate_schema_reports_no_drift(integration_db):
    """
    The live schema still satisfies every column the code queries.

    This pins the 11-table / 0-mismatch result as a regression guard: if Laravel
    changes a column, this fails here rather than in production.
    """
    missing = validate_schema(integration_db)
    assert missing == {}, f"Schema drift detected: {missing}"


def test_required_schema_tables_all_exist(integration_db):
    """Every declared table is present in information_schema."""
    from sqlalchemy import text

    rows = integration_db.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE()")
    ).fetchall()
    live = {r[0] for r in rows}

    absent = [t for t in REQUIRED_SCHEMA if t not in live]
    assert not absent, f"Required tables missing from live schema: {absent}"


def test_validate_schema_detects_missing_column_without_ddl():
    """
    Drift detection is proven with a stub session, not by dropping a column.

    Running destructive DDL against the shared database is unsafe: a test
    failing midway could leave the schema half-migrated. This verifies the
    detection logic with zero effect on any database.

    The stub mirrors the real call protocol: validate_schema issues one
    COUNT(*) per column and reads .scalar(), returning 1 when present.
    """
    from unittest.mock import MagicMock

    from app.infra.db import schema_guard

    dropped = ("credit_transactions", "due_date")

    def _execute(_sql, params):
        return _Result(0 if (params["table"], params["col"]) == dropped else 1)

    db = MagicMock()
    db.execute.side_effect = _execute

    missing = schema_guard.validate_schema(db)

    assert missing == {"credit_transactions": ["due_date"]}, (
        "exactly the dropped column must be reported, nothing else"
    )


def test_validate_schema_reports_clean_when_nothing_missing():
    """The stub path must also prove a healthy schema yields an empty dict."""
    from unittest.mock import MagicMock

    from app.infra.db import schema_guard

    db = MagicMock()
    db.execute.return_value.scalar.return_value = 1

    assert schema_guard.validate_schema(db) == {}


def test_validate_schema_reports_missing_columns_when_query_fails():
    """
    A failing probe is treated as drift, not as a pass.

    Failing open here would report a healthy schema while the database is
    unreachable, which is the dangerous direction for a startup guard.
    """
    from unittest.mock import MagicMock

    from app.infra.db import schema_guard

    db = MagicMock()
    db.execute.side_effect = RuntimeError("connection lost")

    missing = schema_guard.validate_schema(db)

    assert set(missing) == set(REQUIRED_SCHEMA), "every table must be flagged"
    assert all(missing.values()), "each flagged table must list its columns"


# --------------------------------------------------------------------------- #
# Repository reads -- the methods the scoring path actually calls               #
# --------------------------------------------------------------------------- #


def test_get_agent_returns_identity(integration_db):
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    agent = repo.get_agent(integration_db, agent_id)

    assert isinstance(agent, tuple), "get_agent must return a (id, name) tuple"
    assert agent[0] == agent_id
    assert isinstance(agent[1], str)


def test_get_agent_unknown_id_raises_domain_error(integration_db):
    """A missing agent surfaces as a domain error, not an unhandled SQL error."""
    from app.domain.errors import AgentNotFoundError

    repo = AgentRepository()

    with pytest.raises(AgentNotFoundError):
        repo.get_agent(integration_db, -987_654_321)


def test_get_experience_stats_shape(integration_db):
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    stats = repo.get_experience_stats(integration_db, agent_id)

    assert set(stats) == {
        "created_at",
        "lifetime_bookings",
        "lifetime_revenue",
        "lifetime_cancelled",
    }
    assert stats["lifetime_bookings"] >= 0
    assert stats["lifetime_cancelled"] >= 0
    assert stats["lifetime_revenue"] >= 0.0


def test_get_credit_stats_all_fields_present(integration_db):
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    stats = repo.get_credit_stats(integration_db, agent_id)

    for field in (
        "current_overdue_count",
        "current_overdue_ratio",
        "current_max_delay_days",
        "outstanding_amount",
        "historical_late_payment_count",
        "historical_late_payment_ratio",
        "average_payment_delay_days",
        "maximum_payment_delay_days",
        "consecutive_unpaid_cycles",
    ):
        assert hasattr(stats, field), f"CreditStats missing field: {field}"

    assert stats.current_overdue_count >= 0
    assert 0.0 <= stats.current_overdue_ratio <= 100.0
    assert 0.0 <= stats.historical_late_payment_ratio <= 100.0
    assert stats.current_max_delay_days >= 0


def test_get_multi_timeframe_stats_shape_and_monotonicity(integration_db):
    """
    All four windows are present and nested counts never decrease as the window
    widens -- a wider window can only include more rows, so a violation would
    indicate a broken window filter or a join that drops rows.
    """
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    windows = repo.get_multi_timeframe_stats(integration_db, agent_id)

    assert set(windows) == {1, 7, 30, 365}

    for days, window in windows.items():
        for key in (
            "searches",
            "bookstep_failed",
            "adjusted_bookstep_failed",
            "other_step_failed",
            "effective_searches",
            "no_activity",
            "bookings",
            "booking_volume",
            "avg_booking_value",
            "revenue_consistency",
            "low_confidence",
        ):
            assert key in window, f"window {days}d missing key: {key}"

        assert window["searches"] >= 0
        assert window["bookstep_failed"] >= 0
        assert window["effective_searches"] >= 0
        assert window["effective_searches"] <= window["searches"]
        assert window["no_activity"] == (window["effective_searches"] == 0)

        # adjusted_bookstep_failed is a search-volume cap: it can never exceed
        # 70% of searches, nor the raw failure count it is derived from.
        assert window["adjusted_bookstep_failed"] <= int(window["searches"] * 0.7)
        assert window["adjusted_bookstep_failed"] <= window["bookstep_failed"]

    for narrow, wide in ((1, 7), (7, 30), (30, 365)):
        assert windows[narrow]["searches"] <= windows[wide]["searches"], (
            f"search count fell from {narrow}d to {wide}d, window filter inconsistent"
        )
        assert windows[narrow]["bookstep_failed"] <= windows[wide]["bookstep_failed"], (
            f"failure count fell from {narrow}d to {wide}d, window filter inconsistent"
        )


def test_get_multi_timeframe_zero_activity_agent(integration_db):
    """
    An agent id with no history must return well-formed empty windows, not None
    or a crash -- this is the path that marks a component 'unavailable'.
    """
    repo = AgentRepository()

    windows = repo.get_multi_timeframe_stats(integration_db, -987_654_321)

    assert set(windows) == {1, 7, 30, 365}
    for window in windows.values():
        assert window["searches"] == 0
        assert window["bookstep_failed"] == 0
        assert window["effective_searches"] == 0
        assert window["no_activity"] is True
        assert window["low_confidence"] is True


def test_scoring_path_supplier_queries_execute(integration_db):
    """
    The supplier queries the scoring path actually calls (scorer.py:600-601) must
    run against the live schema. These decide the search-to-booking component.
    """
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    targets = repo.get_supplier_l2b_targets(integration_db)
    assert isinstance(targets, list)
    assert targets, "supplier targets must not be empty"
    for row in targets:
        assert {"code", "name", "target"} <= set(row), f"unexpected target shape: {row.keys()}"
        assert isinstance(row["target"], float)
        assert 0.0 < row["target"] <= 1.0

    searches = repo.get_agent_supplier_searches(integration_db, agent_id, days=365)
    assert isinstance(searches, list)
    for row in searches:
        assert isinstance(row, dict)

    activity = repo.get_agent_search_activity(integration_db, agent_id)
    assert isinstance(activity, dict)


def test_agent_booking_counts_by_provider_uses_raw_provider_values(integration_db):
    """
    bookings.provider holds GDS codes (GF/SABRE/AMADEUS), NOT supplier names.

    This pins that the L2B component aggregates by the raw provider value, so a
    future 'helpful' refactor to join suppliers on provider cannot silently
    rewrite the scoring semantics into an empty join.
    """
    repo = AgentRepository()
    agent_id = _pick_agent_with_activity(integration_db)

    rows = repo.get_agent_booking_counts_by_provider(integration_db, agent_id, days=365)

    assert isinstance(rows, list)
    for row in rows:
        assert set(row) == {"provider", "bookings"}
        assert isinstance(row["provider"], str)
        assert row["bookings"] > 0

    providers = {r["provider"] for r in rows}
    supplier_names = {
        r["name"] for r in repo.get_supplier_l2b_targets(integration_db) if r.get("name")
    }
    assert not (providers & supplier_names), (
        "bookings.provider is matching supplier names, which is the wrong data link"
    )


def test_dead_l2b_snapshot_query_is_broken_against_live_schema(integration_db):
    """
    KNOWN DEFECT (pre-existing, not a regression): the query inside
    get_agent_supplier_l2b_snapshot joins bookings.supplier_id, which does not
    exist in the real schema -- bookings has no supplier_id column at all.

    Nothing in app/ calls this method, so it is unreachable dead code today.
    This test documents the defect so it cannot be revived unnoticed. It is
    expected to FAIL until the query is either fixed or deleted.
    """
    repo = AgentRepository()

    with pytest.raises(Exception) as exc_info:
        repo.get_agent_supplier_l2b_snapshot(integration_db, 944)

    assert "supplier_id" in str(exc_info.value), (
        "expected the missing-column error to name supplier_id"
    )


def test_get_label_evidence_returns_structured_rows(integration_db):
    """The ML label query must execute against the real schema."""
    repo = AgentRepository()

    evidence = repo.get_label_evidence(
        integration_db,
        target="severe_reliability",
        cut_off=date.today() - timedelta(days=365),
        horizon_days=90,
    )
    assert isinstance(evidence, dict)
    for key, row in evidence.items():
        assert isinstance(key, int)
        assert isinstance(row, dict)


# --------------------------------------------------------------------------- #
# Financial boundary: < vs <= on due-today                                    #
# --------------------------------------------------------------------------- #


def _credit_row(due: date) -> dict:
    return {
        "agent_id": SENTINEL_AGENT_ID,
        "principal_amount": 100.0,
        "total_payable": 100.0,
        "service_fee": 0.0,
        "paid_amount": 0.0,
        "credit_days": 30,
        "due_date": due,
        "status": "unpaid",
    }


def test_due_today_boundary_respects_operator(
    integration_db, synthetic_rows, monkeypatch
):
    """
    With credit_overdue_boundary='<', a transaction due today is NOT overdue;
    one due yesterday IS. With '<=', both are. This off-by-one silently
    misclassifies agents, so both sides of the boundary are pinned.
    """
    from app.infra.settings import get_settings

    today = date.today()
    synthetic_rows(
        "credit_transactions",
        "agent_id",
        [_credit_row(today), _credit_row(today - timedelta(days=1))],
    )

    repo = AgentRepository()

    monkeypatch.setattr(get_settings(), "credit_overdue_boundary", "<")
    strict = repo.get_credit_stats(integration_db, SENTINEL_AGENT_ID)
    assert strict.current_overdue_count == 1, (
        "only the past-due transaction counts when boundary is '<'"
    )

    monkeypatch.setattr(get_settings(), "credit_overdue_boundary", "<=")
    inclusive = repo.get_credit_stats(integration_db, SENTINEL_AGENT_ID)
    assert inclusive.current_overdue_count == 2, (
        "the due-today transaction also counts when boundary is '<='"
    )

    assert strict.outstanding_amount == 100.0
    assert inclusive.outstanding_amount == 200.0


# --------------------------------------------------------------------------- #
# Redis: real server, database 15 only                                         #
# --------------------------------------------------------------------------- #


def test_cache_roundtrip_and_ttl(integration_redis, monkeypatch):
    """Primary and stale tiers are both written, and the primary expires first."""
    monkeypatch.setattr(
        "app.services.cache_adapter.get_redis_provider",
        lambda: _redis_provider_on(integration_redis),
    )

    cache = CacheAdapter()
    cache.set("trust:agent:1:conversion", {"score": 88}, ttl=1)

    assert cache.get("trust:agent:1:conversion") == {"score": 88}
    assert integration_redis.exists("trust:agent:1:conversion") == 1
    assert integration_redis.exists("stale:trust:agent:1:conversion") == 1
    assert integration_redis.ttl("trust:agent:1:conversion") == 1

    time.sleep(1.2)
    assert cache.get("trust:agent:1:conversion") is None, "primary must expire after its TTL"
    assert cache.get_stale("trust:agent:1:conversion") == {"score": 88}, "stale tier must survive"


def test_cache_invalidate_clears_both_tiers(integration_redis, monkeypatch):
    """
    A4: invalidate() deletes BOTH the primary key AND the 24h stale copy.

    A domain-event webhook declares the agent's state changed, so serving the
    stale fallback after an invalidation would hand out a superseded score
    during a later DB outage.
    """
    monkeypatch.setattr(
        "app.services.cache_adapter.get_redis_provider",
        lambda: _redis_provider_on(integration_redis),
    )

    cache = CacheAdapter()
    cache.set("trust:agent:2:conversion", {"score": 70})
    cache.invalidate("trust:agent:2:conversion")

    assert cache.get("trust:agent:2:conversion") is None, "primary must be cleared"
    assert integration_redis.exists("stale:trust:agent:2:conversion") == 0, (
        "A4: the stale copy must be cleared by invalidation too"
    )


def test_cache_lock_is_exclusive_and_lua_owned(integration_redis, monkeypatch):
    """Only one caller wins the lock, and only the owner can release it."""
    monkeypatch.setattr(
        "app.services.cache_adapter.get_redis_provider",
        lambda: _redis_provider_on(integration_redis),
    )

    cache = CacheAdapter()

    token = cache.acquire_lock("agent:3")
    assert token is not None, "first caller must acquire the lock"
    assert cache.acquire_lock("agent:3") is None, "second caller must be refused while locked"

    cache.release_lock("agent:3", "not-the-real-token")
    assert cache.acquire_lock("agent:3") is None, "a forged release must not free the lock"

    cache.release_lock("agent:3", token)
    assert cache.acquire_lock("agent:3") is not None, "the owner must be able to release it"


def test_cache_errors_never_raise(integration_redis, monkeypatch):
    """A broken Redis degrades the cache, it does not fail the request."""
    monkeypatch.setattr(
        "app.services.cache_adapter.get_redis_provider",
        lambda: _redis_provider_on(integration_redis),
    )

    cache = CacheAdapter()
    cache.redis = _ExplodingRedis()

    assert cache.get("any") is None
    assert cache.get_stale("any") is None
    cache.set("any", {"a": 1})
    cache.invalidate("any")
    assert cache.acquire_lock("any") is None


# --------------------------------------------------------------------------- #
# Audit log redirection                                                         #
# --------------------------------------------------------------------------- #


def test_audit_writes_go_to_temp_path(integration_db, tmp_audit_path):
    """The real logs/agent_score_audits.log must not be touched by tests."""
    from app.infra.db.audit_repository import AuditRepository

    assert not tmp_audit_path.exists()

    AuditRepository.append_audit_log(
        agent_id=424_242,
        old_score=50.0,
        new_score=77,
        old_tier="Silver",
        new_tier="Gold",
        metadata={"source": "integration_test"},
    )

    assert tmp_audit_path.exists(), "audit entry must land in the temp file"
    content = tmp_audit_path.read_text(encoding="utf-8")
    assert "424242" in content
    assert "integration_test" in content
