#agent_trust\scripts\train_db_trust_model.py
import os
import joblib
import numpy as np
import logging
from datetime import date
from sqlalchemy import text
from sklearn.ensemble import RandomForestRegressor
from sklearn.model_selection import train_test_split

# Setup Django/FastAPI-like environment paths
import sys
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.infra.db.session import SessionLocal
from app.infra.db.repository import AgentRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

def _calculate_real_world_outcome(db, agent_id) -> float:
    """
    Computes a ground-truth reliability score (5.0 to 100.0) based on actual
    repayment delays, outstanding defaults, and technical integration failures.
    """
    # 1. Fetch repayment history
    tx_rows = db.execute(text("""
        SELECT status, due_date, payment_date, principal_amount 
        FROM credit_transactions 
        WHERE agent_id = :agent_id
    """), {"agent_id": agent_id}).fetchall()
    
    score = 100.0
    
    for row in tx_rows:
        status, due_date, payment_date, principal_amount = row
        
        # Convert to date objects to avoid datetime vs date type comparison errors
        d_date = due_date.date() if hasattr(due_date, 'date') else due_date
        p_date = payment_date.date() if hasattr(payment_date, 'date') else payment_date
        
        if status == 'paid':
            if p_date and d_date and p_date > d_date:
                delay = (p_date - d_date).days
                score -= min(delay * 1.5, 25.0)  # Penalty for past late payments
        else:
            # Currently unpaid
            today = date.today()
            if d_date and today > d_date:
                delay = (today - d_date).days
                score -= min(delay * 2.0, 40.0)  # Extreme penalty for overdue delay
                score -= 10.0                     # Flat outstanding default penalty
                
    # 2. Extract integration / search-abuse metrics
    stats_30d = db.execute(text("""
        SELECT 
            (SELECT COUNT(*) FROM search_sessions WHERE agent_id = :agent_id AND created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)) as searches,
            (SELECT COUNT(*) FROM bookings WHERE agent_id = :agent_id AND status NOT IN ('pending', 'failed', 'cancelled', 'rejected') AND created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)) as bookings
    """), {"agent_id": agent_id}).fetchone()
    
    if stats_30d:
        searches, bookings = stats_30d
        searches = searches or 0
        bookings = bookings or 0
        if searches > 50 and bookings == 0:
            score -= 20.0  # Search scraper / invalid integration penalty
            
    # 3. Technical failures in booking steps
    fails = db.execute(text("""
        SELECT COUNT(*) 
        FROM booking_process bp
        JOIN agents a ON a.user_id = bp.user_id
        WHERE a.id = :agent_id AND bp.state = 'FAILED' AND bp.created_at >= DATE_SUB(CURDATE(), INTERVAL 30 DAY)
    """), {"agent_id": agent_id}).scalar() or 0
    
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
        logger.error(f"Failed to fetch agents from database: {e}")
        db.close()
        return

    logger.info(f"Found {len(agent_ids)} agents. Extracting historical features and calculating ground-truth outcomes...")
    
    X_list = []
    y_list = []
    
    for agent_id in agent_ids:
        try:
            # 1. Calculate the REAL outcome target label
            real_outcome = _calculate_real_world_outcome(db, agent_id)
            
            # 2. Fetch Features
            batch_stats = repo.get_multi_timeframe_stats(db, agent_id)
            credit_stats = repo.get_credit_stats(db, agent_id)
            
            stats_7d = batch_stats.get(7, {})
            stats_30d = batch_stats.get(30, {})
            
            # 3. Assemble matching feature vector
            feature_vector = [
                stats_7d.get("effective_searches", 0),
                stats_7d.get("bookings", 0),
                stats_30d.get("effective_searches", 0),
                stats_30d.get("bookings", 0),
                credit_stats.get("unpaid_count", 0),
                credit_stats.get("unpaid_ratio", 0.0),
                credit_stats.get("current_credit_delay_days", 0)
            ]
            
            X_list.append(feature_vector)
            y_list.append(real_outcome)
            
        except Exception as e:
            logger.warning(f"Failed to extract data for agent {agent_id}: {e}")
            continue

    db.close()

    if not X_list:
        logger.error("No training data could be extracted.")
        return

    X = np.array(X_list)
    y = np.array(y_list)
    
    logger.info(f"Successfully extracted {len(X)} valid training samples.")
    logger.info("Training Predictive RandomForestRegressor on real-world outcomes...")
    
    # Train the model (Since we are using all agents, we will have a robust 1,000 samples!)
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)
    model = RandomForestRegressor(n_estimators=100, max_depth=12, random_state=42)
    model.fit(X_train, y_train)
    
    score = model.score(X_test, y_test)
    logger.info(f"Predictive Model R^2 Score on test split: {score:.4f}")
    
    # Save the model atomically using Option A (Atomic Model Swap)
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    model_dir = os.path.join(base_dir, "app", "ml", "models")
    os.makedirs(model_dir, exist_ok=True)
    
    model_path = os.path.join(model_dir, "trust_model_v1.pkl")
    tmp_model_path = f"{model_path}.tmp"
    
    try:
        # Save to temporary file first
        joblib.dump(model, tmp_model_path)
        # Atomically rename to target path to prevent corrupted reads/race conditions
        os.replace(tmp_model_path, model_path)
        logger.info(f"Autonomous Predictive Model successfully saved atomically to {model_path}!")
    except Exception as e:
        logger.error(f"Failed to save model atomically: {e}")
        if os.path.exists(tmp_model_path):
            os.remove(tmp_model_path)

if __name__ == "__main__":
    extract_and_train()
