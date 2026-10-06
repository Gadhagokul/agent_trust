# Agent Trust Score API — Operations Runbook

Operator-facing manual for the Agent Trust Score API. It documents how the
service behaves in production, how to operate it, and how to respond when
things go wrong. It describes **actual shipped behaviour** only: every metric,
endpoint, default and failure path below matches the code in this repository.

---

## 1. Service overview & architecture

The service is a read-only scoring API. It never writes to the business
database; it reads it and caches the derived scores in Redis.

| Component | Role | Notes |
|---|---|---|
| FastAPI app | Serves scoring, webhook and admin endpoints | `app/main.py` |
| External Dummy Afine MySQL | Source of truth for bookings, credit, searches, suppliers | **Read-only**; Laravel-owned iframes/owned schema |
| Redis 7 | Two-tier score cache, sliding-window rate limiting, ML training lock | Local dev: `redis://`; prod requires TLS `rediss://` |
| Prometheus `/metrics` | Operational telemetry | Bearer-gated (see §5) |

### Callers

- **Laravel backend** — calls `GET /v1/trust-score` with
  `Authorization: Bearer <LARAVEL_SERVICE_TOKEN>`.
- **Core services** (Booking Engine, Credit Engine) — call
  `POST /v1/webhooks/domain-event` with the `X-Webhook-Secret` header to
  invalidate a cached score when an agent's state changes.
- **Admins / agents** — authenticated by `X-User-Id` identity headers; admin
  routes additionally require the admin identity. See §4.
- **Prometheus** — scrapes `GET /metrics` with `Authorization: Bearer <METRICS_TOKEN>`.

### Two-tier score cache

Every score is stored twice in Redis (`app/services/cache_adapter.py`):

- **Primary** — TTL 300 s (5 min). Served on every request.
- **Stale** — TTL 86400 s (24 h). Served **only** when the external DB is
  unreachable or its schema has drifted.

A domain-event webhook removes **both** tiers in one atomic `DEL`
(`cache_adapter.py:79-89`), so a stale fallback never outlives a declared
state change. All Redis errors are swallowed and logged — the cache can never
crash the application.

**No in-process tier.** Redis is the only cache; there is no local memory
layer in front of it.

**Cache key:** `trust:agent:{agent_id}:conversion`. The key does **not** include
a configuration or scoring-weight fingerprint. A scoring-rule or weight change is
therefore not visible to the cache by itself — it takes effect only when the
300 s primary TTL expires or a webhook invalidates the entry. When changing
weights or scoring rules, either wait out the primary TTL or trigger an
invalidation; do not assume the new rule applies immediately.

---

## 2. Configuration reference

All settings come from environment variables (`.env`, `app/infra/settings.py`).

### Required in every environment

| Variable | Purpose | Guard |
|---|---|---|
| `DB_HOST`, `DB_PORT`, `DB_DATABASE`, `DB_USERNAME`, `DB_PASSWORD` | Business database connection | Must all be set (`_validate_startup`) |
| `LARAVEL_SERVICE_TOKEN` | Bearer token shared with Laravel | ≥ 32 chars; non-placeholder (`change-me-`/`your-`/`test-`) |

### Required in production/staging

| Variable | Purpose | Guard |
|---|---|---|
| `CORS_ORIGINS` | Comma-separated allowed browser origins | Must be non-empty in prod/staging |
| `WEBHOOK_SECRET` | Verified against `X-Webhook-Secret` on domain-event webhooks | Must be set in prod/staging |
| `METRICS_TOKEN` | Bearer token guarding `GET /metrics` | Must be set in prod/staging |
| `REDIS_URL` | Redis DSN | Scheme must be `redis://` or `rediss://`; must include a host. **Use `rediss://` (TLS) in production.** |

### Frequently tuned values

| Variable | Default | Meaning |
|---|---|---|
| `RATE_LIMIT_PER_MINUTE` | `120` | Requests/min/key allowed by the sliding-window limiter |
| `RATE_LIMITER_BACKEND` | `redis` | `redis` (shared, recommended) or `memory` (per-process). See §6.3. |
| `LOG_LEVEL` | `INFO` | Root logging level |
| `AUDIT_LOG_PATH` | `logs/agent_score_audits.log` | Audit trail file |
| `AUDIT_TAIL_READ_BYTES` | `262144` | Max bytes read from the audit tail for previous-score lookup |
| `AUDIT_ROTATE_MAX_BYTES` | `10485760` | Rotate active log past this size |
| `AUDIT_BACKUP_COUNT` | `3` | Rotated generations retained |

