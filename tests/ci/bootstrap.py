"""CI-only integration database bootstrap.

Creates and seeds the throwaway schema used by `pytest -m integration` against
the real MySQL container. This is TEST INFRASTRUCTURE ONLY -- it is never part
of the application or production migrations.

The bootstrap fails loudly: after seeding, the app's own schema guard
(REQUIRED_SCHEMA) must report zero drift and every required table must be
non-empty, otherwise the run errors instead of silently skipping tests.
"""

import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.infra.db.schema_guard import REQUIRED_SCHEMA, validate_schema

_SCHEMA_FILE = Path(__file__).with_name("schema_seed.sql")


def _statements(sql: str) -> list[str]:
    return [s.strip() for s in sql.split(";") if s.strip() and not s.strip().startswith("--")]


def _verify(engine) -> None:
    with Session(bind=engine) as session:
        missing = validate_schema(session)
        if missing:
            raise RuntimeError(f"seeded schema does not satisfy REQUIRED_SCHEMA: {missing}")

        empty = []
        for table in REQUIRED_SCHEMA:
            count = session.execute(text(f"SELECT COUNT(*) FROM `{table}`")).scalar()
            if not count:
                empty.append(table)
        if empty:
            raise RuntimeError(f"empty seeded tables: {empty}")


def main() -> int:
    host = os.environ.get("DB_HOST", "127.0.0.1")
    port = int(os.environ.get("DB_PORT", "3306"))
    user = os.environ.get("DB_USERNAME", "root")
    password = os.environ.get("DB_PASSWORD", "")
    database = os.environ.get("DB_DATABASE", "dummy_afine")

    from urllib.parse import quote_plus

    import pymysql

    sql = _SCHEMA_FILE.read_text(encoding="utf-8")

    conn = pymysql.connect(
        host=host, port=port, user=user, password=password, autocommit=True
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{database}` CHARACTER SET utf8mb4")
            cursor.execute(f"USE `{database}`")
            for statement in _statements(sql):
                cursor.execute(statement)
    except Exception as exc:  # pragma: no cover - environment dependent
        print(f"FAILED to seed integration schema: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    url = (
        f"mysql+pymysql://{user}:{quote_plus(password)}@{host}:{port}/{database}"
        "?charset=utf8mb4"
    )
    engine = create_engine(url)
    try:
        _verify(engine)
    except RuntimeError as exc:
        print(f"FAILED schema verification: {exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"Integration schema '{database}' seeded and verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())