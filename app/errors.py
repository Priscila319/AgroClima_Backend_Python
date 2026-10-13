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
    ).model_dump(mode="json")


async def service_error_handler(request: Request, exception: ServiceError) -> JSONResponse:
    return JSONResponse(
        status_code=exception.status_code,
        content=error_payload(request, exception.status_code, exception.message, exception.field_errors),
    )


async def http_error_handler(request: Request, exception: StarletteHTTPException) -> JSONResponse:
    message = exception.detail if isinstance(exception.detail, str) else "A requisição não pôde ser processada."
    return JSONResponse(
        status_code=exception.status_code,
        content=error_payload(request, exception.status_code, message),
    )


async def validation_error_handler(request: Request, exception: RequestValidationError) -> JSONResponse:
    fields: dict[str, str] = {}
    for error in exception.errors():
        location = ".".join(str(part) for part in error.get("loc", ()) if part != "body") or "request"
        fields.setdefault(location, error.get("msg", "Valor inválido."))
    return JSONResponse(
        status_code=400,
        content=error_payload(request, 400, "Revise os campos informados.", fields),
    )


async def unhandled_error_handler(request: Request, exception: Exception) -> JSONResponse:
    logging.getLogger("agroclima.api").error(
        "Unhandled request failure path=%s type=%s", request.url.path, type(exception).__name__
    )
    return JSONResponse(status_code=500, content=error_payload(request, 500, "Ocorreu um erro interno."))
