from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.openapi.utils import get_openapi
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import httpx
from starlette.exceptions import HTTPException as StarletteHTTPException
from swagger_ui_bundle import swagger_ui_path

from app.api.routes import router
from app.cache import TTLCache
from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import (
    ServiceError,
    http_error_handler,
    service_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.logging_config import REQUEST_ID_HEADER, RequestIdMiddleware, configure_logging
from app.models import HealthResponse, ReadyResponse

configure_logging()
logger = logging.getLogger("agroclima.api")


def create_app(settings: Settings | None = None) -> FastAPI:
    app_settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.settings = app_settings
        app.state.profile_cache = TTLCache(app_settings.profile_cache_ttl_seconds)
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
    def custom_openapi() -> dict[str, Any]:
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

    application.openapi = custom_openapi  # type: ignore[method-assign]

    application.add_middleware(
        CORSMiddleware,
        allow_origins=app_settings.configured_origins,
        allow_credentials=app_settings.cors_allow_credentials,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "Accept", REQUEST_ID_HEADER],
        expose_headers=[REQUEST_ID_HEADER],
    )
    # Por último = camada mais externa: todo request (inclusive erros de CORS) recebe um X-Request-ID.
    application.add_middleware(RequestIdMiddleware)
    application.include_router(router)
    application.mount("/static/swagger-ui", StaticFiles(directory=swagger_ui_path), name="swagger-ui")
    application.add_exception_handler(ServiceError, service_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    for status_code in (400, 401, 403, 404, 405, 422, 500):
        application.add_exception_handler(status_code, http_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(StarletteHTTPException, http_error_handler)  # type: ignore[arg-type]
    application.add_exception_handler(Exception, unhandled_error_handler)

    @application.get("/", include_in_schema=False)
    async def root() -> dict[str, str]:
        """Rota raiz para evitar erro 404 Not Found ao acessar o host base."""
        return {
            "status": "UP",
            "service": app_settings.service_name,
            "docs": "/docs",
            "health": "/health",
            "ready": "/ready",
        }

    @application.get("/docs", include_in_schema=False, tags=["Operação"])
    async def local_swagger_ui(request: Request) -> HTMLResponse:
        root_path = request.scope.get("root_path", "").rstrip("/")
        openapi_url = f"{root_path}{application.openapi_url}"
        return get_swagger_ui_html(
            openapi_url=openapi_url,
            title=f"{application.title} - Swagger UI",
            swagger_js_url=f"{root_path}/static/swagger-ui/swagger-ui-bundle.js",
            swagger_css_url=f"{root_path}/static/swagger-ui/swagger-ui.css",
            swagger_favicon_url=f"{root_path}/static/swagger-ui/favicon-32x32.png",
        )

    @application.get("/health", tags=["Operação"], response_model=HealthResponse)
    async def health(request: Request) -> HealthResponse:
        """Liveness: o processo está de pé. Não consulta dependências (use /ready para isso)."""
        settings: Settings = request.app.state.settings
        return HealthResponse(
            service=settings.service_name,
            java_api_configured=bool(settings.java_api_url),
            internal_key_configured=settings.internal_key_configured,
        )

    @application.get("/ready", tags=["Operação"], response_model=ReadyResponse,
                     responses={503: {"model": ReadyResponse, "description": "Dependência indisponível."}})
    async def ready(request: Request) -> ReadyResponse | JSONResponse:
        """Readiness: chave interna configurada e API Java respondendo. 503 quando não está pronto."""
        settings: Settings = request.app.state.settings
        client = JavaApiClient(settings, request.app.state.http_client)
        checks = {
            "internalKey": settings.internal_key_configured,
            "javaApi": await client.ping(),
        }
        is_ready = all(checks.values())
        body = ReadyResponse(
            status="READY" if is_ready else "NOT_READY",
            service=settings.service_name,
            checks=checks,
            message=None if is_ready else "Uma ou mais dependências não estão disponíveis.",
        )
        if is_ready:
            return body
        logger.warning("Readiness falhou: %s", checks)
        return JSONResponse(status_code=503, content=body.model_dump(mode="json", by_alias=True),
                            headers={REQUEST_ID_HEADER: getattr(request.state, "request_id", "-")})

    return application


app = create_app()
