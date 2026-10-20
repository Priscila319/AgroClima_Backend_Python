from __future__ import annotations

from contextvars import ContextVar
import logging
import re
from typing import Any
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
LOG_FORMAT = "%(asctime)s %(levelname)s [%(request_id)s] %(name)s %(message)s"

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Aceita só um id simples vindo do cliente (evita injeção de quebra de linha nos logs).
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


def get_request_id() -> str:
    return request_id_var.get()


def configure_logging() -> None:
    """Formato único de log com request_id em todos os registros (inclusive de bibliotecas)."""
    previous_factory = logging.getLogRecordFactory()

    def factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = previous_factory(*args, **kwargs)
        record.request_id = request_id_var.get()
        return record

    logging.setLogRecordFactory(factory)
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)


class RequestIdMiddleware:
    """Gera (ou reaproveita) o X-Request-ID, guarda no contexto de log e devolve no response."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = ""
        for name, value in scope.get("headers", []):
            if name == REQUEST_ID_HEADER.lower().encode("latin-1"):
                incoming = value.decode("latin-1")
                break
        request_id = incoming if _VALID_REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            request_id_var.reset(token)
