import logging
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "Agent Trust Score API"
    app_env: str = "development"
    app_version: str = "0.1.0"
    log_level: str = "INFO"

    # Database
    db_host: str
    db_port: int = 3306
    db_database: str
    db_username: str
    db_password: str

    # Redis
    redis_url: str = "redis://127.0.0.1:6379/0"

    # Security
    laravel_service_token: str = ""
    webhook_secret: str = ""
    rate_limit_per_minute: int = 120

    # Identity
    identity_model_type: str = "App\\Models\\User"

    # Database connection pool
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30

    # Rate limiter backend ("memory" | "redis")
    rate_limiter_backend: str = "redis"

    # CORS — comma-separated origins in production; must be explicitly configured
    cors_origins: str = ""

    # Scoring & Risk Thresholds
    conversion_thresholds: dict = {1: 15, 7: 50, 30: 150, 365: 500}
    credit_max_delay_days: int = 60
    credit_max_overdue_ratio: float = 50.0
    credit_max_overdue_count: int = 10
    credit_max_consecutive_overdue_cycles: int = 3
    high_risk_score_cap: int = 30

    # Overdue boundary (senior §8.2). A transaction is overdue when:
    #   status <> 'paid' AND due_date IS NOT NULL AND due_date {operator} CURRENT_DATE()
    #   "<"  : a payment due today is NOT yet overdue (senior's recommended definition)
    #   "<=" : a payment due today is overdue immediately at due time
    # BUSINESS CONFIRMATION HELD OPEN — keep "<" until the business team confirms
    # whether due-today counts as overdue. Only these two values are accepted.
    credit_overdue_boundary: str = "<"

    # Reliability attribution (senior §6.2 / §7), awaiting business confirmation.
    # Reason codes that classify a booking failure / cancellation as OUTSIDE the
    # agent's control (supplier outage, internal platform outage, infrastructure
    # timeout, supplier-side inventory failure; airline/supplier cancellations,
    # schedule changes, involuntary cancellations). Empty = attribute every failure
    # and cancellation to the agent. Populating these requires a failure/cancellation
    # reason column in the booking data (pending schema confirmation, Sprint 2).
    non_agent_failure_reasons: list[str] = []
    non_agent_cancellation_reasons: list[str] = []

    # Supplier Quota (Feature A) — site-wide, created-only supplier requests
    supplier_quota_period_type: str = "lifetime"  # lifetime|daily|monthly(rolling 30d)|rolling
    supplier_quota_period_days: int = 30  # window length for period_type="rolling"
    supplier_counted_run_statuses: list[str] = ["success"]

    # Agent Search-to-Booking (Feature B) — additive 20% trust component
    supplier_success_booking_statuses: list[str] = ["confirmed", "ticketed"]
    search_to_booking_min_searches: int = 20
    search_to_booking_neutral_score: float = 80.0
    search_to_booking_at_target_score: float = 80.0
    search_to_booking_excellent_multiplier: float = 4.0
    # Supplier-specific L2B (senior §10-13). Targets come from the site-level
    # suppliers table (minimum_booking / search_limit) and are NEVER derived per
    # agent. Agent searches count created + reused access (SUM(access_count));
    # bookings are attributed per supplier via bookings.provider = suppliers.name.
    # "exclude" is the only sanctioned unconfigured-supplier policy (senior §13):
    # a supplier without a usable target is excluded, never assigned invented
    # compliance. Channel grouping stays OFF until the business defines it.
    l2b_not_configured_policy: str = "exclude"
    l2b_group_by_channel: bool = False
    # Aggregation safeguard (senior §13): no single supplier may drive more than
    # this share of the final L2B component; any excess is redistributed
    # proportionally over the remaining suppliers.
    l2b_max_supplier_share: float = 0.5
    composite_weights: dict[str, float] = {
        "reliability": 0.40,
        "financial": 0.25,
        "experience": 0.15,
        "search_to_booking": 0.20,
    }

    # ── ML Target Programme (Sprint 5; everything ships DISABLED) ─────────────
    #
    # Two independent switches gate ALL ML behaviour (senior §15.1/§15.2):
    #   * ml_enabled: master runtime switch. Default False. Nothing trains, loads,
    #     blends, or is served while False.
    #   * ml_targets: business-approved target list. Default [] = nothing approved.
    #     Empty list is VALID (NOT_READY + disabled are the shipping state).
    #
    # Combiner weights are validated against the STATIC allowed-target whitelist,
    # never against ml_targets — so the placeholder weights (40/35/25) remain valid
    # while ml_targets is empty. When ml_targets later becomes non-empty the coupling
    # check (every approved target has a weight) activates; see _validate_startup().
    ml_enabled: bool = False
    ml_cold_start_min_bookings: int = 400
    ml_horizon_days: int = 30
    ml_targets: list[str] = []
    ml_risk_to_score_weights: dict[str, float] = {
        "severe_default": 0.40,
        "severe_reliability": 0.35,
        "l2b_breach": 0.25,
    }
    ml_risk_to_score_mode: str = "linear_inverse"
    ml_readiness_min_samples: int = 30
    ml_readiness_min_positive: int = 8
    ml_readiness_min_negative: int = 8
    ml_readiness_min_agents: int = 10
    ml_readiness_max_positive_ratio: float = 0.95
    ml_model_registry_dir: str = "ml/models/registry"

    # ── Sprint 6 — Training Controller (senior §19-§24) ───────────────────────
    # Every field below is consumed ONLY by the training controller / supervisor
    # and is inert while ml_enabled=False. Drift-triggered retraining is DEFERRED
    # in this phase: classify_trigger reports NOT_EVALUATED for drift, and the two
    # implemented triggers are data growth and training interval.
    # Data-growth trigger: new eligible labelled records (label-mature bookings)
    # accumulated since the last training dataset, per target. This is never the
    # raw total booking count.
    ml_trigger_increment: int = 100
    # Interval trigger: retrain when this many days passed since the last
    # successful training run for the target.
    ml_training_interval_days: int = 7
    # Supervisor wake-up cadence (implementation choice for the senior's
    # "periodic training opportunity" requirement).
    ml_training_poll_hours: int = 24
    # Cross-process guard: only one worker may run a training tick at a time.
    ml_training_lock_ttl_seconds: int = 3600
    # Promotion headroom: a challenger must beat the champion by this factor
    # before promotion. PROJECT DECISION, not a senior-specified value.
    ml_promotion_headroom: float = 0.002
    # Candidate validation thresholds for the binary classifiers. The senior
    # specifies the METRICS (ROC-AUC, PR-AUC, precision, recall, F1,
    # calibration, confusion matrix, segments) and the validation concept, not
    # these numbers: they are configurable PROJECT DECISIONS.
    ml_classification_min_pr_auc: float = 0.30
    ml_classification_min_f1: float = 0.40
    ml_classification_max_brier: float = 0.25
    # Per-segment regression guard: reject when any segment's recall drops more
    # than this below the champion's recall for that segment.
    ml_segment_max_recall_drop: float = 0.05

    # Reliability sub-components (no-evidence components are excluded, and the
    # remaining weights are renormalized over the configured sum).
    reliability_component_weights: dict[str, float] = {
        "booking_success": 0.6429,
        "cancellation_quality": 0.3571,
    }

    # Audit log file path
    audit_log_path: str = "logs/agent_score_audits.log"

    @property
    def database_url(self) -> str:
        from urllib.parse import quote_plus

        pw = quote_plus(self.db_password)
        return f"mysql+pymysql://{self.db_username}:{pw}@{self.db_host}:{self.db_port}/{self.db_database}?charset=utf8mb4"

    def _validate_startup(self) -> None:
        if self.app_env not in ("development", "local", "test", "staging", "production"):
            logger.warning("Unknown APP_ENV value: %s", self.app_env)
        if not self.laravel_service_token:
            raise RuntimeError("LARAVEL_SERVICE_TOKEN must be set in all environments")
        if len(self.laravel_service_token) < 32:
            raise RuntimeError("LARAVEL_SERVICE_TOKEN must be at least 32 characters")
        placeholder_prefixes = ("change-me", "your-", "test-")
        if self.laravel_service_token.startswith(placeholder_prefixes) and self.app_env != "test":
            raise RuntimeError("LARAVEL_SERVICE_TOKEN must be changed from the default")
        if not all([self.db_host, self.db_database, self.db_username]):
            raise RuntimeError("Database connection settings are incomplete")
        if self.rate_limit_per_minute < 1:
            raise RuntimeError("rate_limit_per_minute must be at least 1")
        if self.credit_overdue_boundary not in ("<", "<="):
            raise RuntimeError("credit_overdue_boundary must be '<' or '<='")
        if self.credit_max_consecutive_overdue_cycles < 1:
            raise RuntimeError("credit_max_consecutive_overdue_cycles must be at least 1")
        if self.app_env in ("production", "staging") and not self.cors_origins:
            raise RuntimeError("CORS_ORIGINS must be configured for production/staging deployment")
        if self.app_env in ("production", "staging") and not self.webhook_secret:
            raise RuntimeError("WEBHOOK_SECRET must be set in production/staging")
        if self.supplier_quota_period_type not in ("lifetime", "daily", "monthly", "rolling"):
            raise RuntimeError(
                f"supplier_quota_period_type must be one of lifetime|daily|monthly|rolling, "
                f"got {self.supplier_quota_period_type!r}"
            )
        if not self.supplier_counted_run_statuses or not self.supplier_success_booking_statuses:
            raise RuntimeError("supplier run/booking status lists must be non-empty")
        if self.search_to_booking_min_searches < 1:
            raise RuntimeError("search_to_booking_min_searches must be at least 1")
        if not 0.0 <= self.search_to_booking_at_target_score <= 100.0:
            raise RuntimeError("search_to_booking_at_target_score must be within 0-100")
        if self.search_to_booking_excellent_multiplier < 1.0:
            raise RuntimeError("search_to_booking_excellent_multiplier must be >= 1.0")
        if self.l2b_not_configured_policy != "exclude":
            raise RuntimeError("l2b_not_configured_policy must be 'exclude'")
        if not 0.0 < self.l2b_max_supplier_share <= 1.0:
            raise RuntimeError("l2b_max_supplier_share must be within (0, 1]")
        if abs(sum(self.composite_weights.values()) - 1.0) > 1e-6:
            raise RuntimeError(
                f"composite_weights must sum to 1.0, got {sum(self.composite_weights.values())}"
            )

        # ── ML Target Programme startup checks (senior §15/§16, Sprint 5) ──────
        # Both ml_targets and the risk-to-score weights are validated against the
        # STATIC whitelist ALLOWED_ML_TARGETS below — never against each other at
        # startup. This is deliberate: it lets the service ship with the default
        # state (ml_targets=[] + ml_enabled=False + placeholder weights 40/35/25)
        # while ml_targets is still awaiting business approval. The only coupling
        # (every approved target must have a weight) is enforced dynamically at
        # runtime by the scorer gate, and by the trainer, when ml_targets is
        # non-empty — never as a startup blocker that would reject the shipping
        # configuration itself.
        allowed_ml_targets = {"severe_default", "severe_reliability", "l2b_breach"}
        if not set(self.ml_targets).issubset(allowed_ml_targets):
            raise RuntimeError(
                "ml_targets must be a subset of "
                f"{sorted(allowed_ml_targets)}, got {self.ml_targets}"
            )
        if not self.ml_risk_to_score_weights or any(
            v <= 0 for v in self.ml_risk_to_score_weights.values()
        ):
            raise RuntimeError("ml_risk_to_score_weights must be non-empty with all weights > 0")
        if not set(self.ml_risk_to_score_weights).issubset(allowed_ml_targets):
            raise RuntimeError(
                "ml_risk_to_score_weights keys must be a subset of "
                f"{sorted(allowed_ml_targets)}, got {list(self.ml_risk_to_score_weights)}"
            )
        if abs(sum(self.ml_risk_to_score_weights.values()) - 1.0) > 1e-6:
            raise RuntimeError(
                "ml_risk_to_score_weights must sum to 1.0, got "
                f"{sum(self.ml_risk_to_score_weights.values())}"
            )
        if not self.reliability_component_weights:
            raise RuntimeError("reliability_component_weights must be non-empty")
        if abs(sum(self.reliability_component_weights.values()) - 1.0) > 1e-6:
            raise RuntimeError(
                "reliability_component_weights must sum to 1.0, got "
                f"{sum(self.reliability_component_weights.values())}"
            )
        if not self.conversion_thresholds:
            raise RuntimeError("conversion_thresholds must be non-empty")
        if any(int(v) < 1 for v in self.conversion_thresholds.values()):
            raise RuntimeError("conversion_thresholds values must be at least 1")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    # pydantic-settings populates fields from env vars at runtime,
    # so the no-arg constructor is correct despite mypy's call-arg warning.
    return Settings()  # type: ignore[call-arg]
