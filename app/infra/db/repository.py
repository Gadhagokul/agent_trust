# app/infra/db/repository.py
import logging
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.errors import AgentNotFoundError, DatabaseUnavailableError, SchemaChangedError
from app.infra.settings import get_settings

logger = logging.getLogger(__name__)

# Sprint 6 label semantics (README target table): a severe default is a credit
# transaction >= 30 days past due and still unpaid; a severe reliability event
# is >= 50% of the eligible window attempts failing.
SEVERE_DEFAULT_PAST_DUE_DAYS = 30
SEVERE_RELIABILITY_RATIO = 0.5


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
    maximum_payment_delay_days: int
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
        boundary = get_settings().credit_overdue_boundary
        try:
            row = db.execute(
                text(f"""
                    SELECT
                        -- Current overdue count (only past-due, not pending-not-due)
                        COUNT(CASE
                            WHEN ct.status <> 'paid'
                             AND ct.due_date IS NOT NULL
                             AND ct.due_date {boundary} CURRENT_DATE()
                            THEN 1
                        END) AS current_overdue_count,

                        -- Current overdue ratio (overdue / total * 100)
                        ROUND(100.0 * COUNT(CASE
                            WHEN ct.status <> 'paid'
                             AND ct.due_date IS NOT NULL
                             AND ct.due_date {boundary} CURRENT_DATE()
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
                                   AND due_date {boundary} CURRENT_DATE()), 0),
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
                               AND due_date {boundary} CURRENT_DATE()), 0
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

                        -- Historical: maximum payment delay (days) for late payments
                        ROUND(COALESCE(MAX(CASE
                            WHEN ct.status = 'paid'
                             AND ct.payment_date > ct.due_date
                            THEN DATEDIFF(ct.payment_date, ct.due_date)
                        END), 0), 0) AS maximum_payment_delay_days,

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
                maximum_payment_delay_days=0,
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
            maximum_payment_delay_days=int(row[7] or 0),
            consecutive_unpaid_cycles=int(row[8] or 0),
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



    def get_total_booking_count(self, db: Session) -> int:
        """
        Sprint 5 - total confirmed booking rows (cold-start/sample-size guard).
        Used by the ML readiness computation as the global L2B baseline. Leak-safe:
        purely a count over the bookings table, never filtered by agent identity
        or date -- a global fetch cannot leak future agent-specific behaviour.
        """
        return int(
            db.execute(text("SELECT COUNT(id) FROM bookings")).scalar() or 0
        )

    def get_labeled_sample_count(
        self,
        db: Session,
        *,
        target: str,
        horizon_days: int,
        as_of: date,
    ) -> int:
        """
        Sprint 6 - eligible LABELLED sample count for one target. This is the data
        source of the training growth trigger and is deliberately NOT the total
        booking count.

        A sample counts as labelled only when its label window has fully matured:
        created_at <= as_of - horizon_days. Rows created inside the still-open
        horizon are excluded because their label is not yet observable - they are
        never imputed, never counted early (senior sec19 leakage rule).

        Target populations:
          * severe_default / severe_reliability -> the matured booking population
            (the positive/negative split is decided later by the label builder);
          * l2b_breach -> only matured bookings at suppliers that actually have an
            L2B benchmark configured (minimum_booking + search_limit), because an
            unconfigured supplier cannot carry an L2B label.
        """
        if target not in ("severe_default", "severe_reliability", "l2b_breach"):
            raise ValueError(f"Unknown ML target {target!r}")

        base_sql = """
            SELECT COUNT(b.id)
            FROM bookings b
            {join}
            WHERE b.created_at <= DATE_SUB(:as_of, INTERVAL :horizon_days DAY)
            {extra}
        """
        if target == "l2b_breach":
            join = "JOIN suppliers s ON s.name = b.provider"
            extra = """
                AND s.is_active = 1
                AND s.minimum_booking IS NOT NULL
                AND s.search_limit IS NOT NULL
                AND s.search_limit > 0
            """
        else:
            join = ""
            extra = ""

        try:
            return int(
                db.execute(
                    text(
                        base_sql.format(join=join, extra=extra)
                    ),
                    {"as_of": as_of, "horizon_days": horizon_days},
                ).scalar()
                or 0
            )
        except ProgrammingError as exc:
            logger.error("Schema change detected in labelled sample count: %s", exc)
            raise SchemaChangedError(
                "Booking/supplier tables no longer match the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error counting labelled samples: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error counting labelled samples: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_training_population(
        self,
        db: Session,
        *,
        cut_off: date,
        limit: int = 5000,
    ) -> list[int]:
        """
        Sprint 6 - training population: agent ids with at least one booking
        created at/before the snapshot cut-off, so point-in-time features exist
        for them. The caller passes a MATURED cut-off (today - horizon), which is
        what makes the (cut_off, cut_off + horizon] label window observable now.
        """
        try:
            rows = db.execute(
                text("""
                    SELECT agent_id
                    FROM bookings
                    WHERE created_at <= :cut_off
                      AND agent_id IS NOT NULL
                    GROUP BY agent_id
                    ORDER BY agent_id
                    LIMIT :limit
                """),
                {
                    "cut_off": cut_off,
                    "limit": limit,
                },
            ).fetchall()
        except ProgrammingError as exc:
            logger.error("Schema change detected in training population query: %s", exc)
            raise SchemaChangedError(
                "'bookings' no longer matches the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error in training population query: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in training population query: %s", exc)
            raise DatabaseUnavailableError() from exc
        return [int(row[0]) for row in rows]

    def get_training_feature_rows(
        self,
        db: Session,
        *,
        agent_ids: list[int],
        as_of: date,
    ) -> list[dict]:
        """
        Sprint 6 - point-in-time ML feature rows for the training population.

        Every column is bounded by `as_of` (the snapshot cut-off T): search
        activity is `first_accessed_at <= T`, bookings are `created_at <= T`, and
        credit exposure is `due_date < T` while still unpaid. Nothing after T can
        enter a feature vector, which is what makes the following label window
        leak-free.

        The overdue picture as of T is an approximation: a transaction that was
        paid after T but before now still counts as unpaid here, because the
        schema exposes no payment-state ledger at T. The date bounds themselves
        are strict; this residual approximation is recorded as a known limitation
        of the training snapshot, not of the live scorer.
        """
        if not agent_ids:
            return []
        placeholders, params = _bind_placeholders("a", [str(aid) for aid in agent_ids])
        try:
            rows = db.execute(
                text(f"""
                    SELECT
                        a.id AS agent_id,
                        COUNT(DISTINCT CASE
                            WHEN ssa.first_accessed_at >=
                                DATE_SUB(:as_of, INTERVAL 7 DAY) THEN ssa.search_session_id
                        END) AS searches_7d,
                        COUNT(DISTINCT CASE
                            WHEN ssa.first_accessed_at >=
                                DATE_SUB(:as_of, INTERVAL 30 DAY) THEN ssa.search_session_id
                        END) AS searches_30d,
                        (SELECT COUNT(*) FROM bookings b
                         WHERE b.agent_id = a.id
                           AND b.status IN ('confirmed', 'ticketed')
                           AND b.created_at >= DATE_SUB(:as_of, INTERVAL 7 DAY)
                           AND b.created_at <= :as_of) AS bookings_7d,
                        (SELECT COUNT(*) FROM bookings b
                         WHERE b.agent_id = a.id
                           AND b.status IN ('confirmed', 'ticketed')
                           AND b.created_at >= DATE_SUB(:as_of, INTERVAL 30 DAY)
                           AND b.created_at <= :as_of) AS bookings_30d,
                        (SELECT COUNT(*) FROM credit_transactions ct
                         WHERE ct.agent_id = a.id
                           AND ct.status <> 'paid'
                           AND ct.due_date IS NOT NULL
                           AND ct.due_date < :as_of) AS current_overdue_count,
                        (SELECT COUNT(*) FROM credit_transactions ct
                         WHERE ct.agent_id = a.id) AS credit_transaction_count,
                        (SELECT COALESCE(MAX(DATEDIFF(:as_of, ct.due_date)), 0)
                         FROM credit_transactions ct
                         WHERE ct.agent_id = a.id
                           AND ct.status <> 'paid'
                           AND ct.due_date IS NOT NULL
                           AND ct.due_date < :as_of) AS current_max_delay_days
                    FROM agents a
                    LEFT JOIN search_session_accesses ssa
                           ON ssa.agent_id = a.id
                          AND ssa.first_accessed_at <= :as_of
                    WHERE a.id IN {placeholders}
                    GROUP BY a.id
                    ORDER BY a.id
                """),
                {**params, "as_of": as_of},
            ).fetchall()
        except ProgrammingError as exc:
            logger.error("Schema change detected in training feature query: %s", exc)
            raise SchemaChangedError(
                "Training feature tables no longer match the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error in training feature query: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in training feature query: %s", exc)
            raise DatabaseUnavailableError() from exc

        return [
            {
                "agent_id": int(row[0]),
                "searches_7d": int(row[1] or 0),
                "searches_30d": int(row[2] or 0),
                "bookings_7d": int(row[3] or 0),
                "bookings_30d": int(row[4] or 0),
                "current_overdue_count": int(row[5] or 0),
                "current_overdue_ratio": (
                    round(100.0 * int(row[5] or 0) / int(row[6]), 2) if row[6] else 0.0
                ),
                "current_max_delay_days": int(row[7] or 0),
            }
            for row in rows
        ]

    def get_label_evidence(
        self,
        db: Session,
        *,
        target: str,
        cut_off: date,
        horizon_days: int,
        severe_default_days: int = SEVERE_DEFAULT_PAST_DUE_DAYS,
    ) -> dict[int, dict[str, float]]:
        """
        Sprint 6 - per-agent label evidence drawn STRICTLY from the open label
        window (cut_off, cut_off + horizon_days]. Features live at/before cut_off
        and labels live strictly after it, so no row can see its own future.

        Label definitions follow the approved target table (README):
          * severe_default    -> a credit transaction in the window that is still
                                 unpaid and >= severe_default_days past due;
          * severe_reliability -> >=50% of the agent's eligible window attempts
                                 failed (BookStep FAILED) with at least one
                                 failure;
          * l2b_breach         -> in-window bookings/searches at a supplier exceed
                                 that supplier's configured L2B target.

        Returns {agent_id: {"positives": n, "attempts": m, "breaches": k}}. The
        caller turns these counts into the binary label and the readiness stats.
        """
        window_end = cut_off + timedelta(days=horizon_days)
        try:
            if target == "severe_default":
                rows = db.execute(
                    text("""
                        SELECT agent_id, COUNT(*)
                        FROM credit_transactions
                        WHERE due_date > :cut_off
                          AND due_date <= :window_end
                          AND status <> 'paid'
                          AND DATEDIFF(CURRENT_DATE(), due_date) >= :severe_days
                        GROUP BY agent_id
                    """),
                    {
                        "cut_off": cut_off,
                        "window_end": window_end,
                        "severe_days": severe_default_days,
                    },
                ).fetchall()
                return {
                    int(row[0]): {"positives": float(row[1] or 0), "attempts": 0.0, "breaches": 0.0}
                    for row in rows
                }

            if target == "severe_reliability":
                failure_rows = db.execute(
                    text("""
                        SELECT a.id, COUNT(*)
                        FROM booking_processes bp
                        JOIN agents a ON a.user_id = bp.user_id
                        WHERE bp.current_step = 'BookStep'
                          AND bp.state = 'FAILED'
                          AND bp.created_at > :cut_off
                          AND bp.created_at <= :window_end
                        GROUP BY a.id
                    """),
                    {"cut_off": cut_off, "window_end": window_end},
                ).fetchall()
                success_rows = db.execute(
                    text("""
                        SELECT agent_id, COUNT(*)
                        FROM bookings
                        WHERE status IN ('confirmed', 'ticketed')
                          AND created_at > :cut_off
                          AND created_at <= :window_end
                        GROUP BY agent_id
                    """),
                    {"cut_off": cut_off, "window_end": window_end},
                ).fetchall()
                failures = {int(row[0]): float(row[1] or 0) for row in failure_rows}
                successes = {int(row[0]): float(row[1] or 0) for row in success_rows}
                evidence: dict[int, dict[str, float]] = {}
                for agent_id in set(failures) | set(successes):
                    failed = failures.get(agent_id, 0.0)
                    succeeded = successes.get(agent_id, 0.0)
                    attempts = failed + succeeded
                    severe = (
                        1.0
                        if (failed > 0 and failed >= SEVERE_RELIABILITY_RATIO * attempts)
                        else 0.0
                    )
                    evidence[agent_id] = {
                        "positives": severe,
                        "attempts": attempts,
                        "breaches": 0.0,
                    }
                return evidence

            if target == "l2b_breach":
                booking_rows = db.execute(
                    text("""
                        SELECT b.agent_id, s.code, COUNT(*)
                        FROM bookings b
                        JOIN suppliers s ON s.id = b.supplier_id
                        WHERE b.status IN ('confirmed', 'ticketed')
                          AND b.created_at > :cut_off
                          AND b.created_at <= :window_end
                          AND s.is_active = 1
                          AND s.minimum_booking IS NOT NULL
                          AND s.search_limit IS NOT NULL
                          AND s.search_limit > 0
                        GROUP BY b.agent_id, s.code
                    """),
                    {"cut_off": cut_off, "window_end": window_end},
                ).fetchall()
                search_rows = db.execute(
                    text("""
                        SELECT a.agent_id, r.supplier_code, SUM(a.access_count)
                        FROM search_session_accesses a
                        JOIN search_supplier_runs r
                          ON r.search_session_id = a.search_session_id
                        WHERE a.first_accessed_at > :cut_off
                          AND a.first_accessed_at <= :window_end
                        GROUP BY a.agent_id, r.supplier_code
                    """),
                    {"cut_off": cut_off, "window_end": window_end},
                ).fetchall()
                target_rows = db.execute(
                    text("""
                        SELECT code, minimum_booking / search_limit
                        FROM suppliers
                        WHERE is_active = 1
                          AND minimum_booking IS NOT NULL
                          AND search_limit IS NOT NULL
                          AND search_limit > 0
                    """)
                ).fetchall()
                targets_by_code = {row[0]: float(row[1]) for row in target_rows}
                searches_by_agent_code: dict[tuple[int, str], float] = {
                    (int(row[0]), row[1]): float(row[2] or 0) for row in search_rows
                }
                evidence = {}
                for row in booking_rows:
                    agent_id = int(row[0])
                    code = row[1]
                    supplier_target = targets_by_code.get(code)
                    if supplier_target is None:
                        continue
                    bookings = float(row[2] or 0)
                    searches = searches_by_agent_code.get((agent_id, code), 0.0)
                    breaches = (
                        1.0
                        if (searches > 0 and bookings / searches > supplier_target)
                        else 0.0
                    )
                    bucket = evidence.setdefault(
                        agent_id, {"positives": 0.0, "attempts": 0.0, "breaches": 0.0}
                    )
                    bucket["attempts"] += bookings
                    bucket["breaches"] += breaches
                    bucket["positives"] = 1.0 if bucket["breaches"] > 0 else 0.0
                return evidence

            raise ValueError(f"Unknown ML target {target!r}")
        except ProgrammingError as exc:
            logger.error("Schema change detected in label evidence query: %s", exc)
            raise SchemaChangedError(
                "Label evidence tables no longer match the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error in label evidence query: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in label evidence query: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_agent_supplier_l2b_snapshot(self, db: Session, agent_id: int) -> list[dict]:
        """
        Sprint 5 - per-agent supplier L2B snapshot for ML label/feature building.
        Each row: supplier_code, target (site config `get_supplier_l2b_targets`),
        searches (search_session count), bookings (confirmed+ticketed count), and
        the derived l2b_ratio. This is the ONLY ML feature source and it is
        point-in-time: every row is computed strictly from aggregates already
        present in the DB at call time (no retroactive/future rows).
        """
        targets_by_code = {t["code"]: t for t in self.get_supplier_l2b_targets(db)}
        search_counts = self.get_agent_supplier_searches(db, agent_id, days=365)
        booking_rows = db.execute(
            text(
                """
                SELECT s.code AS supplier_code, COUNT(b.id) AS bookings
                FROM suppliers s
                LEFT JOIN bookings b
                       ON b.supplier_id = s.id
                      AND b.status IN ('confirmed', 'ticketed')
                      AND b.agent_id = :agent_id
                GROUP BY s.code
                """
            ),
            {"agent_id": agent_id},
        ).fetchall()

        booking_by_code = {r[0]: int(r[1] or 0) for r in booking_rows}
        snapshots: list[dict] = []
        for code, info in targets_by_code.items():
            searches = sum(
                1 for r in search_counts if r.get("supplier_code") == code
            )
            bookings = booking_by_code.get(code, 0)
            target = info.get("target")
            snapshots.append(
                {
                    "supplier_code": code,
                    "target": target,
                    "searches": searches,
                    "bookings": bookings,
                    "l2b_ratio": round(bookings / searches, 4) if searches else 0.0,
                }
            )
        return snapshots

    def get_supplier_l2b_targets(self, db: Session) -> list[dict]:
        """
        Site-level per-supplier Search-to-Booking benchmarks (senior §10-13).

        target = minimum_booking / search_limit per active supplier. These are
        SUPPLIER/SITE-level configuration values and are never derived from any
        agent's activity. A supplier is "configured" only when both columns are
        present and search_limit > 0; otherwise target is None and the supplier
        is excluded from scoring (never assigned invented compliance).
        """
        try:
            rows = db.execute(
                text("""
                    SELECT code, name, minimum_booking, search_limit
                    FROM suppliers
                    WHERE is_active = 1
                """)
            ).fetchall()

            targets = []
            for row in rows:
                code = row[0]
                name = row[1]
                minimum_booking = row[2]
                search_limit = row[3]
                if (
                    minimum_booking is not None
                    and search_limit is not None
                    and int(search_limit) > 0
                ):
                    target = float(minimum_booking) / float(search_limit)
                else:
                    target = None
                targets.append({"code": code, "name": name, "target": target})
            return targets
        except ProgrammingError as exc:
            logger.error("Schema change detected in supplier L2B target query: %s", exc)
            raise SchemaChangedError("'suppliers' no longer matches the expected schema.") from exc
        except OperationalError as exc:
            logger.error("Database connection error querying supplier L2B targets: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying supplier L2B targets: %s", exc)
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

    def get_agent_supplier_searches(
        self, db: Session, agent_id: int, days: int = 365
    ) -> list[dict]:
        """
        Agent search volume per supplier (supplier-specific L2B denominator).

        Both access types count (created + reused): a reused search is still the
        agent searching, so excluding it would inflate their L2B. Volume uses
        SUM(access_count) because one access row can represent many repeat
        initiations/reuses. Confirmed from real data: one session is sent to all
        selected suppliers, so the same access_count legitimately contributes to
        every supplier that participated in that session (per search_supplier_runs).
        """
        try:
            rows = db.execute(
                text("""
                    SELECT r.supplier_code AS code, SUM(a.access_count) AS searches
                    FROM search_session_accesses a
                    JOIN search_supplier_runs r
                      ON r.search_session_id = a.search_session_id
                    WHERE a.agent_id = :agent_id
                      AND a.first_accessed_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                    GROUP BY r.supplier_code
                """),
                {"agent_id": agent_id, "days": days},
            ).fetchall()

            return [
                {"code": row[0], "searches": int(row[1] or 0)}
                for row in rows
                if row[0] is not None
            ]
        except ProgrammingError as exc:
            logger.error("Schema change detected in agent supplier searches query: %s", exc)
            raise SchemaChangedError(
                "'search_session_accesses' or 'search_supplier_runs' no longer matches "
                "the expected schema."
            ) from exc
        except OperationalError as exc:
            logger.error("Database connection error querying agent supplier searches: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying agent supplier searches: %s", exc)
            raise DatabaseUnavailableError() from exc

    def get_agent_booking_counts_by_provider(
        self, db: Session, agent_id: int, days: int = 365
    ) -> list[dict]:
        """
        Agent bookings per supplier, attributed via bookings.provider, matching
        suppliers.name for the corresponding supplier (confirmed data link).
        """
        try:
            settings = get_settings()
            status_ph, status_params = _bind_placeholders(
                "s", settings.supplier_success_booking_statuses
            )
            rows = db.execute(
                text(f"""
                    SELECT provider, COUNT(*) AS bookings
                    FROM bookings
                    WHERE agent_id = :agent_id
                      AND status IN {status_ph}
                      AND created_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                    GROUP BY provider
                """),
                {**status_params, "agent_id": agent_id, "days": days},
            ).fetchall()

            return [
                {"provider": row[0], "bookings": int(row[1] or 0)}
                for row in rows
                if row[0] is not None
            ]
        except ProgrammingError as exc:
            logger.error("Schema change detected in agent booking counts query: %s", exc)
            raise SchemaChangedError("'bookings' no longer matches the expected schema.") from exc
        except OperationalError as exc:
            logger.error("Database connection error querying agent booking counts: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            ) from exc
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying agent booking counts: %s", exc)
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
