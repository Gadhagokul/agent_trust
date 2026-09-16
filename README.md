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
LARAVEL_SERVICE_TOKEN=<random-token-shared-with-laravel>
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
Authorization: Bearer <service-token>
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
    "search_to_booking_score": 100.0,
    "composite_trust_score": 85.1,
    "ml_calibration_score": 79.4,
    "overall_score": 84
  },
  "features": { "...conversion metrics by time window...", "search_activity": { "created": 8, "reused": 4, "searches": 12, "bookings": 3 } },
  "calculated_at": "2026-06-10T11:00:00"
}
```

> `search_to_booking_score` is `null` (and the composite is recomputed over the remaining weights) when the agent has **no search behavior data** in the last 365 days.

### `GET /v1/health/live`

Liveness probe. Returns `{"status": "ok"}`.

### `GET /v1/health/ready`

Readiness probe — checks Redis connectivity and DB schema integrity.

### `GET /v1/suppliers/quota-status`

Site-wide supplier request quota (decision layer, **Feature A**). Returns the
current created-search request count per supplier vs its `search_limit`
(`suppliers` table in the Laravel DB).

```http
GET /v1/suppliers/quota-status?period_type=monthly&days=30
```

Optional query params: `period_type` (`lifetime|daily|monthly|rolling`,
default `lifetime`) and `days` (window size for `rolling`, default 30).

**Response:**
```json
{
  "period_type": "lifetime",
  "period_days": 30,
  "computed_at": "2026-09-09T10:00:00Z",
  "suppliers": [
    {
      "code": "EMIRATES",
      "name": "Emirates",
      "is_active": true,
      "health_status": "healthy",
      "search_limit": 5000,
      "minimum_booking": 250,
      "consumed": 3100,
      "remaining": 1900,
      "status": "available",
      "available_to_search": true
    }
  ]
}
```

The Laravel search service calls this **before** issuing a supplier request and
proceeds only when `available_to_search` is `true`. Statuses:
`unused`, `available`, `exhausted`, `monitoring_only` (inactive supplier).
`consumed` counts **created** search sessions only (`first_access_type = 'created'`);
reused/cached sessions never consume quota. A single search session across
6 suppliers counts 1 per supplier, not 1 per run.

### `GET /v1/suppliers/{code}/quota-status`

Same shape, filtered to a single supplier code (case-insensitive). `404` for
unknown codes, `400` for invalid `period_type`.

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
| `agents` | Agent identity, `establishment_name`, `created_at`, `user_id`, `is_active`, `approval_status` |
| `users` | Laravel user accounts; source of `X-User-Id` |
| `roles` + `model_has_roles` | Role assignment used to determine admin vs agent |
| `credit_transactions` | Credit history, due dates, payments |
| `bookings` | Booking records with status and amounts |
| `search_sessions` | Agent search volume signals (existing Reliability/ML input) |
| `search_session_accesses` | Search intent per session (`search_session_id`, `first_access_type = created`/`reused`, `first_accessed_at`, `agent_id`) — the additive Search-to-Booking input |
| `search_supplier_runs` | Supplier request runs (`supplier_id`, `search_session_id`, `status`, timestamps) — 1 row per supplier per search; quota consumption is `COUNT(DISTINCT search_session_id)` |
| `suppliers` | Supplier catalog: `code`, `name`, `is_active`, `health_status`, `search_limit` (quota cap), `minimum_booking` (S2B target only) |
| `booking_process` | Booking funnel step tracking |

### Authentication & Authorization

Every request must present the shared **service token** as `Authorization: Bearer <token>`. This token is the same value in the Laravel `.env` and the FastAPI `.env` (`LARAVEL_SERVICE_TOKEN`); it authenticates the *Laravel service*, not a user login. Laravel attaches it to every request; missing or wrong token → **401**.

| Header | Required | Description |
|--------|----------|-------------|
| `Authorization: Bearer <token>` | yes | The shared service token (`LARAVEL_SERVICE_TOKEN`) |
| `X-User-Id` | for identity endpoints | The Laravel user's id (e.g. `auth()->id()`) |
| `X-Agent-Id` | no (verified) | Agent id; FastAPI cross-checks it against `agents.user_id` |

- **Roles are resolved by this service** from `users → model_has_roles → roles`, never accepted from the client.
- The **role is admin if the user has *any* admin role**, otherwise agent.
- **Agents** can only read their own trust score (`GET /v1/me/trust-score`, or `?agent_id=<own id>`).
- **Admins** can read any agent's score and list them via `GET /v1/admin/trust-scores?q=&page=&page_size=`.
- A request presenting only the service token (no `X-User-Id`) is treated as a legacy admin-level caller **for `GET /v1/trust-score` only**.
- `GET /v1/admin/trust-scores` always requires an identity: **401** without `X-User-Id`, **403** if the user isn't an admin, **200** with an admin `X-User-Id`.
- `POST /v1/webhooks/domain-event` additionally requires `X-Webhook-Secret` (payload-level authenticity from core services).

**Laravel side**: set the same token in Laravel's `.env` (`LARAVEL_SERVICE_TOKEN=<same value>`) and send the header on every request:

```php
Http::withToken(config('services.trust_service_token'))->get('http://127.0.0.1:8000/v1/me/trust-score', [...]);
```

**Local testing:** the local seed DB has no `roles`/`model_has_roles` tables, so in dev mode add the optional `X-User-Role: admin` header (ignored in prod) to simulate admin:
```bash
curl -H "Authorization: Bearer <service-token>" -H "X-User-Id: 1" -H "X-User-Role: admin" \
  "http://127.0.0.1:8000/v1/admin/trust-scores?q=muhriz"