### Scoring/ML knobs (config-only, change published scores)

Thresholds, tier boundaries, composite weights, confidence switches and all
ML switches (`ml_enabled`, `ml_targets`, trigger/interval/lock settings) are
configuration, not code. Changing them changes published scores or enables
ML behaviour — treat as a controlled change (see §7). The ML Target Programme
**ships disabled**: with the defaults nothing trains, loads, blends or serves.

### High-Risk thresholds (senior review §8.2)

High Risk is a hard cap, not a re-scoring: when any threshold below is breached
the overall (and operational) score is **capped at `high_risk_score_cap`** and
the sentinel tier is returned regardless of the composite score
(`_check_high_risk` — `agent_trust_scorer.py:157-183`, applied at
`agent_trust_scorer.py:687-701`).

| Setting | Default | Trigger condition |
|---|---|---|
| `credit_max_overdue_ratio` | `50.0` | `current_overdue_ratio > 50%` (overdue / total credit lines) |
| `credit_max_delay_days` | `60` | `current_max_delay_days > 60` (longest single payment delay) |
| `credit_max_overdue_count` | `10` | `current_overdue_count >= 10` outstanding past-due invoices |
| `credit_max_consecutive_overdue_cycles` | `3` | defaulted the last `3` consecutive credit cycles |
| `credit_overdue_boundary` | `<` | Whether a due-today invoice counts as overdue; only `"<"` (due today NOT yet overdue — recommended) or `"<="` are accepted |
| `high_risk_score_cap` | `30` | Effective score cap while High Risk |

All are config-only (see §7 — a threshold change is a controlled config change
that alters published scores). Each breached trigger is recorded in
`high_risk_reasons` on the result and audit line.

### Secret generation

Generate each secret once and store it in the deployment's secret manager:

```
python -c "import secrets; print(secrets.token_urlsafe(48))"   # LARAVEL_SERVICE_TOKEN
python -c "import secrets; print(secrets.token_urlsafe(32))"   # WEBHOOK_SECRET / METRICS_TOKEN
```

`LARAVEL_SERVICE_TOKEN` must be the **same** value in this API and in the
Laravel `.env`.

---

## 3. Startup sequence

On boot (`app/main.py` lifespan, `:122-164`), in order:

1. `configure_logging()` — structured JSON logging to stdout.
2. `settings._validate_startup()` — rejects missing/weak tokens, incomplete DB
   settings, unset prod/staging gates, invalid Redis URL and invalid
   thresholds/weights. **Refuses to boot** on any violation.
3. **Schema fail-fast (production/staging only)** — `_run_startup_schema_check()`
   compares the external DB against `REQUIRED_SCHEMA`; on drift it raises
   `RuntimeError("Database schema drift on startup: ...")` and **the process
   exits** (`main.py:29-47`). Development/local/test keep a DB-optional boot.
4. ML training supervisor — created **only** when `ml_enabled` is true **and**
   `ml_targets` is non-empty (`main.py:50-56`). With the shipped defaults it is
   not created and no training code loads.
5. Graceful shutdown disposes the DB engine, closes Redis and cancels the
   supervisor (`main.py:146-163`).

The HTTP layer also enforces a **30 s request timeout**: any request exceeding
it returns `504 {"detail":{"code":"request_timeout"}}` (`main.py:175-193`).

---

