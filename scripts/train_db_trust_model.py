# scripts/train_db_trust_model.py
import logging
import os
import sys
from datetime import date

import joblib
import numpy as np
from sklearn.ensemble import RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error
from sklearn.model_selection import train_test_split
from sqlalchemy import text

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infra.db.repository import AgentRepository
from app.infra.db.session import SessionLocal

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def _calculate_real_world_outcome(db, agent_id) -> float:
    """
    Computes a heuristic target score (5.0 to 100.0) used as a pseudo-label
    for training. Penalties cover repayment delays, outstanding defaults, and
    BookStep failures, using the same search/booking definitions as the
    production Search-to-Booking pipeline:
    - searches: COUNT(DISTINCT search_session_id) over search_session_accesses
    - bookings: status IN ('confirmed', 'ticketed')
    - failures: booking_processes current_step='BookStep' AND state='FAILED',
      capped at searches_30d * 0.7 (matching production).
    This is a heuristic/manual label, not a directly measured outcome.
    """
    tx_rows = db.execute(
        text("""
            SELECT status, due_date, payment_date
            FROM credit_transactions
            WHERE agent_id = :agent_id
        """),
        {"agent_id": agent_id},
    ).fetchall()

    score = 100.0

    for row in tx_rows:
        status, due_date_val, payment_date_val = row

        d_date = due_date_val.date() if hasattr(due_date_val, "date") else due_date_val
        p_date = payment_date_val.date() if hasattr(payment_date_val, "date") else payment_date_val

        if status == "paid":
            if p_date and d_date and p_date > d_date:
                delay = (p_date - d_date).days
                score -= min(delay * 1.5, 25.0)
        else:
            today = date.today()
            if d_date and today > d_date:
                delay = (today - d_date).days
                score -= min(delay * 2.0, 40.0)
                score -= 10.0

    searches_30d = 0
    stats_30d = db.execute(
        text("""
            SELECT
                (SELECT COUNT(DISTINCT search_session_id) FROM search_session_accesses
                 WHERE agent_id = :agent_id
                   AND first_accessed_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)) as searches,
                (SELECT COUNT(*) FROM bookings
                 WHERE agent_id = :agent_id
                   AND status IN ('confirmed', 'ticketed')
                   AND created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)) as bookings
        """),
        {"agent_id": agent_id},
    ).fetchone()

    if stats_30d:
        searches_30d, bookings = stats_30d
        searches_30d = int(searches_30d or 0)
        bookings = int(bookings or 0)
        if searches_30d > 50 and bookings == 0:
            score -= 20.0

    fails = db.execute(
        text("""
            SELECT COUNT(*)
            FROM booking_processes bp
            JOIN agents a ON a.user_id = bp.user_id
            WHERE a.id = :agent_id
              AND bp.current_step = 'BookStep'
              AND bp.state = 'FAILED'
              AND bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
        """),
        {"agent_id": agent_id},
    ).scalar() or 0

    fails = min(fails, int(searches_30d * 0.7))
    score -= min(fails * 2.0, 15.0)

    return max(5.0, min(score, 100.0))


def extract_and_train():
    db = SessionLocal()
    repo = AgentRepository()

    logger.info("Connecting to Database and fetching all active agents...")

    try:
        agent_rows = db.execute(text("SELECT id FROM agents")).fetchall()
        agent_ids = [row[0] for row in agent_rows]
    except Exception as e:
        logger.error("Failed to fetch agents from database: %s", e)
        db.close()
        return

    logger.info("Found %d agents. Extracting features and computing outcomes...", len(agent_ids))

    x_list = []
    y_list = []

    for agent_id in agent_ids:
        try:
            real_outcome = _calculate_real_world_outcome(db, agent_id)

            batch_stats = repo.get_multi_timeframe_stats(db, agent_id)
            credit_stats = repo.get_credit_stats(db, agent_id)

            stats_7d = batch_stats.get(7, {})
            stats_30d = batch_stats.get(30, {})

            feature_vector = [
                stats_7d.get("effective_searches", 0),
                stats_7d.get("bookings", 0),
                stats_30d.get("effective_searches", 0),
                stats_30d.get("bookings", 0),
                credit_stats.current_overdue_count,
                credit_stats.current_overdue_ratio,
                credit_stats.current_max_delay_days,
            ]

            x_list.append(feature_vector)
            y_list.append(real_outcome)

        except Exception as e:
            logger.warning("Failed to extract data for agent %s: %s", agent_id, e)
            continue

    db.close()

    if not x_list:
        logger.error("No training data could be extracted.")
        return

    x = np.array(x_list)
    y = np.array(y_list)

    logger.info("Successfully extracted %d valid training samples.", len(x))
    logger.info("Training Predictive RandomForestRegressor on real-world outcomes...")

    x_train, x_test, y_train, y_test = train_test_split(x, y, test_size=0.2, random_state=42)
    model = RandomForestRegressor(n_estimators=100, max_depth=12, random_state=42)
    model.fit(x_train, y_train)

    preds = model.predict(x_test)
    r2_score = model.score(x_test, y_test)
    mae = mean_absolute_error(y_test, preds)
    rmse = mean_squared_error(y_test, preds, squared=False)
    logger.info("Model metrics on test split: R^2=%.4f MAE=%.4f RMSE=%.4f", r2_score, mae, rmse)

    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model_dir = os.path.join(base_dir, "app", "ml", "models")
    os.makedirs(model_dir, exist_ok=True)

    model_path = os.path.join(model_dir, "trust_model_v1.pkl")
    tmp_model_path = f"{model_path}.tmp"

    try:
        joblib.dump(model, tmp_model_path)
        os.replace(tmp_model_path, model_path)
        logger.info("Model saved atomically to %s", model_path)
    except Exception as e:
        logger.error("Failed to save model atomically: %s", e)
        if os.path.exists(tmp_model_path):
            os.remove(tmp_model_path)


if __name__ == "__main__":
    extract_and_train()