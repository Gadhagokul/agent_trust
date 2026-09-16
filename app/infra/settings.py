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
    high_risk_score_cap: int = 30

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
    composite_weights: dict[str, float] = {
        "reliability": 0.40,
        "financial": 0.25,
        "experience": 0.15,
        "search_to_booking": 0.20,
    }
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
        if abs(sum(self.composite_weights.values()) - 1.0) > 1e-6:
            raise RuntimeError(
                f"composite_weights must sum to 1.0, got {sum(self.composite_weights.values())}"
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
