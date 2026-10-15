"""API process entry point.

``create_api_app(container)`` is the factory tests use. ``uvicorn app.main_api:app`` builds
the production app lazily from settings on first access.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi

from app import __version__
from app.api import health
from app.api.v1 import router as v1_router
from app.container import Container, build_container
from app.platform.correlation import RequestContextMiddleware
from app.platform.http.errors import ErrorEnvelope, install_error_handlers
from app.platform.logging import configure_logging
from app.settings import load_settings

API_PREFIX = "/api/v1"


def _openapi(app: FastAPI) -> dict[str, Any]:
    """OpenAPI document that always publishes the shared error envelope schemas."""
    if app.openapi_schema is None:
        spec = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schemas = spec.setdefault("components", {}).setdefault("schemas", {})
        error_schema = ErrorEnvelope.model_json_schema(
            by_alias=True, ref_template="#/components/schemas/{model}", mode="serialization"
        )
        schemas.update(error_schema.pop("$defs", {}))
        schemas.setdefault("ErrorEnvelope", error_schema)
        app.openapi_schema = spec
    return app.openapi_schema


def create_api_app(container: Container) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        await container.aclose()

    app = FastAPI(
        title="RelaxAI API",
        version=__version__,
        openapi_url=f"{API_PREFIX}/openapi.json",
        docs_url=f"{API_PREFIX}/docs",
        redoc_url=None,
        lifespan=lifespan,
    )
    app.state.container = container
    install_error_handlers(app)
    app.include_router(health.router)
    app.include_router(v1_router, prefix=API_PREFIX)
    app.add_middleware(RequestContextMiddleware)
    app.openapi = lambda: _openapi(app)  # type: ignore[method-assign]
    return app


_app: FastAPI | None = None


def _production_app() -> FastAPI:
    global _app
    if _app is None:
        settings = load_settings()
        configure_logging(settings.telemetry.log_level)
        _app = create_api_app(build_container(settings))
    return _app


def __getattr__(name: str) -> Any:
    # PEP 562: ``uvicorn app.main_api:app`` resolves this lazily, so importing the module
    # (tests, OpenAPI export) never reads settings or reconfigures logging.
    if name == "app":
        return _production_app()
    raise AttributeError(name)
