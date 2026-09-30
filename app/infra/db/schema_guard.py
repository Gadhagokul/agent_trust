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

from app.observability.metrics import SCHEMA_DRIFT

logger = logging.getLogger(__name__)

# Declare exactly which columns we query — update here if the query changes
REQUIRED_SCHEMA: dict[str, list[str]] = {
    "agents": [
        "id",
        "establishment_name",
        "user_id",  # Identity: users -> agents mapping
        "is_active",
        "approval_status",
        "created_at",  # Account age for the experience component
        "email",  # Admin trust-score search (search_agents)
    ],
    "users": [
        "id",
        "is_active",
    ],
    "roles": [
        "id",
        "name",
    ],
    "model_has_roles": [
        "role_id",
        "model_id",
        "model_type",
    ],
    "credit_transactions": [
        "agent_id",
        "payment_date",
        "due_date",
        "principal_amount",
        "status",
        "credit_days",  # Used to scope each transaction to its own evaluation window
    ],
    "bookings": [
        "agent_id",
        "provider",  # supplier attribution for L2B: equals suppliers.name
        "status",
        "total_amount",
        "created_at",
    ],
    "search_sessions": [
        "user_id",
        "status",
        "created_at",
    ],
    "booking_processes": [
        "user_id",
        "current_step",
        "state",
        "created_at",  # Required for timeframe filtering in BookStep failure count
    ],
    # Supplier quota (Feature A) + agent search-to-booking (Feature B)
    "suppliers": [
        "id",
        "code",
        "name",
        "is_active",
        "health_status",
        "search_limit",  # site-wide quota limit (created-only supplier requests)
        "minimum_booking",  # derives the search-to-booking target ratio for scoring
    ],
    "search_supplier_runs": [
        "search_session_id",
        "supplier_id",
        "supplier_code",
        "status",
        "created_at",
    ],
    "search_session_accesses": [
        "search_session_id",
        "agent_id",
        "first_access_type",  # 'created' -> real supplier request; 'reused' -> cache hit
        "first_accessed_at",
        "access_count",  # Search volume (SUM) for supplier-specific L2B
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
                        table,
                        col,
                    )
            except Exception as exc:
                logger.error("Schema guard query failed for %s.%s: %s", table, col, exc)
                missing.setdefault(table, []).append(col)

    if missing:
        SCHEMA_DRIFT.inc()

    return missing
