# Agent Trust Score

A **standalone FastAPI microservice** that evaluates the creditworthiness, reliability, and trust score of B2B sales agents for an airline & hotel booking platform.

> **Architecture**: Your **React** frontend calls this API directly. Your **Laravel** backend manages the MySQL database (agents, bookings, credits, etc.). This service **reads** from that DB read-only and **writes** audit logs back.

---

## Quick Start

### Prerequisites

- Python 3.10+
- Redis 7+
- Read-only MySQL access to the Laravel database

### 1. Clone & Install

```bash
pip install -r requirements.txt
```

### 2. Configure

Copy `.env` and fill in your credentials:

```env
APP_ENV=development

DB_HOST=127.0.0.1
DB_PORT=3306
DB_DATABASE=your_laravel_db
DB_USERNAME=your_user
DB_PASSWORD=your_password

REDIS_URL=redis://127.0.0.1:6379/0
API_KEY=your-production-api-key
```

### 3. Run

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Visit `http://localhost:8000/docs` for the Swagger UI.

---

## Docker

```bash
docker compose up --build
```

This starts:
- `agent-trust-api` on port **8000**
- `agent-trust-redis` on port **6379**

---

## API Reference

### `GET /v1/trust-score`

Calculate and retrieve the trust score for an agent.

**Request:**
```http
GET /v1/trust-score?agent_id=123
X-API-Key: your-api-key
```

**Response:**
```json
{
  "agent_id": 123,
  "agent_name": "Acme Travels",
  "tier": "Gold",
  "badges": ["Perfect Payer"],
  "high_risk_flag": false,
  "high_risk_reasons": [],
  "scores": {
    "reliability_score": 92.5,
    "financial_score": 88.0,
    "experience_score": 67.3,
    "composite_trust_score": 85.1,
    "ml_calibration_score": 79.4,
    "overall_score": 84
  },
  "features": { "...conversion metrics by time window..." },
  "calculated_at": "2026-06-10T11:00:00"
}
```

### `GET /v1/health/live`

Liveness probe. Returns `{"status": "ok"}`.

### `GET /v1/health/ready`

Readiness probe — checks Redis connectivity and DB schema integrity.

### `POST /v1/webhooks/domain-event`

Invalidates the agent's cache so the next score fetch is recalculated.

```json
{
  "agent_id": 123,
  "event_type": "booking_created",
  "event_data": {}
}
```

Event types: `booking_created`, `booking_cancelled`, `credit_issued`, `credit_defaulted`, `credit_repaid`.

### `GET /metrics`

Prometheus metrics endpoint (exposed by `prometheus-fastapi-instrumentator`). Standard request duration, request count, error rate, etc.

---

## Laravel Integration

This service reads from the Laravel-maintained MySQL database. The **required schema** is documented in `app/infra/db/schema_guard.py`.

### Required Tables

| Table | Purpose |
|-------|---------|
| `agents` | Agent identity, `establishment_name`, `created_at` |
| `credit_transactions` | Credit history, due dates, payments |
| `bookings` | Booking records with status and amounts |
| `search_sessions` | Agent search volume signals |
| `booking_process` | Booking funnel step tracking |

### Optional: Webhook Integration

For real-time cache invalidation, configure your Laravel backend to call `POST /v1/webhooks/domain-event` whenever:

- A booking is created or cancelled → `booking_created` / `booking_cancelled`
- Credit is issued, repaid, or defaulted → `credit_issued` / `credit_repaid` / `credit_defaulted`

This is **not required** (the cache auto-expires every 5 minutes) but it keeps scores up-to-date faster.

### Audit Table

The `agent_score_audits` table must be pre-created in MySQL. This service writes score change records to it.

```sql
CREATE TABLE agent_score_audits (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    agent_id    BIGINT NOT NULL,
    old_score   DECIMAL(5,2),
    new_score   DECIMAL(5,2),
    score_delta DECIMAL(5,2),
    old_tier    VARCHAR(20),
    new_tier    VARCHAR(20),
    event_type  VARCHAR(50),
    metadata    JSON,
    created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_agent_id (agent_id)
);
```

---

## React Frontend Integration

```typescript
const TRUST_API = "https://trust-score.yourdomain.com";

async function getAgentTrustScore(agentId: number) {
  const res = await fetch(`${TRUST_API}/v1/trust-score?agent_id=${agentId}`, {
    headers: { "X-API-Key": process.env.REACT_APP_TRUST_API_KEY },
  });
  if (!res.ok) throw new Error(`Trust score fetch failed: ${res.status}`);
  return res.json();
}
```

**CORS**: Set `CORS_ORIGINS` in `.env` to your React app's domain (e.g., `https://app.yourdomain.com`). Use `*` for development.

**Environment variables for React**:
```
REACT_APP_TRUST_API_URL=https://trust-score.yourdomain.com
REACT_APP_TRUST_API_KEY=your-api-key
```

---

## Scoring Overview

| Component | Weight | Description |
|-----------|--------|-------------|
| **Reliability** | 50% | Booking success rate (45%), cancellation quality (25%), refunds (15%), supplier (10%), SLA (5%) |
| **Financial** | 30% | Unpaid ratio + payment delay penalty |
| **Experience** | 20% | Account tenure + lifetime booking volume |
| **ML Calibration** | 20% of final | RandomForest overlay trained on historical repayment data |

Final score = `(Composite × 0.8) + (ML × 0.2)`, capped at 100.

### Tiers

| Tier | Score | Condition |
|------|-------|-----------|
| Platinum | ≥ 85 | Zero unpaid, delay < 5 days |
| Gold | ≥ 70 | Good standing |
| Silver | ≥ 45 | Moderate standing |
| Bronze | < 45 | Below average |
| High Risk | varies | Extreme debt signals or score < 35 |

---

## Production Checklist

- [ ] Change `API_KEY` from the default
- [ ] Set `APP_ENV=production`
- [ ] Configure `CORS_ORIGINS` with your React app's domain
- [ ] Run behind a reverse proxy (nginx, Caddy) for TLS termination
- [ ] Ensure MySQL credentials are read-only (SELECT only, no INSERT/UPDATE/DELETE outside `agent_score_audits`)
- [ ] Monitor `/metrics` in your observability stack
- [ ] Set up CI/CD via the provided GitHub Actions workflows

---

## Development

```bash
# Install dev dependencies
make dev

# Lint & typecheck
make lint
make typecheck

# Run tests
make test

# Run locally
make run
```

## Project Structure

```
├── app/
│   ├── main.py                  # FastAPI app entry point
│   ├── api/v1/                  # API routes and schemas
│   ├── domain/                  # Domain models, errors, audit ORM
│   ├── infra/                   # DB session, Redis, settings, schema guard
│   ├── ml/                      # ML model inference
│   ├── observability/           # Logging, request context
│   ├── security/                # Auth, rate limiting
│   └── services/                # Scoring engine, cache adapter
├── Dockerfile
├── docker-compose.yml
├── .github/workflows/           # CI/CD pipelines
└── tests/                       # Unit & integration tests
```
