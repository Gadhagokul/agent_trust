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
    api_key: str = "change-me-in-production"
    rate_limit_per_minute: int = 120

    # CORS — comma-separated origins in production, "*" in dev/local
    cors_origins: str = "*"

    # Scoring & Risk Thresholds
    conversion_thresholds: dict = {1: 15, 7: 50, 30: 150, 365: 500}
    credit_max_delay_days: int = 60
    credit_max_unpaid_ratio: float = 50.0
    credit_max_unpaid_count: int = 10
    high_risk_score_cap: int = 30

    @property
    def database_url(self) -> str:
        from urllib.parse import quote_plus
        pw = quote_plus(self.db_password)
        return f"mysql+pymysql://{self.db_username}:{pw}@{self.db_host}:{self.db_port}/{self.db_database}?charset=utf8mb4"

    def validate(self) -> None:
        if self.app_env not in ("development", "local", "test", "staging", "production"):
            logger.warning("Unknown APP_ENV value: %s", self.app_env)
        if self.app_env in ("production", "staging") and self.api_key == "change-me-in-production":
            raise RuntimeError(
                "API_KEY must be changed from the default in production/staging"
            )
        if not all([self.db_host, self.db_database, self.db_username]):
            raise RuntimeError("Database connection settings are incomplete")
        if self.rate_limit_per_minute < 1:
            raise RuntimeError("rate_limit_per_minute must be at least 1")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()