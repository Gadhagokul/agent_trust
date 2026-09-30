# app/main.py
import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from app.api.v1.router import api_router
from app.domain.errors import DomainError
from app.infra.db.schema_guard import validate_schema
from app.infra.db.session import SessionLocal, engine
from app.infra.redis_provider import get_redis_provider
from app.infra.settings import get_settings
from app.observability.logging import configure_logging
from app.observability.metrics import MetricsMiddleware, metrics_response
from app.observability.request_context import RequestContextMiddleware
from app.security.auth import get_metrics_viewer

logger = logging.getLogger(__name__)

TRAINING_LOCK_NAME = "agent_trust:ml_training_lock"


def startup_schema_check_required(settings) -> bool:
    """Startup schema validation is a production fail-fast, not a dev burden.

    Development/local/test keep a DB-optional boot (unit tests construct the
    app without a MySQL server); production/staging refuse to boot into a
    drifted external schema (senior §39).
    """
    return settings.app_env in ("production", "staging")


def _run_startup_schema_check(settings) -> None:
    """Fail at boot when the external DB no longer matches REQUIRED_SCHEMA."""
    db = SessionLocal()
    try:
        missing = validate_schema(db)
    finally:
        db.close()
    if missing:
        raise RuntimeError(f"Database schema drift on startup: {missing}")


def training_supervisor_enabled(settings) -> bool:
    """The supervisor only exists while the ML programme is switched on.

    With the shipped defaults (ml_enabled=False, ml_targets=[]) this returns
    False, so no task is created, no lock is taken and no training code loads.
    """
    return bool(settings.ml_enabled) and bool(settings.ml_targets)


async def _training_supervisor(settings) -> None:
    """Periodic training opportunity (Sprint 6).

    Wakes every ml_training_poll_hours, takes a Redis lock so only one worker
    trains per tick, and runs the training controller in a worker thread so the
    event loop is never blocked. The controller opens and closes its own DB
    session; no session is created here and none crosses the thread boundary.
    Any failure is logged and retried on the next tick - it never kills the API.
    """
    from app.infra.db.session import SessionLocal
    from app.ml.trust_model import (
        build_default_dataset,
        run_training_controller,
        train_random_forest_candidate,
    )

    poll_seconds = max(1, int(settings.ml_training_poll_hours)) * 3600

    def build_dataset(target, *, as_of, db):
        return build_default_dataset(settings, db, target, as_of=as_of)

    def labeled_count_reader(db, target, as_of):
        from app.infra.db.repository import AgentRepository

        return AgentRepository().get_labeled_sample_count(
            db,
            target=target,
            horizon_days=int(settings.ml_horizon_days),
            as_of=as_of,
        )

    while True:
        await asyncio.sleep(poll_seconds)
        lock = None
        try:
            lock = get_redis_provider().client.lock(
                TRAINING_LOCK_NAME,
                timeout=int(settings.ml_training_lock_ttl_seconds),
            )
            if not lock.acquire(blocking=False):
                logger.info("ML training tick skipped: lock held by another worker")
                continue
            logger.info("ML training tick: running training controller")
            await asyncio.to_thread(
                run_training_controller,
                settings,
                build_dataset=build_dataset,
                train_candidate=train_random_forest_candidate,
                session_factory=SessionLocal,
                labeled_count_reader=labeled_count_reader,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ML training tick failed; retried on the next tick")
        finally:
            if lock is not None:
                try:
                    lock.release()
                except Exception:
                    logger.warning("ML training lock release failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    app.state.settings = settings

    settings._validate_startup()

    if startup_schema_check_required(settings):
        _run_startup_schema_check(settings)

    logger.info("Service started", extra={"env": settings.app_env, "version": settings.app_version})

    training_task: asyncio.Task | None = None
    if training_supervisor_enabled(settings):
        training_task = asyncio.create_task(_training_supervisor(settings))
        app.state.training_supervisor = training_task
        logger.info(
            "ML training supervisor started (poll every %sh)",
            settings.ml_training_poll_hours,
        )
    else:
        logger.info("ML training supervisor not started (ML programme disabled)")

    try:
        yield
    finally:
        if training_task is not None:
            training_task.cancel()
            try:
                await training_task
            except asyncio.CancelledError:
                pass
        engine.dispose()
        logger.info("Database connections closed")

        try:
            redis_provider = get_redis_provider()
            redis_provider.client.close()
            logger.info("Redis connection closed")
        except Exception:
            pass

        logger.info("Service shut down gracefully")


app = FastAPI(
    title=get_settings().app_name,
    version=get_settings().app_version,
    lifespan=lifespan,
)


class TimeoutMiddleware:
    def __init__(self, app: ASGIApp, timeout: int = 30):
        self.app = app
        self.timeout = timeout

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await asyncio.wait_for(self.app(scope, receive, send), timeout=self.timeout)
        except asyncio.TimeoutError:
            from fastapi.responses import JSONResponse

            response = JSONResponse(
                status_code=504,
                content={"detail": {"code": "request_timeout", "message": "Request timed out"}},
            )
            await response(scope, receive, send)


app.add_middleware(RequestContextMiddleware)
app.add_middleware(TimeoutMiddleware, timeout=30)
app.add_middleware(MetricsMiddleware)

cors_origins_str = get_settings().cors_origins
if cors_origins_str:
    origins = [o.strip() for o in cors_origins_str.split(",") if o.strip()]
else:
    origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Expose Prometheus metrics (bearer-gated, senior §38)
@app.get("/metrics")
def metrics(_: None = Depends(get_metrics_viewer)):
    return metrics_response()


app.include_router(api_router)


@app.exception_handler(DomainError)
async def domain_exception_handler(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": {"code": exc.code, "message": exc.message}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors()},
    )


@app.get("/")
async def root():
    settings = get_settings()
    return {
        "service": settings.app_name,
        "version": settings.app_version,
        "env": settings.app_env,
    }
