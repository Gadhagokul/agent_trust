# app/infra/db/repository.py
import logging

from sqlalchemy import text
from sqlalchemy.exc import OperationalError, ProgrammingError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.domain.errors import AgentNotFoundError, DatabaseUnavailableError, SchemaChangedError

logger = logging.getLogger(__name__)


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
            )
        except OperationalError as exc:
            logger.error("Database connection error querying 'agents': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            )
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'agents': %s", exc)
            raise DatabaseUnavailableError()

        if not result:
            raise AgentNotFoundError(f"Agent {agent_id} not found")

        return result[0], result[1]

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
                        COUNT(CASE WHEN status NOT IN ('pending', 'failed', 'cancelled', 'rejected') THEN id END) as total_bookings, 
                        SUM(CASE WHEN status NOT IN ('pending', 'failed', 'cancelled', 'rejected') THEN total_amount ELSE 0 END) as total_revenue,
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
                "lifetime_cancelled": total_cancelled
            }
        except ProgrammingError as exc:
            logger.error("Schema change in experience stats query: %s", exc)
            raise SchemaChangedError(
                "Column or table in experience stats no longer matches expected schema."
            )
        except OperationalError as exc:
            logger.error("DB connection error in experience stats query: %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            )
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in experience stats query: %s", exc)
            raise DatabaseUnavailableError()

    def get_credit_stats(self, db: Session, agent_id: int) -> dict:
        """
        Get all credit metrics for trust scoring.
        Calculates active overdue days, unpaid ratio over all historical cycles,
        and total outstanding debt.
        """
        try:
            result = db.execute(
                text("""
                    SELECT
                        GREATEST(
                            COALESCE(
                                (
                                    SELECT MAX(DATEDIFF(CURRENT_DATE(), due_date))
                                    FROM credit_transactions
                                    WHERE agent_id = :agent_id AND status != 'paid' AND CURRENT_DATE() > due_date
                                ), 0
                            ),
                            COALESCE(
                                (
                                    SELECT CASE
                                        WHEN status = 'paid' AND payment_date > due_date THEN DATEDIFF(payment_date, due_date)
                                        ELSE 0
                                    END
                                    FROM credit_transactions
                                    WHERE agent_id = :agent_id
                                    ORDER BY due_date DESC, id DESC
                                    LIMIT 1
                                ), 0
                            )
                        ) AS current_credit_delay_days,

                        COUNT(*) AS total_credit_transactions,

                        -- Unpaid amounts/counts only penalize if due date has actually passed
                        SUM(CASE WHEN status != 'paid' AND CURRENT_DATE() > due_date THEN ABS(principal_amount) ELSE 0 END) AS total_unpaid_amount,

                        SUM(ABS(principal_amount)) AS total_credit_given,

                        COUNT(CASE WHEN status != 'paid' THEN 1 END) AS unpaid_count
                    FROM credit_transactions
                    WHERE agent_id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'credit_transactions' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'credit_transactions' no longer matches expected schema. "
                "The external DB may have been updated by the source team."
            )
        except OperationalError as exc:
            logger.error("Database connection error querying 'credit_transactions': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            )
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'credit_transactions': %s", exc)
            raise DatabaseUnavailableError()

        total_given = float(result[3] or 0.0)
        total_unpaid = float(result[2] or 0.0)
        total_cycles = int(result[1] or 0)
        unpaid_count = int(result[4] or 0)

        return {
            "current_credit_delay_days": int(result[0] or 0),
            "total_credit_transactions": total_cycles,
            "total_unpaid_amount": total_unpaid,
            "total_credit_given": total_given,
            "unpaid_count": unpaid_count,
            "unpaid_ratio": round((unpaid_count / total_cycles * 100), 2) if total_cycles > 0 else 0.0,
        }

    def get_recent_credit_cycles(self, db: Session, agent_id: int, limit: int = 3) -> list[str]:
        """
        Fetch the statuses of the agent's most recent credit cycles (newest first).
        Used for the consecutive default evaluation.
        """
        try:
            results = db.execute(
                text("""
                    SELECT status
                    FROM credit_transactions
                    WHERE agent_id = :agent_id
                    ORDER BY due_date DESC, id DESC
                    LIMIT :limit
                """),
                {"agent_id": agent_id, "limit": limit},
            ).fetchall()
            return [row[0] for row in results]
        except ProgrammingError as exc:
            logger.error("Schema change in recent credit cycles query: %s", exc)
            raise SchemaChangedError()
        except OperationalError as exc:
            logger.error("DB connection error in recent credit cycles query: %s", exc)
            raise DatabaseUnavailableError()
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in recent credit cycles query: %s", exc)
            raise DatabaseUnavailableError()

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
                        COUNT(CASE WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM search_sessions WHERE agent_id = :agent_id
                """),
                {"agent_id": agent_id}
            ).fetchone()

            # 2. BookStep Failures Batch
            bs_rows = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM booking_process bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id AND bp.current_step = 'BookStep' AND bp.state = 'FAILED'
                """),
                {"agent_id": agent_id}
            ).fetchone()

            # 3. Other Step Failures Batch
            other_rows = db.execute(
                text("""
                    SELECT 
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1 END) as d1,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 1 END) as d7,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 1 END) as d30,
                        COUNT(CASE WHEN bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 1 END) as d365
                    FROM booking_process bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id AND bp.current_step != 'BookStep' AND bp.state = 'FAILED'
                """),
                {"agent_id": agent_id}
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
                {"agent_id": agent_id}
            ).fetchall()

            # Note: The above query only returns rows for windows with activity.
            # We must accumulate results (since 1d is also 7d, 30d, 365d).
            # Helper to build the cumulative map
            b_map = {1: [0,0,0,0,0], 7: [0,0,0,0,0], 30: [0,0,0,0,0], 365: [0,0,0,0,0]}
            for row in booking_rows:
                win = int(row[0])
                # Add this window's stats to itself and all larger windows
                for target_win in [1, 7, 30, 365]:
                    if win <= target_win:
                        b_map[target_win][1] += row[1] # count
                        b_map[target_win][2] += float(row[2]) # sum
                        # Standard deviation cannot be accurately accumulated without raw squared differences.
                        # For trust scoring, we use the largest applicable window's stddev as an approximation.
                        b_map[target_win][4] = float(row[4])
            
            # Recompute accurate averages from the accumulated sums and counts
            for target_win in [1, 7, 30, 365]:
                if b_map[target_win][1] > 0:
                    b_map[target_win][3] = b_map[target_win][2] / b_map[target_win][1]

            # Map bookings back to days
            #b_map = {row[0]: row for row in booking_rows}

            # Helper to build the timeframe payload
            def build_window(days, s_val, bs_val, os_val):
                b_data = b_map.get(days, (days, 0, 0.0, 0.0, 0.0))
                adj_bs = int(min(bs_val, s_val * 0.7))
                eff_s = max(s_val - adj_bs, 0)
                

                threshold_map = {1: 15, 7: 50, 30: 150, 365: 500}
                threshold = threshold_map.get(days, 150)

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
                    "low_confidence": eff_s < (threshold * 0.2)
                }


            return {
                1:   build_window(1, search_rows[0], bs_rows[0], other_rows[0]),
                7:   build_window(7, search_rows[1], bs_rows[1], other_rows[1]),
                30:  build_window(30, search_rows[2], bs_rows[2], other_rows[2]),
                365: build_window(365, search_rows[3], bs_rows[3], other_rows[3]),
            }

        except Exception as exc:
            logger.error("Failed batch stat fetch for agent %s: %s", agent_id, exc)
            raise DatabaseUnavailableError("Batch metric fetch failed")

    def get_agent_conversion_stats(self, db: Session, agent_id: int, days_lookback: int) -> dict:
        """
        Calculates search-to-booking conversions over a specified trailing timeframe.

        Design principles applied:
          1. BookStep failures are excluded via effective_searches (not by JOIN on bookings).
          2. Abuse prevention: BookStep failures capped at 70% of raw searches so agents
             cannot intentionally fail at BookStep to inflate their conversion rate.
          3. Other-step failures (agent-caused) counted separately as behavioral signal.
          4. Low-confidence flag raised when effective_searches < 5.
          5. Revenue normalization: avg_booking_value and revenue_consistency (stddev).
        """
        try:
            # ── 1. Raw search volume ────────────────────────────────────────────────
            searches = db.execute(
                text("""
                    SELECT COUNT(*)
                    FROM search_sessions
                    WHERE agent_id = :agent_id
                      AND created_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                """),
                {"agent_id": agent_id, "days": days_lookback}
            ).scalar() or 0

            # ── 2. BookStep failures (likely system/payment-gateway) ─────────────────
            bookstep_failed = db.execute(
                text("""
                    SELECT COUNT(*)
                    FROM booking_process bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id
                      AND bp.current_step = 'BookStep'
                      AND bp.state = 'FAILED'
                      AND bp.created_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                """),
                {"agent_id": agent_id, "days": days_lookback}
            ).scalar() or 0

            # ── 3. Other-step failures (likely agent-caused: validation, duplicates) ─
            other_step_failed = db.execute(
                text("""
                    SELECT COUNT(*)
                    FROM booking_process bp
                    JOIN agents a ON a.user_id = bp.user_id
                    WHERE a.id = :agent_id
                      AND bp.current_step != 'BookStep'
                      AND bp.state = 'FAILED'
                      AND bp.created_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                """),
                {"agent_id": agent_id, "days": days_lookback}
            ).scalar() or 0

            # ── 4. Abuse prevention cap ─────────────────────────────────────────────
            # Cap BookStep exclusions to max 70% of raw searches.
            # Prevents agents from intentionally failing at BookStep to shrink the
            # denominator and artificially inflate their conversion rate.
            adjusted_bookstep_failed = int(min(bookstep_failed, searches * 0.7))

            # Floor at 0. Division is safely guarded below.
            effective_searches = max(searches - adjusted_bookstep_failed, 0)

            # ── 5. Bookings + revenue metrics ───────────────────────────────────────
            bookings_data = db.execute(
                text("""
                    SELECT
                        COUNT(*),
                        COALESCE(SUM(total_amount), 0),
                        COALESCE(AVG(total_amount), 0),
                        COALESCE(STDDEV(total_amount), 0)
                    FROM bookings
                    WHERE agent_id = :agent_id
                      AND status NOT IN ('pending', 'failed', 'cancelled', 'rejected')
                      AND created_at >= DATE_SUB(CURDATE(), INTERVAL :days DAY)
                """),
                {"agent_id": agent_id, "days": days_lookback}
            ).fetchone()

            bookings            = bookings_data[0] or 0
            revenue             = float(bookings_data[1] or 0.0)
            avg_booking_value   = round(float(bookings_data[2] or 0.0), 2)
            # Lower stddev = more consistent revenue; stored raw for scorer to interpret
            revenue_consistency = round(float(bookings_data[3] or 0.0), 2)

            threshold_map = {1: 15, 7: 50, 30: 150, 365: 500}
            threshold = threshold_map.get(days_lookback, 150)

            return {
                "searches":               searches,
                "bookstep_failed":        bookstep_failed,
                "adjusted_bookstep_failed": adjusted_bookstep_failed,
                "other_step_failed":      other_step_failed,
                "effective_searches":     effective_searches,
                "no_activity": effective_searches == 0, 
                "low_confidence":         effective_searches < (threshold * 0.2),
                "bookings":               bookings,
                "booking_volume":         revenue,
                "avg_booking_value":      avg_booking_value,
                "revenue_consistency":    revenue_consistency,
            }
        except Exception as exc:
            logger.error("Failed to fetch conversion stats for agent %s: %s", agent_id, exc)
            raise DatabaseUnavailableError("Failed to calculate conversion metrics")

    # ───────────────────────────────────────────────────────────────────── #
    # Phase 1: Reliability Metrics                                         #
    # ───────────────────────────────────────────────────────────────────── #

    def get_booking_reliability_stats(self, db: Session, agent_id: int) -> dict:
        """
        Computes reliability metrics from the bookings table.
        Uses the actual bookings.status enum values from production:
          - 'confirmed' and 'ticketed' → completed (successful)
          - 'cancelled'                → failed (agent or system cancellation)
          - 'pending'                  → incomplete (not yet actionable)

        Returns:
            completed_bookings: int
            cancelled_bookings: int
            pending_bookings: int
            total_actionable: int (completed + cancelled)
            completion_rate: float (0-100)
            cancellation_rate: float (0-100)
        """
        try:
            result = db.execute(
                text("""
                    SELECT
                        COUNT(*) AS total,
                        SUM(CASE WHEN status IN ('confirmed', 'ticketed') THEN 1 ELSE 0 END) AS completed,
                        SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                        SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending
                    FROM bookings
                    WHERE agent_id = :agent_id
                """),
                {"agent_id": agent_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change detected in 'bookings' table: %s", exc)
            raise SchemaChangedError(
                "Column or table 'bookings' no longer matches expected schema."
            )
        except OperationalError as exc:
            logger.error("Database connection error querying 'bookings': %s", exc)
            raise DatabaseUnavailableError(
                "Unable to connect to the external database. Please try again later."
            )
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error querying 'bookings': %s", exc)
            raise DatabaseUnavailableError()

        total = int(result[0] or 0)
        completed = int(result[1] or 0)
        cancelled = int(result[2] or 0)
        pending = int(result[3] or 0)
        actionable = completed + cancelled

        return {
            "total_bookings": total,
            "completed_bookings": completed,
            "cancelled_bookings": cancelled,
            "pending_bookings": pending,
            "total_actionable": actionable,
            "completion_rate": round(completed / actionable * 100, 2) if actionable > 0 else 0.0,
            "cancellation_rate": round(cancelled / actionable * 100, 2) if actionable > 0 else 0.0,
        }

    def get_timeframed_reliability(self, db: Session, agent_id: int) -> dict[int, dict]:
        """
        Reliability metrics broken down by 1/7/30/365-day windows.
        Mirrors the structure of get_multi_timeframe_stats() for consistency.
        """
        try:
            rows = db.execute(
                text("""
                    SELECT
                        window_days,
                        COUNT(*) AS total,
                        SUM(CASE WHEN status IN ('confirmed', 'ticketed') THEN 1 ELSE 0 END) AS completed,
                        SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled
                    FROM (
                        SELECT
                            status,
                            CASE
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 1 DAY) THEN 1
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 7 DAY) THEN 7
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY) THEN 30
                                WHEN created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY) THEN 365
                            END AS window_days
                        FROM bookings
                        WHERE agent_id = :agent_id
                          AND created_at >= DATE_SUB(CURDATE(), INTERVAL 365 DAY)
                    ) AS filtered
                    WHERE window_days IS NOT NULL
                    GROUP BY window_days
                """),
                {"agent_id": agent_id},
            ).fetchall()

            rmap = {1: {"total": 0, "completed": 0, "cancelled": 0},
                    7: {"total": 0, "completed": 0, "cancelled": 0},
                    30: {"total": 0, "completed": 0, "cancelled": 0},
                    365: {"total": 0, "completed": 0, "cancelled": 0}}

            for row in rows:
                win = int(row[0])
                for target in [1, 7, 30, 365]:
                    if win <= target:
                        rmap[target]["total"] += int(row[1] or 0)
                        rmap[target]["completed"] += int(row[2] or 0)
                        rmap[target]["cancelled"] += int(row[3] or 0)

            def build(days: int) -> dict:
                d = rmap[days]
                actionable = d["completed"] + d["cancelled"]
                return {
                    "bookings_total": d["total"],
                    "completed": d["completed"],
                    "cancelled": d["cancelled"],
                    "completion_rate": round(d["completed"] / actionable * 100, 2) if actionable > 0 else 0.0,
                    "cancellation_rate": round(d["cancelled"] / actionable * 100, 2) if actionable > 0 else 0.0,
                }

            return {1: build(1), 7: build(7), 30: build(30), 365: build(365)}

        except Exception as exc:
            logger.error("Failed to fetch timeframed reliability for agent %s: %s", agent_id, exc)
            raise DatabaseUnavailableError("Timeframed reliability fetch failed")

    # ───────────────────────────────────────────────────────────────────── #
    # Phase 1: Experience Metrics                                          #
    # ───────────────────────────────────────────────────────────────────── #

    def get_agent_tenure(self, db: Session, agent_id: int) -> dict:
        """
        Returns agent tenure information based on the earliest booking and
        agent account creation date.
        """
        try:
            row = db.execute(
                text("""
                    SELECT
                        a.created_at AS account_created_at,
                        MIN(b.created_at) AS first_booking_at,
                        MAX(b.created_at) AS last_booking_at,
                        COUNT(b.id) AS lifetime_bookings
                    FROM agents a
                    LEFT JOIN bookings b ON b.agent_id = a.id
                    WHERE a.id = :agent_id
                    GROUP BY a.id, a.created_at
                """),
                {"agent_id": agent_id},
            ).fetchone()
        except ProgrammingError as exc:
            logger.error("Schema change in tenure query: %s", exc)
            raise SchemaChangedError()
        except OperationalError as exc:
            logger.error("DB connection error in tenure query: %s", exc)
            raise DatabaseUnavailableError()
        except SQLAlchemyError as exc:
            logger.error("Unexpected DB error in tenure query: %s", exc)
            raise DatabaseUnavailableError()

        if not row:
            return {
                "account_created_days": 0,
                "first_booking_days": None,
                "lifetime_bookings": 0,
                "has_booking_history": False,
            }

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        account_created = row[0]
        first_booking = row[1]
        lifetime = int(row[3] or 0)

        account_days = (now - account_created.replace(tzinfo=timezone.utc)).days if account_created else 0
        first_booking_days = (now - first_booking.replace(tzinfo=timezone.utc)).days if first_booking else None

        return {
            "account_created_days": max(account_days, 0),
            "first_booking_days": max(first_booking_days, 0) if first_booking_days is not None else None,
            "lifetime_bookings": lifetime,
            "has_booking_history": first_booking is not None,
        }

    def get_volume_trends(self, db: Session, agent_id: int) -> dict:
        """
        Computes month-over-month booking volume trends for the past 6 months.
        Provides signals for agent experience and growth trajectory.
        """
        try:
            rows = db.execute(
                text("""
                    SELECT
                        DATE_FORMAT(created_at, '%Y-%m') AS month,
                        COUNT(*) AS bookings,
                        COALESCE(SUM(total_amount), 0) AS revenue
                    FROM bookings
                    WHERE agent_id = :agent_id
                      AND status IN ('confirmed', 'ticketed')
                      AND created_at >= DATE_SUB(CURDATE(), INTERVAL 6 MONTH)
                    GROUP BY DATE_FORMAT(created_at, '%Y-%m')
                    ORDER BY month ASC
                """),
                {"agent_id": agent_id},
            ).fetchall()
        except Exception as exc:
            logger.error("Failed to fetch volume trends for agent %s: %s", agent_id, exc)
            raise DatabaseUnavailableError("Volume trends fetch failed")

        months = []
        total_6m = 0
        for row in rows:
            b = int(row[1] or 0)
            r = float(row[2] or 0.0)
            months.append({"month": row[0], "bookings": b, "revenue": r})
            total_6m += b

        if len(months) >= 2:
            recent = months[-1]["bookings"]
            previous = months[-2]["bookings"]
            growth = round((recent - previous) / max(previous, 1) * 100, 2) if previous > 0 else 0.0
        else:
            growth = 0.0

        return {
            "monthly_breakdown": months,
            "total_bookings_6m": total_6m,
            "active_months_6m": len(months),
            "month_over_month_growth_pct": growth,
            "has_consistent_activity": len(months) >= 3,
        }