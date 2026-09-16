# app/infra/db/repository.py
import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.errors import AgentNotFoundError, DatabaseUnavailableError, SchemaChangedError
from app.infra.settings import get_settings

logger = logging.getLogger(__name__)


def _bind_placeholders(prefix: str, values: list[str]) -> tuple[str, dict]:
    """Builds `(:p0, :p1, ...)` placeholders + a params dict for an IN list."""
    params = {f"{prefix}{i}": v for i, v in enumerate(values)}
    placeholders = ", ".join(f":{k}" for k in params)
    return f"({placeholders})", params


def _window_sql(period_type: str, days: int) -> tuple[str, dict]:
    """
    Returns (sql_predicate, params) for a trailing window boundary.

    - lifetime : no date filter (quota counted for all history)
    - daily    : from today 00:00 (CURDATE())
    - monthly  : rolling last 30 days (same trailing-window semantics as the
                 1/7/30/365-day trust stats, NOT a calendar month)
    - rolling  : floating window of `days` back from NOW()
    Raises ValueError for an unsupported period_type.
    """
    if period_type == "lifetime":
        return "TRUE", {}
    if period_type == "daily":
        return "CURDATE()", {}
    if period_type == "monthly":
        return "DATE_SUB(CURDATE(), INTERVAL 30 DAY)", {}
    if period_type == "rolling":
        if days < 1:
            raise ValueError("supplier_quota_period_days must be at least 1")
        return "DATE_SUB(NOW(), INTERVAL :rolling_days DAY)", {"rolling_days": days}
    raise ValueError(f"Unsupported period_type: {period_type!r}")


@dataclass(frozen=True)
class CreditStats:
    """Separates current financial risk from historical payment behavior."""

    # Current financial risk (what the agent owes NOW)
    current_overdue_count: int
    current_overdue_ratio: float
    current_max_delay_days: int
    outstanding_amount: float

    # Historical payment behavior (how the agent has paid BEFORE)
    historical_late_payment_count: int
    historical_late_payment_ratio: float
    average_payment_delay_days: float
    consecutive_unpaid_cycles: int


