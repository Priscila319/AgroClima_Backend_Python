from __future__ import annotations

from datetime import UTC, datetime
from http import HTTPStatus
import logging
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.logging_config import REQUEST_ID_HEADER, request_id_var

logger = logging.getLogger("agroclima.api")


class ServiceError(Exception):
    def __init__(self, status_code: int, message: str, field_errors: dict[str, str] | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.field_errors = field_errors or {}


class ApiError(BaseModel):
    model_config = ConfigDict(alias_generator=lambda value: value, populate_by_name=True)
    timestamp: datetime
    status: int
    error: str
    message: str
    path: str
    fieldErrors: dict[str, str]
    requestId: str | None = None


def _request_id(request: Request) -> str | None:
    return getattr(request.state, "request_id", None)


def error_payload(request: Request, status: int, message: str,
                  field_errors: dict[str, str] | None = None) -> dict[str, Any]:
    try:
        error_name = HTTPStatus(status).phrase
    except ValueError:
        error_name = "Error"
    return ApiError(
        timestamp=datetime.now(UTC),
        status=status,
        error=error_name,
        message=message,
        path=request.url.path,
        fieldErrors=field_errors or {},
        requestId=_request_id(request),
    ).model_dump(mode="json")


def _error_response(request: Request, status: int, message: str,
                    field_errors: dict[str, str] | None = None) -> JSONResponse:
    headers = {REQUEST_ID_HEADER: rid} if (rid := _request_id(request)) else None
    return JSONResponse(status_code=status, content=error_payload(request, status, message, field_errors),
                        headers=headers)


async def service_error_handler(request: Request, exception: ServiceError) -> JSONResponse:
    if exception.status_code >= 500:
        # 5xx de negócio (ex.: Java fora do ar) merece rastro; a causa original vem em __cause__.
        logger.error("Falha de serviço status=%s method=%s path=%s: %s", exception.status_code,
                     request.method, request.url.path, exception.message, exc_info=exception.__cause__)
    return _error_response(request, exception.status_code, exception.message, exception.field_errors)


async def http_error_handler(request: Request, exception: StarletteHTTPException) -> JSONResponse:
    message = exception.detail if isinstance(exception.detail, str) else "A requisição não pôde ser processada."
    return _error_response(request, exception.status_code, message)


async def validation_error_handler(request: Request, exception: RequestValidationError) -> JSONResponse:
    fields: dict[str, str] = {}
    for error in exception.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body") or "request"
        fields.setdefault(location, error.get("msg", "Valor inválido."))
    return _error_response(request, 400, "Revise os campos informados.", fields)


async def unhandled_error_handler(request: Request, exception: Exception) -> JSONResponse:
    # Este handler roda fora do RequestIdMiddleware (no ServerErrorMiddleware): restaura o id para o log.
    token = request_id_var.set(_request_id(request) or "-")
    try:
        # logger.exception registra o traceback completo; o cliente recebe só o request_id para suporte.
        logger.exception("Erro não tratado method=%s path=%s", request.method, request.url.path,
                         exc_info=exception)
    finally:
        request_id_var.reset(token)
    return _error_response(request, 500, "Ocorreu um erro interno.")