```

Curl example (agent):
```bash
curl "https://trust-score.yourdomain.com/v1/me/trust-score" \
  -H "Authorization: Bearer <service-token>" \
  -H "X-User-Id: 42" \
  -H "X-Agent-Id: 7"
```

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
    headers: { "Authorization": `Bearer ${process.env.REACT_APP_TRUST_API_TOKEN}` },
  });
  if (!res.ok) throw new Error(`Trust score fetch failed: ${res.status}`);
  return res.json();
}
```

**CORS**: Set `CORS_ORIGINS` in `.env` to your React app's domain (e.g., `https://app.yourdomain.com`). Use `*` for development.

**Environment variables for React**:
```
REACT_APP_TRUST_API_URL=https://trust-score.yourdomain.com
REACT_APP_TRUST_API_TOKEN=<service-token>
```

---

## Scoring Overview

| Component | Weight | Description |
|-----------|--------|-------------|
| **Reliability** | 40% | Booking success rate (64.3%), cancellation quality (35.7%) — no evidence → component excluded |
| **Financial** | 25% | Unpaid ratio + payment delay penalty |
| **Experience** | 15% | Account tenure + lifetime booking volume |
| **Search-to-Booking** | 20% | Created + reused search intents vs confirmed/ticketed bookings, last 365 days |
| **ML Calibration** | 20% of final | RandomForest overlay trained on historical repayment data |

Final score = `(Composite × 0.8) + (ML × 0.2)`, capped at 100.

### Search-to-Booking Component (Feature B)

- **Searches** = `COUNT(DISTINCT search_session_id)` across created **and** reused
  search intents (`search_session_accesses`) in the last 365 days.
- **Bookings** = confirmed/ticketed bookings, same 365-day boundary.
- **Target ratio** is derived from the DB, not hardcoded: `Σ minimum_booking / Σ search_limit`
  over active suppliers (~0.05, i.e. 1 booking per 20 searches). `minimum_booking`
  never blocks searches — it only sets this target.
- Score uses a baseline + progressive curve (both anchors configurable via
  `SEARCH_TO_BOOKING_AT_TARGET_SCORE` and `SEARCH_TO_BOOKING_EXCELLENT_MULTIPLIER`):
  meeting (not exceeding) the target ratio equals the minimum acceptable score
  (default **80**); the score rises linearly to **100** at `excellent_multiplier` ×
  the target ratio (default **4×** = 20%). Below the target it ramps linearly
  from 0 up to the acceptable score.
- Low-volume confidence: the base score is blended toward neutral 80 by
  `min(1, searches / 20)`; 0 bookings at high volume → 0.
- Example (33 searches, target 5%): 0 bookings → 0, 1 → 48.5, 2 (=minimum) → 81.4,
  3 → 85.5, 4 → 89.5, 5 → 93.5, 6 → 97.6, 7+ → 100.
- **No search data** in 365 days → component excluded (`null`) and the composite
  is recomputed over the remaining active weights (fully additive design).
- Rollback: `SEARCH_TO_BOOKING_AT_TARGET_SCORE=100` +
  `SEARCH_TO_BOOKING_EXCELLENT_MULTIPLIER=1` reproduces the pre-curve behavior.

### Tiers

| Tier | Score | Condition |
|------|-------|-----------|
| Platinum | ≥ 80 | Zero unpaid, delay < 5 days |
| Gold | ≥ 65 | Good standing |
| Silver | ≥ 50 | Moderate standing |
| Bronze | ≥ 35 | Below average |
| High Risk | < 35 | Extreme debt signals |

---

## Production Checklist

- [ ] Generate a long random service token and set the **same** `LARAVEL_SERVICE_TOKEN` in both the Laravel and FastAPI `.env` files
- [ ] Set `APP_ENV=production`
- [ ] Configure `CORS_ORIGINS` with your React app's domain
- [ ] Run behind a reverse proxy (nginx, Caddy) for TLS termination
- [ ] Ensure MySQL credentials are read-only (SELECT only, no INSERT/UPDATE/DELETE outside `agent_score_audits`)
- [ ] Monitor `/metrics` in your observability stack
- [ ] Set up CI/CD via the provided GitHub Actions workflows

## Streamlit Dashboard

A single-page, light-themed viewer (`streamlit_app.py`) that calls
`GET /v1/trust-score?agent_id=<id>` and renders the full transparent scoring
breakdown. It only displays API results — trust scores are always computed by
the backend. The Supplier Quota feature lives in the API and is intentionally
not surfaced here.

```bash
streamlit run streamlit_app.py
```

Configuration comes from an optional `.env` next to the app:

```env
API_BASE_URL=http://127.0.0.1:8000
TRUST_API_TOKEN=<service-token>
```

On fetch, the page shows:

- Agent info box and a 🔵/🟢/🟡/🟣 tier badge (with `🚨 SEVERE WARNING` when `high_risk_flag`).
- **Transparent Trust Score Breakdown**: Reliability (40%), Financial (25%),
  Experience (15%), Search-to-Booking (20%) — the S2B card shows `N/A` when the
  agent has no search data (composite is then renormalized over the remaining
  components).
- **Overall Trust Score**: Composite, ML Calibration, and the Final `/100`.
- **Search-to-Booking Conversions** tabs (Daily / Weekly / Monthly /
  Yearly-Lifetime): total searches, capped BookStep system failures (+ raw),
  agent failures, effective searches, bookings, revenue output, avg value &
  stability.
- **Current Credit & Default Signals**: current delay, unpaid credits,
  default rate, and the `Calculated at` timestamp.

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
