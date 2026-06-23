# app/main.py
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.domain.errors import DomainError
from app.infra.settings import get_settings
from app.observability.logging import configure_logging
from app.observability.request_context import RequestContextMiddleware


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging()
    app.state.settings = settings
    yield


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