class AgentRepository:
    """
    Repository for credit-based trust score queries against the external (read-only) DB.

    All SQL calls are wrapped with error handling so that:
      - OperationalError  → DatabaseUnavailableError (DB is down / table missing)
      - ProgrammingError  → SchemaChangedError       (column renamed or dropped)
      - Any other error   → DatabaseUnavailableError  (safe catch-all)
    """

    def get_agent(self, db: Session, agent_id: int) -> tuple[int, str]:
        """Get basic agent info."""
        try:
            result = db.execute(
                text("""
                    SELECT id, establishment_name
                    FROM agents
                    WHERE id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'agents' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'agents' no longer matches expected schema. "
                "External DB may have been updated."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying 'agents': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'agents': %s", exc)
            raise DatabaseUnavailableError() from exc

        if not result:
            raise AgentNotFoundError(f"Agent {agent_id} not found")

        return result[0], result[1]

    def get_user_role(self, db: Session, user_id: int, model_type: str) -> list[str]:
        """
        Resolve the Laravel role names for a user via
        users -> model_has_roles -> roles.
        """
        try:
            rows = db.execute(
                text("""
                    SELECT r.name
                    FROM roles r
                    JOIN model_has_roles m ON m.role_id = r.id
                    WHERE m.model_id = :user_id
                      AND m.model_type = :model_type
                """),
                {"user_id": user_id, "model_type": model_type},
            ).fetchall()
        except ProgrammingError as exc:
            logger.error("Schema change detected in role tables: %s", exc)
            raise SchemaChangedError(
                "'roles' or 'model_has_roles' no longer matches expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying roles: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying roles: %s", exc)
            raise DatabaseUnavailableError() from exc

        return [str(row[0]) for row in rows if row[0]]

    def get_agent_for_user(self, db: Session, user_id: int) -> dict | None:
        """
        Resolution of a user to their linked (preferred active/approved) agent
        via agents.user_id. Read-only.
        """
        try:
            row = db.execute(
                text("""
                    SELECT id, is_active, approval_status
                    FROM agents
                    WHERE user_id = :user_id
                    ORDER BY
                        (approval_status = 'approved') DESC,
                        is_active DESC,
                        id ASC
                    LIMIT 1
                """),
                {"user_id": user_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'agents' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'agents' no longer matches expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying 'agents': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'agents': %s", exc)
            raise DatabaseUnavailableError() from exc

        if row is None:
            return None

        return {
            "id": int(row[0]),
            "is_active": int(row[1] or 0) == 1,
            "approval_status": row[2],
        }

    def search_agents(
        self,
        db: Session,
        q: str | None,
        page: int,
        page_size: int,
    ) -> tuple[list[dict], int]:
        """
        Paginated, read-only agent search used by the admin trust-score list.

        Filters on establishment_name / email (LIKE) or an exact agent id.
        Returns (rows, total_count).
        """
        where_sql = ""
        params: dict = {}
        if q:
            where_sql = (
                "WHERE establishment_name LIKE :q OR email LIKE :q OR id = CAST(:exact AS UNSIGNED)"
            )
            params["q"] = f"%{q}%"
            params["exact"] = q

        try:
            total = (
                db.execute(text(f"SELECT COUNT(*) FROM agents {where_sql}"), params).scalar() or 0
            )
            rows = db.execute(
                text(
                    f"""
                    SELECT id, establishment_name, email
                    FROM agents
                    {where_sql}
                    ORDER BY establishment_name ASC
                    LIMIT :limit OFFSET :offset
                    """
                ),
                {**params, "limit": page_size, "offset": (page - 1) * page_size},
            ).fetchall()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'agents' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'agents' no longer matches expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying 'agents': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'agents': %s", exc)
            raise DatabaseUnavailableError() from exc

        items = [
            {
                "id": int(row[0]),
                "establishment_name": row[1] or "",
                "email": row[2] or "",
            }
            for row in rows
        ]
        return items, int(total or 0)

    def get_experience_stats(self, db: Session, agent_id: int) -> dict:
        """
        Get experience metrics for the composite trust score.
        Calculates account age and lifetime booking volume.
        """
        try:
            agent_record = db.execute(
                text("SELECT created_at FROM agents WHERE id = :agent_id"),
                {"agent_id": agent_id},
            ).fetchone()

            created_at = agent_record[0] if agent_record and agent_record[0] else None

            booking_record = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN status NOT IN
                            ('pending', 'failed', 'cancelled', 'rejected')
                            THEN id END) as total_bookings, 
                        SUM(CASE WHEN status NOT IN
                            ('pending', 'failed', 'cancelled', 'rejected')
                            THEN total_amount ELSE 0 END) as total_revenue,
                        COUNT(CASE WHEN status = 'cancelled' THEN id END) as total_cancelled
                    FROM bookings 
                    WHERE agent_id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()

            total_bookings = int(booking_record[0] or 0) if booking_record else 0
            total_revenue = float(booking_record[1] or 0.0) if booking_record else 0.0
            total_cancelled = int(booking_record[2] or 0) if booking_record else 0

            return {
                "created_at": created_at,
                "lifetime_bookings": total_bookings,
                "lifetime_revenue": total_revenue,
                "lifetime_cancelled": total_cancelled,
            }
        except ProgrammingError as exc:
            logger.error("Schema change in experience stats query: %s", exc)
            raise SchemaChangedError(
                "Column or table in experience stats no longer matches expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("DB connection error in experience stats query: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in experience stats query: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_credit_stats(self, db: Session, agent_id: int) -> CreditStats:
        """
        Returns credit metrics split into current overdue risk and historical
        payment behavior.

        Overdue definition: status != 'paid' AND due_date IS NOT NULL AND due_date < CURRENT_DATE().
        The agent gets the full due date to make payment.
        """
        try:
            row = db.execute(
                text("""
                    SELECT
                        -- Current overdue count (only past-due, not pending-not-due)
                        COUNT(CASE
                            WHEN ct.status <> 'paid'
                             AND ct.due_date IS NOT NULL
                             AND ct.due_date < CURRENT_DATE()
                            THEN 1
                        END) AS current_overdue_count,

                        -- Current overdue ratio (overdue / total * 100)
                        ROUND(100.0 * COUNT(CASE
                            WHEN ct.status <> 'paid'
                             AND ct.due_date IS NOT NULL
                             AND ct.due_date < CURRENT_DATE()
                            THEN 1
                        END) / NULLIF(COUNT(*), 0), 2) AS current_overdue_ratio,

                        -- Current max delay days
                        GREATEST(
                            COALESCE(
                                (SELECT MAX(DATEDIFF(CURRENT_DATE(), due_date))
                                 FROM credit_transactions
                                 WHERE agent_id = :agent_id
                                   AND status <> 'paid'
                                   AND due_date IS NOT NULL
                                   AND due_date < CURRENT_DATE()), 0),
                            COALESCE(
                                (SELECT CASE
                                    WHEN status = 'paid' AND payment_date > due_date
                                    THEN DATEDIFF(payment_date, due_date)
                                    ELSE 0
                                END
                                FROM credit_transactions
                                WHERE agent_id = :agent_id
                                ORDER BY due_date DESC, id DESC
                                LIMIT 1), 0)
                        ) AS current_max_delay_days,

                        -- Outstanding amount (sum of overdue principal)
                        COALESCE(
                            (SELECT SUM(ABS(principal_amount))
                             FROM credit_transactions
                             WHERE agent_id = :agent_id
                               AND status <> 'paid'
                               AND due_date IS NOT NULL
                               AND due_date < CURRENT_DATE()), 0
                        ) AS outstanding_amount,

                        -- Historical: late payment count (paid but after due date)
                        COUNT(CASE
                            WHEN ct.status = 'paid'
                             AND ct.payment_date > ct.due_date
                            THEN 1
                        END) AS historical_late_payment_count,

                        -- Historical: late payment ratio (late / total paid * 100)
                        ROUND(100.0 * COUNT(CASE
                            WHEN ct.status = 'paid'
                             AND ct.payment_date > ct.due_date
                            THEN 1
                        END) / NULLIF(COUNT(CASE WHEN ct.status = 'paid' THEN 1 END), 0), 2)
                            AS historical_late_payment_ratio,

                        -- Historical: average payment delay (days) for late payments
                        ROUND(COALESCE(AVG(CASE
                            WHEN ct.status = 'paid'
                             AND ct.payment_date > ct.due_date
                            THEN DATEDIFF(ct.payment_date, ct.due_date)
                        END), 0), 1) AS average_payment_delay_days,

                        -- Consecutive unpaid cycles (most recent 3)
                        (SELECT COUNT(*)
                         FROM (
                             SELECT status
                             FROM credit_transactions
                             WHERE agent_id = :agent_id
                             ORDER BY due_date DESC, id DESC
                             LIMIT 3
                         ) AS recent
                         WHERE recent.status <> 'paid') AS consecutive_unpaid_cycles

                    FROM credit_transactions ct
                    WHERE ct.agent_id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'credit_transactions' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'credit_transactions' no longer matches expected schema. "
                "The external DB may have been updated by the source team."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying 'credit_transactions': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'credit_transactions': %s", exc)
            raise DatabaseUnavailableError() from exc

        if row is None:
            return CreditStats(
                current_overdue_count=0,
                current_overdue_ratio=0.0,
                current_max_delay_days=0,
                outstanding_amount=0.0,
                historical_late_payment_count=0,
                historical_late_payment_ratio=0.0,
                average_payment_delay_days=0.0,
                consecutive_unpaid_cycles=0,
            )

        return CreditStats(
            current_overdue_count=int(row[0] or 0),
            current_overdue_ratio=float(row[1] or 0.0),
            current_max_delay_days=int(row[2] or 0),
            outstanding_amount=float(row[3] or 0.0),
            historical_late_payment_count=int(row[4] or 0),
            historical_late_payment_ratio=float(row[5] or 0.0),
            average_payment_delay_days=float(row[6] or 0.0),
            consecutive_unpaid_cycles=int(row[7] or 0),
        )

    def get_supplier_quota_status(
        self,
        db: Session,
        period_type: str | None = None,
        period_days: int | None = None,
    ) -> list[dict]:
        """
        Site-wide supplier request quota (Feature A).

        Counts created-only supplier requests (search_supplier_runs joined to
        search_session_accesses with first_access_type='created') and compares
        them against the supplier's search_limit. The quota is site-wide and
        shared by every user/agent — never scoped by agent_id. minimum_booking
        is returned for transparency but never used to block requests.

        Status matrix:
          unused           consumed == 0
          available        consumed <  search_limit
          exhausted        consumed >= search_limit
          monitoring_only  supplier is inactive
        """
        try:
            settings = get_settings()
            period_type = period_type or settings.supplier_quota_period_type
            period_days = (
                period_days if period_days is not None else settings.supplier_quota_period_days
            )

            window_sql, window_params = _window_sql(period_type, period_days)
            status_ph, status_params = _bind_placeholders(
                "s", settings.supplier_counted_run_statuses
            )

            rows = db.execute(
                text(f"""
                    SELECT s.id, s.code, s.name, s.is_active, s.health_status,
                           s.search_limit, s.minimum_booking,
                           COUNT(DISTINCT r.search_session_id) AS consumed
                    FROM suppliers s
                    LEFT JOIN search_supplier_runs r
                      ON r.supplier_id = s.id
                     AND r.status IN {status_ph}
                     AND {window_sql}
                     AND EXISTS (
                         SELECT 1 FROM search_session_accesses a
                         WHERE a.search_session_id = r.search_session_id
                           AND a.first_access_type = 'created'
                     )
                    GROUP BY s.id
                    ORDER BY s.id
                """),
                {**status_params, **window_params},
            ).fetchall()

            suppliers = []
            for row in rows:
                is_active = int(row[3] or 0) == 1
                search_limit = int(row[5] or 0)
                consumed = int(row[7] or 0)
                remaining = max(search_limit - consumed, 0)

                if not is_active:
                    status = "monitoring_only"
                elif consumed == 0:
                    status = "unused"
                elif consumed < search_limit:
                    status = "available"
                else:
                    status = "exhausted"

                suppliers.append(
                    {
                        "code": row[1],
                        "name": row[2],
                        "is_active": is_active,
                        "health_status": row[4],
                        "search_limit": search_limit,
                        "minimum_booking": int(row[6] or 0),
                        "consumed": consumed,
                        "remaining": remaining,
                        "status": status,
                        "available_to_search": is_active and status in ("available", "unused"),
                    }
                )
            return suppliers
        except ProgrammingError as exc:
            logger.error("Schema change detected in supplier quota query: %s", exc)
            raise SchemaChangedError(
                "'suppliers', 'search_supplier_runs' or 'search_session_accesses' "
                "no longer matches the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying supplier quota: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying supplier quota: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_supplier_expected_ratio(self, db: Session) -> float:
        """
        DB-derived search-to-booking target ratio (bookings per 1 search):
        Σ minimum_booking / Σ search_limit over active suppliers.

        With the current seed data this equals 1050 / 21000 = 0.05 (20:1).
        Returns 0.0 when no active supplier has a usable limit so callers can
        treat the component as "no data".
        """
        try:
            row = db.execute(
                text("""
                    SELECT COALESCE(SUM(minimum_booking), 0) / COALESCE(SUM(search_limit), 0)
                    FROM suppliers
                    WHERE is_active = 1
                      AND minimum_booking > 0
                """)
            ).scalar()
            return float(row or 0.0)
        except ProgrammingError as exc:
            logger.error("Schema change detected in supplier ratio query: %s", exc)
            raise SchemaChangedError("'suppliers' no longer matches the expected schema.") from exc
        except OperationalError as exc:
            logger.error("Database connection error querying supplier ratio: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying supplier ratio: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_agent_search_activity(self, db: Session, agent_id: int, days: int = 365) -> dict:
        """
        Agent search-to-booking inputs (Feature B) over a single trailing window.

        searches = COUNT(DISTINCT search_session_id) covering both 'created' and
                   'reused' access rows — every search intent the agent actually
                   used, regardless of whether it hit the supplier API or a cache.
        bookings = agent's successful bookings within the same boundary (status
                   from supplier_success_booking_statuses).
        """
        try:
            window_expr = "DATE_SUB(CURDATE(), INTERVAL :days DAY)"
            search_row = db.execute(
                text(f"""
                    SELECT
                        COUNT(DISTINCT search_session_id) AS searches,
                        SUM(CASE WHEN first_access_type = 'created' THEN 1 ELSE 0 END) AS created,
                        SUM(CASE WHEN first_access_type = 'reused'  THEN 1 ELSE 0 END) AS reused
                    FROM search_session_accesses
                    WHERE agent_id = :agent_id
                      AND first_accessed_at >= {window_expr}
                """),
                {"agent_id": agent_id, "days": days},
            ).fetchone()

            settings = get_settings()
            status_ph, status_params = _bind_placeholders(
                "s", settings.supplier_success_booking_statuses
            )
            bookings = (
                db.execute(
                    text(f"""
                    SELECT COUNT(*)
                    FROM bookings
                    WHERE agent_id = :agent_id
                      AND status IN {status_ph}
                      AND created_at >= {window_expr}
                """),
                    {**status_params, "agent_id": agent_id, "days": days},
                ).scalar()
                or 0
            )

            assert search_row is not None
            return {
                "searches": int(search_row[0] or 0),
                "created": int(search_row[1] or 0),
                "reused": int(search_row[2] or 0),
                "bookings": int(bookings),
            }
        except ProgrammingError as exc:
            logger.error("Schema change detected in agent search activity query: %s", exc)
            raise SchemaChangedError(
                "'search_session_accesses' or 'bookings' no longer matches the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying agent search activity: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying agent search activity: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_multi_timeframe_stats(self, db: Session, agent_id: int) -> dict[int, dict]:
        """
        Optimized batch fetcher for 1, 7, 30, and 365-day windows.
        Reduces total database roundtrips from 16 queries down to 4.
        """
        try:
            # 1. Searches Batch
            search_rows = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN ss.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN ss.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN ss.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN ss.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM search_sessions ss
                    JOIN agents a ON a.user_id = ss.user_id
                    WHERE a.id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()

            # 2. BookStep Failures Batch
            bs_rows = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM booking_processes bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id AND bp.current_step = 'BookStep' AND bp.state = 'FAILED'
                """),
                {"agent_id": agent_id},
            ).fetchone()

            # 3. Other Step Failures Batch
            other_rows = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN bp.created_at >=
                            DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM booking_processes bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id AND bp.current_step != 'BookStep' AND bp.state = 'FAILED'
                """),
                {"agent_id": agent_id},
            ).fetchone()

            # 4. Bookings & Revenue Batch (Single scan optimization)
            booking_rows = db.execute(
                text("""
                    SELECT 
                        window_days,
                        COUNT(*),
                        COALESCE(SUM(total_amount), 0),
                        COALESCE(AVG(total_amount), 0),
                        COALESCE(STDDEV(total_amount), 0)
                    FROM (
                        SELECT 
                            total_amount,
                            CASE 
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 7
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 30
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 365
                            END as window_days
                        FROM bookings 
                        WHERE agent_id = :agent_id 
                          AND status NOT IN ('pending', 'failed', 'cancelled', 'rejected')
                          AND created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
                    ) as filtered
                    WHERE window_days IS NOT NULL
                    GROUP BY window_days
                """),
                {"agent_id": agent_id},
            ).fetchall()

            # Note: The above query only returns rows for windows with activity.
            # We must accumulate results (since 1d is also 7d, 30d, 365d).
            # Helper to build the cumulative map
            b_map: dict[int, list[float]] = {
                1: [0.0, 0.0, 0.0, 0.0, 0.0],
                7: [0.0, 0.0, 0.0, 0.0, 0.0],
                30: [0.0, 0.0, 0.0, 0.0, 0.0],
                365: [0.0, 0.0, 0.0, 0.0, 0.0],
            }
            for row in booking_rows:
                win = int(row[0])
                # Add this window's stats to itself and all larger windows
                for target_win in [1, 7, 30, 365]:
                    if win <= target_win:
                        b_map[target_win][1] += row[1]  # count
                        b_map[target_win][2] += float(row[2])  # sum
                        # Standard deviation cannot be accurately accumulated without
                        # the raw squared differences.
                        # For trust scoring, we use the largest applicable window's
                        # stddev as an approximation.
                        b_map[target_win][4] = float(row[4])

            # Recompute accurate averages from the accumulated sums and counts
            for target_win in [1, 7, 30, 365]:
                if b_map[target_win][1] > 0:
                    b_map[target_win][3] = b_map[target_win][2] / b_map[target_win][1]

            # Map bookings back to days
            # b_map = {row[0]: row for row in booking_rows}

            # Helper to build the timeframe payload
            def build_window(days, s_val, bs_val, os_val):
                b_data = b_map.get(days, (days, 0, 0.0, 0.0, 0.0))
                adj_bs = int(min(bs_val, s_val * 0.7))
                eff_s = max(s_val - adj_bs, 0)

                threshold = get_settings().conversion_thresholds.get(days, 150)

                return {
                    "searches": s_val,
                    "bookstep_failed": bs_val,
                    "adjusted_bookstep_failed": adj_bs,
                    "other_step_failed": os_val,
                    "effective_searches": eff_s,
                    "no_activity": eff_s == 0,
                    "bookings": b_data[1],
                    "booking_volume": float(b_data[2]),
                    "avg_booking_value": round(b_data[2] / b_data[1], 2) if b_data[1] > 0 else 0.0,
                    "revenue_consistency": round(float(b_data[4]), 2) if days == 365 else 0.0,
                    "low_confidence": eff_s < (threshold * 0.2),
                }

            assert search_rows is not None and bs_rows is not None and other_rows is not None

            return {
                1: build_window(1, search_rows[0], bs_rows[0], other_rows[0]),
                7: build_window(7, search_rows[1], bs_rows[1], other_rows[1]),
                30: build_window(30, search_rows[2], bs_rows[2], other_rows[2]),
                365: build_window(365, search_rows[3], bs_rows[3], other_rows[3]),
            }

        except Exception as exc:
            logger.error("Failed batch stat fetch for agent %s: %s", agent_id, exc)
            raise DatabaseUnavailableError("Batch metric fetch failed") from exc
