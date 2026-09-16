import os

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DB_HOST", "127.0.0.1")
os.environ.setdefault("DB_PORT", "3306")
os.environ.setdefault("DB_DATABASE", "test_db")
os.environ.setdefault("DB_USERNAME", "test_user")
os.environ.setdefault("DB_PASSWORD", "test_pass")
os.environ.setdefault("REDIS_URL", "redis://127.0.0.1:6379/0")
os.environ.setdefault("LARAVEL_SERVICE_TOKEN", "test-service-token-0123456789abcdef")
os.environ.setdefault("LOG_LEVEL", "CRITICAL")