## 4. Endpoints & auth

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/v1/health/live` | none | Liveness. Returns `ok` while the process is up. |
| GET | `/v1/health/ready` | none | Readiness. `ok`/`degraded` + per-check detail. |
| GET | `/v1/trust-score` | Laravel Bearer token | Score for the calling identity (`X-User-Id`). |
| GET | `/v1/trust-score/{agent_id}` | Admin | Score for an explicit agent id. |
| GET | `/v1/admin/trust-scores` | Admin | Filtered, paginated admin list/search. |
| POST | `/v1/webhooks/domain-event` | `X-Webhook-Secret` | Invalidate a cached score on state change. |
| GET | `/metrics` | `METRICS_TOKEN` Bearer | Prometheus scrape target. |
| GET | `/` | none | Service name/version/env banner. |

`/v1/suppliers/*` routes are registered under the suppliers router.

### Health probes

- **`/v1/health/live`** — always `ok` if the process is serving. Use for
  restart/instance liveness; it exercises nothing.
- **`/v1/health/ready`** — reports three checks (`health.py:53-65`):
  `redis` (ping), `database_reachable` (`SELECT 1`), `database_schema_ok`
  (`validate_schema`). If schema is drifting it also returns
  `database_missing_schema` with the missing objects. Status is `ok` only when
  all three pass, otherwise `degraded`.

Downstream domain errors are returned as
`{"detail":{"code": "...", "message": "..."}}` with the matching HTTP status
(`main.py:224-229`); validation errors return `422` with FastAPI's `errors` list.

---

## 5. Observability, logs & alerting

### Metrics (`GET /metrics`)

Bearer-gated by `METRICS_TOKEN` (`main.py:216-218`). All HTTP/cache labels use
**route templates** (e.g. `/v1/trust-score/{agent_id}`), never raw URLs;
unmatched paths are labeled `unmatched` (`metrics.py:166-170`).

| Series | Labels | Meaning |
|---|---|---|
| `agent_trust_http_requests_total` | `status`, `method`, `route` | HTTP requests by outcome/route |
| `agent_trust_http_duration_seconds` | — | HTTP duration histogram (seconds) |
| `agent_trust_scoring_duration_ms` | — | Scoring latency histogram — **observed in seconds**; legacy `_ms` name kept for dashboard compatibility |
| `agent_trust_requests_total` | `status` | Legacy service-tone counter (back-compat) |
| `agent_trust_cache_hits_total` | — | Primary cache reads that returned a value |
| `agent_trust_cache_misses_total` | — | Primary cache reads that returned nothing |
| `agent_trust_cache_stale_hits_total` | — | **24 h stale fallback served (DB down or schema changed)** |
| `agent_trust_cache_invalidations_total` | — | Webhook-triggered invalidations |
| `agent_trust_cache_errors_total` | — | Redis errors swallowed by the cache |
| `agent_trust_database_failures_total` | — | Scoring requests that hit a broken/drifted DB |
| `agent_trust_schema_drift_total` | — | Schema-guard drift detections |
| `agent_trust_webhooks_total` | `outcome` | `received` / `invalid_secret` / `no_secret_configured` |
| `agent_trust_ratelimit_rejections_total` | — | Requests rejected with HTTP 429 |
| `agent_trust_component_unavailable_total` | `component` | Component excluded from composite for lack of evidence |
| `agent_trust_ml_predictions_total` … `agent_trust_model_rollbacks_total` | — | ML/Training lifecycle counters (gate-ON only, inert by default) |

Prometheus scrape config (reference; bearer token from your secret manager):

```yaml
scrape_configs:
  - job_name: agent-trust
    metrics_path: /metrics
    scheme: https
    bearer_token_file: /run/secrets/metrics_token
    static_configs:
      - targets: ["agent-trust.example.com"]
```

Suggested alert rules (documented policy — wire them in your alerting tool):

```yaml
groups:
  - name: agent-trust
    rules:
      - alert: AgentTrust5xxRateHigh
        expr: sum by (route) (rate(agent_trust_http_requests_total{status=~"5.."}[5m])) > 0.05
        for: 5m
      - alert: AgentTrustStaleServed
        expr: increase(agent_trust_cache_stale_hits_total[5m]) > 0
      - alert: AgentTrustSchemaDrift
        expr: increase(agent_trust_schema_drift_total[5m]) > 0
      - alert: AgentTrustWebhookSecretFailures
        expr: sum(increase(agent_trust_webhooks_total{outcome="invalid_secret"}[5m])) > 5
      - alert: AgentTrustRateLimited
        expr: increase(agent_trust_ratelimit_rejections_total[5m]) > 50
```

### Logs

- **Application logs** — structured JSON on **stdout** (`configure_logging`).
- **Access log** — one line per HTTP request on the `agent_trust.access`
  logger: `request_id`, `method`, `path` (route template), `status`,
  `duration_ms`. It carries no client IP by design.
- **Audit trail** — `logs/agent_score_audits.log` (default). Previous-score
  lookup reads a bounded 256 KiB tail, walking rotated backups newest-first;
  the active file rotates at 10 MiB keeping 3 backups
  (`settings.py:226-235`).

> **Deployment note:** the audit log path is local to the container. If
> previous-score continuity across restarts matters, mount a volume at the
> configured `AUDIT_LOG_PATH`.

---

## 6. Failure modes & recovery

### 6.1 External MySQL is down

- **Symptom:** `database_failures` increments; `/v1/health/ready` shows
  `database_reachable: false`; access-log 503s with code `database_unavailable`.
- **Behaviour:** scoring falls back to the **24 h stale cache**
  (`cache_stale_hits` fires). Agents with a stale copy still get a score;
  agents without one return `503 database_unavailable`
  (`errors.py:26-34`). The audit trail still writes. The cache's Redis errors
  are swallowed and logged.
- **Recovery:** coordinate with the DB/Laravel owner. Nothing to redeploy —
  behaviour returns to primary-cache as soon as the DB answers. Do **not**
  flush Redis; the stale tier is what keeps agents served during the outage.

### 6.2 Schema drift on the external DB

- **Symptom:** `schema_drift` increments; `/v1/health/ready` reports
  `database_schema_ok: false` plus the missing objects; **production/staging
  refuse to boot** with `Database schema drift on startup: <objects>`
  (`main.py:39-47`). Already-running processes serve `503 schema_changed`
  with stale-cache fallback where available (`errors.py:37-43`).
- **Recovery:** restore the missing tables/columns listed in the message (the
  check runs against `REQUIRED_SCHEMA` in `app/infra/db/schema_guard.py`),
  then redeploy/restart.

### 6.3 Redis is down

- **Symptom:** `cache_errors` increments; `/v1/health/ready` shows
  `redis: false`; scoring requests hit the DB on every request (cold cache).
- **Behaviour:**
  - **Cache** degrades silently — `get`/`set`/`invalidate`/`get_stale`
    swallow and log Redis errors; scoring continues against the DB.
  - **Rate limiter** (`RATE_LIMITER_BACKEND=redis`, the default) raises
    `DatabaseUnavailableError` on failure, so **rate-limited authenticated
    requests return `503 database_unavailable`** (`rate_limit.py:75-76`). This
    is the documented behaviour of the redis backend, not a bug.
- **Recovery:** restore Redis. Consider the in-process limiter
  (`RATE_LIMITER_BACKEND=memory`) only for an explicitly single-worker,
  non-sharded deployment; the memory backend resets on restart and is not
  shared across processes.

### 6.4 Cache reset / Redis flush

- After a Redis flush, the cache is cold. The first request per agent recomputes
  the score and repopulates primary + stale tiers. A domain-event webhook
  clears both tiers in one atomic delete (`cache_adapter.py:79-89`).

### 6.5 Webhook secret failures

- **Symptom:** `agent_trust_webhooks_total` spikes by outcome
  (`webhooks.py:39-51`).
  - `no_secret_configured` → the endpoint has no `WEBHOOK_SECRET` configured:
    it returns `503`, so configure the secret.
  - `invalid_secret` → `401 Unauthorized`: the sender's `X-Webhook-Secret`
    does not match. Verify the shared value with the sending service.
  - `received` → normal.

### 6.6 Rate limiting

- `agent_trust_ratelimit_rejections_total` increments and callers receive
  `429` from the sliding-window limiter (default 120/min/key). Tune
  `RATE_LIMIT_PER_MINUTE` if traffic exceeded the cap; verify the caller is not
  hammering before raising the limit.

### 6.7 ML Target Programme (ships disabled)

- With `ml_enabled=false` (or empty `ml_targets`) **nothing** trains, loads,
  blends or serves; the supervisor is not created (`main.py:50-56`). The ML
  counters are defined but inert.
- When enabled: a Redis-locked supervisor wakes every
  `ml_training_poll_hours`, and only one worker trains per tick (lock
  `ml_training_lock_ttl_seconds`). Training runs that start are counted in
  `training_runs_total` (`runs == success + failure`); a tick that decides
  "nothing to do" increments nothing. Candidate evaluation promotes a
  challenger only above `ml_promotion_headroom`; a bad challenger is rejected
  and the champion is kept. Any tick failure is logged and retried on the next
  tick — it never kills the API (`main.py:90-119`).
- Rollback of a promoted model is supported (see §7).

### 6.8 Request timeouts

- Requests longer than 30 s return `504 request_timeout` (`main.py:175-193`).
  A spike usually means the external DB is slow; see §6.1/§6.2 first.

---

## 7. Rollback & safe changes

### Application rollback

1. Redeploy the previous GHCR image tag (`docker-release.yml` publishes on
   `v*.*.*` tags):
   ```
   docker compose up -d --pull always --force-recreate <previous-tag>
   ```
2. The startup checks (§3) re-run and gate the boot.

### Configuration rollback

1. Restore the previous `.env` / secret set.
2. Restart the app; `_validate_startup()` re-runs and refuses invalid config.

### Model rollback (ML programme enabled)

A promoted champion can be rolled back to the previous known-good production
version via the model registry tooling (counter: `model_rollbacks_total`).
Model artifacts are pinned by registry version directory under
`ML_MODEL_REGISTRY_DIR`.

### Safe config changes

Operations are **config-only**: thresholds, tier boundaries, composite
weights, confidence switches (default off) and ML switches. Enabling/adjusting
any of them **changes published scores**. Treat each as a controlled change:

1. Review the before/after score distribution (the Wilson/confidence switches
   exist precisely because enabling them changes published scores).
2. Enable in a low-risk environment first; watch the score/tier distribution
   and alert rules from §5.
3. Promote to production and watch `http_duration`, cache staleness and DB
   load for a settle period.

---

## 8. Secrets rotation

| Secret | Procedure |
|---|---|
| `LARAVEL_SERVICE_TOKEN` | Generate a new token (§2). Update this API **and** the Laravel `.env` at the same time; deploy both together. Requests with the old token are rejected until both sides converge. |
| `WEBHOOK_SECRET` | Generate a new token. Update this API **and notify every calling core service** of the new `X-Webhook-Secret` value before/with deployment to avoid a `invalid_secret` storm (401s). |
| `METRICS_TOKEN` | Generate a new token. Update this API **and** every Prometheus scrape config / secret store. |

`/metrics` uses its own token (`METRICS_TOKEN`, `settings.py:35-38`) so scrape
credentials rotate independently of the service token.

---

## 9. Troubleshooting

### Golden commands

```bash
# Liveness / readiness
curl -sf http://localhost:8000/v1/health/live
curl -s http://localhost:8000/v1/health/ready        # status ok|degraded + checks

# Container state / logs
docker compose ps
docker compose logs --tail=200 app

# Redis
redis-cli ping
redis-cli --scan --pattern 'stale:trust:*'           # stale-cache keys
redis-cli --scan --pattern 'lock:*'                   # cache locks
redis-cli --scan --pattern 'rl:*'                     # rate-limit keys

# Metrics (bearer)
curl -s -H "Authorization: Bearer $METRICS_TOKEN" http://localhost:8000/metrics \
  | grep -E 'cache_stale|schema_drift|database_failures|webhooks_total'
```

### Tracing a single request

Every access-log line carries a `request_id`. Filter the access log by it to
get `method`, route, `status` and `duration_ms` in one place; cross-reference
the same `request_id` in application logs.

### Symptom → action (quick table)

| Symptom | Look at | Action |
|---|---|---|
| `ready` = `degraded`, `redis: false` | §6.3 | Restore Redis; expect rate-limited 503s meanwhile (redis backend) |
| `ready` = `degraded`, `database_reachable: false` | §6.1 | DB/Laravel owner; stale cache is serving |
| `ready` = `degraded`, `database_schema_ok: false` | §6.2 | Restore drifted tables/columns; prod won't boot until fixed |
| `cache_stale_hits` rising | §6.1/§6.2 | Confirm DB outage/drift; leave stale tier intact |
| Webhook `invalid_secret` storm | §6.5 | Rotate/coordinate `WEBHOOK_SECRET` with senders |
| 429s rising | §6.6 | Tune `RATE_LIMIT_PER_MINUTE` or throttle caller |
| 504 `request_timeout` | §6.8/§6.1 | Investigate DB latency; slow pages |
| Audit file growth | §5 logs note | Built-in rotation (10 MiB/3 backups); mount volume if continuity needed |