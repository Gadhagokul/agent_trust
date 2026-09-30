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

### Supplier-specific Search-to-Book (L2B) — Sprint 2 (active rule)

The Search-to-Booking component is computed **per supplier** from the existing
tables only — no new configuration table is consumed.

- **Site-level benchmarks (senior §10-13):** each active supplier's target is
  `suppliers.minimum_booking / suppliers.search_limit`, read fresh on every score
  (no cache layer). `minimum_booking` never restricts searches; it only sets the
  L2B target. A supplier is *configured* only when both columns are present and
  `search_limit > 0`; otherwise it is **excluded** — the scorer never invents
  compliance for an unconfigured supplier (`l2b_not_configured_policy = "exclude"`).
- **Agent searches per supplier:** `SUM(search_session_accesses.access_count)`
  joined through `search_supplier_runs`, counting **created + reused** intents
  (both are the agent searching; excluding reuse would inflate L2B). Confirmed
  from real data: one session queries every supplier the agent selected, so the
  same access_count legitimately contributes to each participating supplier.
- **Bookings per supplier:** `bookings.provider = suppliers.name`.
- **Per-supplier scoring:** `ratio_s = bookings_s / searches_s` vs `target_s`
  through the existing curve. **Aggregation:** share-weighted (share = supplier
  search volume ÷ total configured volume) with `l2b_max_supplier_share`
  (default 0.5) capping any single supplier's influence; the excess is
  redistributed proportionally (senior §13).
- **Open item (business, not yet decided):** channel grouping
  (`l2b_group_by_channel = false`). The channel dimension is not present in the
  schema, so grouping stays OFF until the business defines it.

**Confirmed data relationships (Sprint 2):**
- **Agent** dimension: `search_session_accesses.agent_id`.
- **Supplier** dimension: `search_supplier_runs.supplier_id` / `supplier_code`
  (one row per supplier per search session — verified against real data).
- **Booking → supplier** link: `bookings.provider` = `suppliers.name`.
- **Channel** dimension (NDC / GDS) is **not present** in the current schema —
  grouping by channel stays OFF (`l2b_group_by_channel = false`).

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
| **Search-to-Booking** | 20% | Per-supplier search-to-book compliance vs site benchmarks (`suppliers.minimum_booking/search_limit`), last 365 days |
| **ML Calibration** | 20% of final | Future-risk model(s) — see ML Target Proposal below |

Final score = `(Composite × 0.8) + (ML × 0.2)`, capped at 100.

> **Sprint 5 (active design, all ML currently DISABLED):** when ML is `NOT_READY` the ML
> contribution is dropped and the final score is the rule composite alone
> (**§15.1**: `Final = round(Composite)`), never a "re-created today score". When all gates
> pass the final is `round(Composite × 0.8 + ML × 0.2)`.

### ML Target Proposal (Sprint 5 — business review)

