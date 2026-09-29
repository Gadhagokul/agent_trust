import os
from pathlib import Path

from dotenv import dotenv_values

_ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
_ENV = dotenv_values(_ENV_FILE) if _ENV_FILE.exists() else {}

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("LARAVEL_SERVICE_TOKEN", "test-service-token-0123456789abcdef")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

# The application is a strictly read-only consumer of the external database
# (no INSERT/UPDATE/DELETE/commit anywhere in app/), so pointing tests at the
# real credentials is safe. These are assigned -- not setdefault -- because a
# stray shell variable would otherwise silently win over .env, and pydantic
# settings give environment variables priority over the .env file.
os.environ["DB_DATABASE"] = _ENV.get("DB_DATABASE", "dummy_afine")
os.environ["DB_USERNAME"] = _ENV.get("DB_USERNAME", "root")
os.environ["DB_PASSWORD"] = _ENV.get("DB_PASSWORD", "")

# The integration fixture builds its own client against Redis DB 15 directly
# rather than overriding REDIS_URL: get_settings() and get_redis_provider() are
# lru_cache singletons that resolve on first call, so a late env override here
# would be silently ignored. DB 0 is left untouched.
os.environ.setdefault("REDIS_URL", _ENV.get("REDIS_URL", "redis://127.0.0.1:6379/0"))

import pytest  # noqa: E402
import redis as redis_lib  # noqa: E402
from sqlalchemy import text  # noqa: E402

# Sentinel id used to tag rows the integration tests create, so teardown can
# remove exactly what the test added and nothing else.
SENTINEL_USER_ID = -999_001
SENTINEL_AGENT_ID = -999_001
INTEGRATION_REDIS_DB = 15

# Every table the application reads. Used by the row-count safety net.
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


def _require_integration_db():
    """Skip the calling test when the live database is unreachable."""
    pytest.importorskip("pymysql")


@pytest.fixture(scope="session")
def integration_settings():
    from app.infra.settings import get_settings

    return get_settings()


@pytest.fixture(scope="session")
def integration_db(integration_settings):
    """A real SQLAlchemy session against the live external database."""
    _require_integration_db()

    from app.infra.db.session import engine

    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Live MySQL unreachable, skipping integration test: {exc}")

    with engine.connect() as conn:
        conn.execute(text("SELECT 1"))

    from sqlalchemy.orm import Session

    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def integration_redis():
    """A real Redis client on an isolated database number (never DB 0)."""
    base = os.environ.get("REDIS_URL", "redis://127.0.0.1:6379/0").rsplit("/", 1)[0]
    url = f"{base}/{INTEGRATION_REDIS_DB}"

    try:
        client = redis_lib.from_url(url, decode_responses=True, socket_connect_timeout=2)
        client.ping()
    except Exception as exc:  # pragma: no cover - environment dependent
        pytest.skip(f"Redis unreachable on DB {INTEGRATION_REDIS_DB}, skipping: {exc}")

    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


@pytest.fixture
def tmp_audit_path(tmp_path, monkeypatch):
    """Redirect the JSONL audit log away from the real logs/ directory.

    audit_repository reads settings.audit_log_path inside the function, not at
    import time, so a runtime patch takes effect for every subsequent write.
    """
    from app.infra.settings import get_settings

    target = tmp_path / "logs" / "agent_score_audits.log"
    target.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(get_settings(), "audit_log_path", str(target))
    return target


@pytest.fixture
def synthetic_rows(integration_db):
    """Insert boundary-case rows tagged with a sentinel id, then remove them."""
    created: list[tuple[str, int]] = []

    def _insert(table: str, sentinel_id: int, columns: dict) -> None:
        assert table in _MONITORED_TABLES, f"refusing to write to unmonitored table {table}"
        col_sql = ", ".join(f"`{col}`" for col in columns)
        placeholders = ", ".join(["%s"] * len(columns))
        integration_db.execute(
            text(f"INSERT INTO `{table}` ({col_sql}) VALUES ({placeholders})"),
            tuple(columns.values()),
        )
        integration_db.commit()
        created.append((table, sentinel_id))

    try:
        yield _insert
    finally:
        for table, sentinel_id in reversed(created):
            key = "agent_id" if table in ("bookings", "credit_transactions") else "user_id"
            integration_db.execute(
                text(f"DELETE FROM `{table}` WHERE `{key}` = :sid"),
                {"sid": sentinel_id},
            )
        integration_db.commit()
