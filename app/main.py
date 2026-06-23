# app/main.py
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from app.api.v1.router import api_router
from app.domain.errors import DomainError
from app.infra.db.session import engine
from app.infra.redis_provider import get_redis_provider
from app.infra.settings import get_settings
from app.observability.logging import configure_logging
from app.observability.request_context import RequestContextMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    app.state.settings = settings

    settings.validate()

    logger.info("Service started", extra={"env": settings.app_env, "version": settings.app_version})

    yield

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

app.add_middleware(RequestContextMiddleware)

origins = (
    ["*"]
    if get_settings().cors_origins == "*"
    else [o.strip() for o in get_settings().cors_origins.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Instrument the app before returning/startup to prevent middleware modification errors
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

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