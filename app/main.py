from __future__ import annotations

from contextlib import asynccontextmanager
import logging
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import RedirectResponse

from app.api.routes import router
from app.config import Settings
from app.errors import (
    ServiceError,
    http_error_handler,
    service_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("agroclima.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        app.state.http_client = httpx.AsyncClient(timeout=app_settings.request_timeout_seconds)
        try:
            yield
        finally:
            await app.state.http_client.aclose()

    application = FastAPI(
        title=app_settings.service_name,
        description=(
            "API analítica para temperatura, umidade, métricas climáticas e dashboards. "
            "A autenticação e o escopo de dados são validados pela API Java."
        ),
        version="1.0.0",
        root_path=app_settings.root_path,
        docs_url=None,
        redoc_url=None,
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )
    application.state.settings = app_settings

    # Compatibilidade com Swagger UI: Força a versão do OpenAPI para 3.0.3
    def custom_openapi():
        if application.openapi_schema:
            return application.openapi_schema
        openapi_schema = get_openapi(
            title=application.title,
            version=application.version,
            description=application.description,
            routes=application.routes,
        )
        openapi_schema["openapi"] = "3.0.3"
        application.openapi_schema = openapi_schema
        return application.openapi_schema

    application.openapi = custom_openapi

    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.configured_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept"],
    )
    application.include_router(router)
    application.add_exception_handler(ServiceError, service_error_handler)
    application.add_exception_handler(RequestValidationError, validation_error_handler)
    application.add_exception_handler(400, http_error_handler)
    application.add_exception_handler(401, http_error_handler)
    application.add_exception_handler(403, http_error_handler)
    application.add_exception_handler(404, http_error_handler)
    application.add_exception_handler(405, http_error_handler)
    application.add_exception_handler(422, http_error_handler)
    application.add_exception_handler(500, http_error_handler)
    application.add_exception_handler(Exception, unhandled_error_handler)

    @application.get("/", include_in_schema=False)
    async def root():
        """Rota raiz para evitar erro 404 Not Found ao acessar o host base."""
        return {
            "status": "UP",
            "service": app_settings.service_name,
            "docs": "/docs",
            "health": "/health",
        }

    @application.get("/docs", include_in_schema=False, tags=["Operação"])
    async def local_swagger_ui(request: Request):
        root_path = request.scope.get("root_path", "").rstrip("/")
        openapi_url = f"{root_path}{application.openapi_url}"
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{application.title} - Swagger UI",
            swagger_js_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js",
            swagger_css_url="https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css",
        )

    @application.get("/health", tags=["Operação"])
    async def health(request: Request):
        settings: Settings = request.app.state.settings
        return {
            "status": "UP",
            "service": settings.service_name,
            "javaApiConfigured": bool(settings.java_api_url),
            "internalKeyConfigured": settings.internal_key_configured,
        }

    return application


app = create_app()
