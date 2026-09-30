# app/observability/logging.py
from logging.config import dictConfig

from app.infra.settings import get_settings


def configure_logging() -> None:
    settings = get_settings()
    dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {
                "json": {
                    "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                    "format": "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s",
                },
                # Access-log lines only: the extra fields live solely on this
                # formatter so ordinary JSON lines stay byte-for-byte unchanged.
                "json_access": {
                    "()": "pythonjsonlogger.jsonlogger.JsonFormatter",
                    "format": (
                        "%(asctime)s %(levelname)s %(name)s %(message)s %(request_id)s "
                        "%(method)s %(path)s %(status)s %(duration_ms)s"
                    ),
                },
            },
            "handlers": {
                "default": {
                    "class": "logging.StreamHandler",
                    "formatter": "json",
                },
                "access": {
                    "class": "logging.StreamHandler",
                    "formatter": "json_access",
                },
            },
            "loggers": {
                "agent_trust.access": {
                    "handlers": ["access"],
                    "level": "INFO",
                    "propagate": False,
                },
            },
            "root": {
                "handlers": ["default"],
                "level": settings.log_level.upper(),
            },
        }
    )