The legacy ML calibration (a RandomForest trained on a heuristic pseudo-label that
recomputed *today's* score from the *same* signals as the rules) is being retired. It is
replaced by a **per-target future-risk** framework: each target is its own binary model
predicting the probability of a defined future event within a horizon window.

| Target | ML prediction |
|--------|---------------|
| `severe_default` | Probability the agent develops a severe payment/default event in the next horizon (a credit transaction ≥30 days past-due/unpaid, or a §27 high-risk condition) |
| `severe_reliability` | Probability the agent has a severe booking failure/cancellation event in the next horizon (≥50% of eligible attempts fail/cancel) |
| `l2b_breach` | Probability the agent breaches a supplier's L2B limit (any `ratio_s > target_s`) in the next horizon |

- **No leakage:** features use only data at/before `T`; labels come only from `(T, T + horizon]`.
- **Readiness gate per target:** no model runs/trains until its target has enough labeled
  positives/negatives (`ml_readiness_*` settings) and the datasets pass leakage validation.
- **Registry:** each target keeps its own immutable version registry with an explicit
  status (`PRODUCTION | CHALLENGER | REJECTED`); only a checksum-verified `PRODUCTION`
  artifact is ever loaded.
- **Two-switch approval model:** nothing trains, loads, or influences the live score until
  (1) business approves the target list + horizon (`ml_targets`), AND (2) the validated
  model is promoted AND `ml_enabled = true`.
- **Combiner (Option B):** when READY, the three per-target probabilities are converted to
  scores (`ml_risk_to_score_mode`, default `linear_inverse` = `100 − p`) and combined with
  the configured weights per target (`ml_risk_to_score_weights`; placeholder proposal
  `severe_default` 40 / `severe_reliability` 35 / `l2b_breach` 25). Weights are renormalized
  over the READY targets only.

**Open approvals requested from business/data-science:**
1. Target list + horizon(s) (30 / 60 / 90 days).
2. Combiner weights (proposal: 40 / 35 / 25) and the risk→score mapping (`100 − p`).

Until both are approved the service ships with `ml_targets = []` and `ml_enabled = false`
=> ML is always `NOT_READY` => `Final = round(Composite)`.

### Sprint 6 - Automated Training Lifecycle (all ML currently DISABLED)

Sprint 6 adds the controller that trains, evaluates, promotes, and rolls back per-target
models. It is **defined but disabled**: with `ml_enabled = false` (or `ml_targets = []`) the
supervisor is never started, no dataset is built, no registry write happens, and the live
score stays `Final = round(Composite)`.

**Execution paths (both call the same controller):**

| Path | Trigger | Notes |
|------|---------|-------|
| Supervisor (`app/main.py`) | Every `ml_training_poll_hours` while the app runs | Redis lock (`ml_training_lock_ttl_seconds`) guarantees a single trainer across workers/instances; work runs in `asyncio.to_thread` and the controller opens/closes its own DB session |
| CLI (`scripts/train_db_trust_model.py`) | On demand / cron | Useful for initial backfill before enabling the supervisor |

**Pipeline per target:** gate -> trigger -> readiness -> dataset (`T = as_of - horizon`,
features at/before `T`, labels in `(T, T+horizon]`) -> train (`RandomForestClassifier`,
temporal hold-out) -> evaluate -> validate -> verify SHA-256 -> promote/reject.

**Triggers** (a run with no trigger is `SKIPPED`/`NOT_RUN` and never counts as a success):

| Trigger | Rule |
|---------|------|
| `data_growth` | Eligible labeled records grew by `ml_trigger_increment` since the last successful run (never total bookings) |
| `interval` | `ml_training_interval_days` elapsed since the last successful training (first run is always due) |
| `drift` | **Deferred** this phase - reported as `NOT_EVALUATED`, never a trigger |

**Evaluation** is classification (binary default-risk), reported on a temporal hold-out:
ROC-AUC, PR-AUC, precision, recall, F1, Brier calibration, confusion matrix, and per-segment
recall. A candidate must clear `ml_classification_min_pr_auc`, `ml_classification_min_f1`,
`ml_classification_max_brier`, and must not regress any segment recall by more than
`ml_segment_max_recall_drop` versus the champion. These thresholds and the
`ml_promotion_headroom` margin are project defaults, not senior-specified values.

**Champion/challenger + rollback:** the challenger is promoted only if it beats the champion
by `ml_promotion_headroom`; otherwise the champion is kept and the candidate is stored as
`REJECTED` (auditable, never loaded). Promotion rotates the registry state so
`previous_known_good_production_version` always names the explicit prior production version -
rollback copies that artifact forward as a new version (`v003`, ...) and never guesses
`v{N-1}`.

**State and artifacts** live under `ml_model_registry_dir/<target>/`: `state.json`
(controller state: last successful run, labeled-record watermark, production/champion
version, per-segment recall, last outcome) and `vNNN/model.joblib` + `vNNN/metadata.json`
(status `PRODUCTION | CHALLENGER | REJECTED`, SHA-256, score, gate decisions).

**Metrics:** `agent_trust_training_runs_total`, `agent_trust_training_success_total`,
`agent_trust_training_failures_total`, `agent_trust_model_promotions_total`,
`agent_trust_model_rejections_total`, `agent_trust_model_rollbacks_total`.

### Tiers

### Search-to-Booking Component (Feature B)

- **Per-supplier benchmark**: `target_s = suppliers.minimum_booking / search_limit`
  per active supplier (site-level configuration — never derived per agent).
- **Agent searches**: `SUM(access_count)` per supplier from
  `search_session_accesses × search_supplier_runs`, 365-day window, created **and**
  reused both count. **Bookings** per supplier = `bookings.provider` =
  `suppliers.name`, confirmed/ticketed, same window.
- **Supplier score**: `ratio_s = bookings_s / searches_s` vs `target_s` on the
  baseline + progressive curve (anchors configurable via
  `SEARCH_TO_BOOKING_AT_TARGET_SCORE` and `SEARCH_TO_BOOKING_EXCELLENT_MULTIPLIER`):
  meeting (not exceeding) the target equals the minimum acceptable score
  (default **80**); rising linearly to **100** at `excellent_multiplier` × target
  (default **4×**). Below the target it ramps linearly toward 0.
- **Aggregation**: share-weighted mean of per-supplier scores, where share =
  supplier search volume ÷ total configured volume. No supplier may drive more
  than `l2b_max_supplier_share` (default 50%); the excess is redistributed
  proportionally. Example: suppliers at 80%/15%/5% search share scoring
  40/80/90 → **61.25**.
- Low-volume confidence: `min(1, searches / 20)` blends the base toward neutral
  80; 0 bookings at high volume → 0. `SUM(access_count)` semantics mean a small
  number of accesses can never read as perfect conversion.
- **No configured supplier / no search data** in 365 days → component excluded
  (`null`) and the composite recomputes over the remaining active weights
  (senior §5).
- Audit metadata records `l2b_component`, `l2b_policy`, and
  `l2b_unconfigured_suppliers`.
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
- [x] Monitor `/metrics` in your observability stack
- [ ] Set up CI/CD via the provided GitHub Actions workflows

## Observability

Prometheus metrics are exposed at `/metrics` (bearer-gated by `METRICS_TOKEN`).
HTTP and cache series label by **route template** (e.g. `/v1/trust-score/{agent_id}`),
never raw URLs or query strings; requests that match no route are labeled `unmatched`.

The operator-focused manual lives in
[`docs/OPS_RUNBOOK.md`](docs/OPS_RUNBOOK.md): architecture, configuration,
startup, failure modes, recovery and rollback.

Key series:

- `agent_trust_http_requests_total{status,method,route}` — HTTP volume and errors
  by route template; `agent_trust_http_duration_seconds` — overall latency.
- `agent_trust_scoring_duration_ms` — scoring latency (kept for dashboard
  back-compat; observed in seconds so the default buckets are correct).
- `agent_trust_requests_total{status}` — legacy service-tone counter (unchanged).
- `agent_trust_cache_{hits,misses,stale_hits,invalidations,errors}_total` — the
  two-tier cache. `cache_stale_hits` fires whenever a 24h stale fallback was
  served because the DB was unavailable or its schema drifted.
- `agent_trust_database_failures_total`, `agent_trust_schema_drift_total`,
  `agent_trust_webhooks_total{outcome}`, `agent_trust_ratelimit_rejections_total`.
- ML Target Programme and training-lifecycle counters (Sprint 5/6, unchanged).

Each HTTP request also emits one structured JSON line on logger
`agent_trust.access` (fields: `request_id`, `method`, `path`, `status`,
`duration_ms`). Ordinary service logs are unchanged; access telemetry never
includes client IPs.

Alert candidates: rising 5xx rate per route, `cache_stale_hits` rate > 0
(degraded mode), spikes in `webhooks_total{outcome="invalid_secret"}` or
`ratelimit_rejections_total`, and any `schema_drift_total` increase.

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
