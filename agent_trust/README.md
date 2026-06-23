# 🏦 Agent Trust Score System

An enterprise-grade microservice and dashboard for evaluating the creditworthiness and trustworthiness of sales agents based on their credit transaction history.

## 🏗️ Architecture overview

This project operates on a **Read-Only** architecture, pulling transaction data from an actively changing, third-party MySQL database. To ensure stability against upstream schema changes, the application employs a highly resilient, two-tier cache with a defensive schema guard.

### Tech Stack

- **Backend:** FastAPI (Python 3.10+)
- **Frontend:** Streamlit 
- **Database Access:** SQLAlchemy 2.0 (Read-Only MySQL connection)
- **Caching & Rate Limiting:** Redis
- **Data Validation:** Pydantic v2

---

## ⚙️ Core Business Logic: The 7-Day Window

The trust scoring logic (`AgentTrustScorer`) operates on a strict **7-day maximum repayment window** rule. 

When an admin issues credit to an agent, a `due_date` is set (Credit Date + 7 days max). 

The score heavily penalizes agents who exceed this window and rewards agents who pay quickly.

### Scoring Bands (`avg_delay_days`)
* Delay is calculated as `DATEDIFF(payment_date, due_date)`
* **Negative** = Paid before 7-day due date (Rewarded)
* **Positive** = Paid after 7-day due date (Penalized)
* **NULL** = Unpaid (Severely penalized as 30+ days)

| Tier | Requirement | Default Tolerance |
|---|---|---|
| **🥇 PLATINUM** | Score ≥ 85 | 0 defaults, consistently pays early |
| **🥈 GOLD** | Score ≥ 70 | Max 1 default, average delay ≤ 2 days |
| **🥉 SILVER** | Score ≥ 45 | Max 1 default, moderate late payments |
| **🟫 BRONZE** | Score < 45 | 2+ defaults, or severely late |

*Note: Requires a minimum of 3 credit transactions to generate a score.*

---

## 🛡️ Resilience & Schema Drift Protection

Because the MySQL database belongs to another team and changes frequently, this application implements rigorous defensive measures to prevent crashes:

1. **Schema Guard (`schema_guard.py`)** 
   - Queries `INFORMATION_SCHEMA` to verify required tables (`agents`, `credit_transactions`) and exact columns still exist.
   - Tied directly to the `/health/ready` endpoint to instantly report if the source team broke the schema.
2. **Two-Tier Cache Fallback**
   - **Primary Cache (5 MIN TTL):** Standard fast reads.
   - **Stale Cache (24 HR TTL):** If a `SchemaChangedError` or `DatabaseUnavailableError` occurs during a live query, the app gracefully degrades to serve the 24-hour stale cache instead of throwing a 500 server error.
3. **Repository Hardening**
   - All SQL execution is wrapped. `ProgrammingError` (renamed columns) and `OperationalError` (downed DBs) are isolated and converted to clean HTTP 503 responses.

---

## 🚀 Running the Project

### Prerequisites
- Python 3.10+
- A running Redis instance
- Read-only access to the external MySQL database

### 1. Installation

```bash
pip install -r requirements.txt
```

### 2. Environment Configuration

Create a `.env` file in the root directory:

```env
APP_ENV=development
LOG_LEVEL=INFO
API_KEY=your-secure-api-key

# Database (External, Read-Only)
DB_HOST=127.0.0.1
DB_PORT=3306
DB_DATABASE=agent_db
DB_USERNAME=readonly_user
DB_PASSWORD=secret

# Redis
REDIS_URL=redis://127.0.0.1:6379/0
```

### 3. Start the FastAPI Backend

```bash
uvicorn app.main:app --host 0.0.0.1 --port 8000 --reload
```
* The API will be available at `http://localhost:8000`
* Swagger Docs available at `http://localhost:8000/docs`

### 4. Start the Streamlit Dashboard

In a separate terminal, start the UI:

```bash
streamlit run streamlit_app/main.py
```
* The Dashboard will be available at `http://localhost:8501`

---

## 📡 API Endpoints

### `GET /v1/trust-score`
Computes and retrieves the trust score and tier for an agent.
**Params:** `?agent_id=123`
**Auth:** Requires `X-API-Key` header (unless in `APP_ENV=development`)

### `GET /health/live`
Simple liveness check for Kubernetes/AWS.

### `GET /health/ready`
Deep readiness check. Validates that Redis is connected **and** that the exact expected schema still exists in the external MySQL database. If schema drift is detected, returns HTTP 200 but with `"status": "degraded"` detailing the missing columns.
