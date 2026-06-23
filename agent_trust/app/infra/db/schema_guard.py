# app/infra/db/schema_guard.py
"""
Schema Guard — validates that the external (read-only) database still has
the tables and columns our queries depend on.

Run at health-check time or on-startup to detect schema drift early,
before it causes a crash in production requests.
"""
import logging

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.domain.errors import SchemaChangedError

logger = logging.getLogger(__name__)

# Declare exactly which columns we query — update here if the query changes
REQUIRED_SCHEMA: dict[str, list[str]] = {
    "agents": [
        "id",
        "establishment_name",
    ],
    "credit_transactions": [
        "agent_id",
        "payment_date",
        "due_date",
        "principal_amount",
        "status",
        "credit_days",   # Used to scope each transaction to its own evaluation window
    ],
    "bookings": [
        "agent_id",
        "status",
        "total_amount",
        "booking_date",
    ],
    "search_sessions": [
        "agent_id",
        "status",
        "created_at",
    ],
    "booking_process": [
        "user_id",
        "current_step",
        "state",
        "created_at",  # Required for timeframe filtering in BookStep failure count
    ],
}


def validate_schema(db: Session) -> dict[str, list[str]]:
    """
    Check that all required tables and columns exist in the external DB.

    Returns a dict of any missing items:
        {"agents": ["establishment_name"], "credit_transactions": ["due_date"]}

    Returns an empty dict if everything is present.
    """
    missing: dict[str, list[str]] = {}

    for table, columns in REQUIRED_SCHEMA.items():
        for col in columns:
            try:
                result = db.execute(
                    text("""
                        SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                        WHERE TABLE_NAME   = :table
                          AND COLUMN_NAME  = :col
                    """),
                    {"table": table, "col": col},
                ).scalar()

                if not result:
                    missing.setdefault(table, []).append(col)
                    logger.warning(
                        "Schema drift detected: column '%s.%s' is missing in external DB",
                        table, col,
                    )
            except Exception as exc:
                logger.error("Schema guard query failed for %s.%s: %s", table, col, exc)
                missing.setdefault(table, []).append(col)

    return missing


def assert_schema_ok(db: Session) -> None:
    """
    Like validate_schema() but raises SchemaChangedError if anything is missing.
    Use this at the start of a request to fail fast with a clear 503.
    """
    missing = validate_schema(db)
    if missing:
        details = ", ".join(
            f"{tbl}.{col}" for tbl, cols in missing.items() for col in cols
        )
        raise SchemaChangedError(
            f"External DB schema changed. Missing: [{details}]. "
            "Contact the source team or check REQUIRED_SCHEMA in schema_guard.py."
        )
