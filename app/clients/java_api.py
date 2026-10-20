from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import Any

import httpx

from app.config import Settings
from app.errors import ServiceError

logger = logging.getLogger("agroclima.java_client")

# Falhas transitórias: vale tentar de novo (apenas em GET, que é idempotente).
RETRYABLE_STATUS = frozenset({502, 503, 504})


class JavaApiClient:
    """Único ponto de acesso do Python à API Java.

    - Usuário: o JWT do usuário é repassado para as rotas que a API Java autoriza (perfil, plantações,
      status e sincronização do ThingSpeak).
    - Serviço: leituras climáticas são lidas pela rota interna protegida por INTERNAL_API_KEY.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.base_url = settings.java_api_url.rstrip("/")

    async def current_user(self, authorization: str) -> dict[str, Any]:
        return await self._request("GET", "/api/auth/me", authorization=authorization)

    async def plantations(self, authorization: str) -> list[dict[str, Any]]:
        result = await self._request("GET", "/api/plantations", authorization=authorization)
        if not isinstance(result, list):
            raise ServiceError(502, "A API Java retornou uma resposta inválida para plantações.")
        return result

    async def climate_readings(self, start: datetime, end: datetime,
                               channel_id: str | None = None) -> list[dict[str, Any]]:
        """Leituras de temperatura e umidade persistidas pela API Java, lidas com a credencial de serviço."""
        if not self.settings.internal_key_configured:
            raise ServiceError(503, "A integração interna com a API Java não está configurada (INTERNAL_API_KEY).")
        params: dict[str, str] = {"from": _utc_instant(start), "to": _utc_instant(end)}
        if channel_id:
            params["channelId"] = channel_id
        result = await self._request(
            "GET", "/api/internal/climate-readings", params=params, headers=self._service_headers(),
        )
        if not isinstance(result, list):
            raise ServiceError(502, "A API Java retornou uma resposta inválida para leituras climáticas.")
        return result

    async def thingspeak_status(self, authorization: str) -> dict[str, Any]:
        result = await self._request("GET", "/api/integrations/thingspeak/status", authorization=authorization)
        if not isinstance(result, dict):
            raise ServiceError(502, "A API Java retornou um status de integração inválido.")
        return result

    async def thingspeak_sync(self, authorization: str, start: datetime | None = None,
                              end: datetime | None = None) -> dict[str, Any]:
        params: dict[str, str] = {}
        if start and end:
            params = {"from": _utc_instant(start), "to": _utc_instant(end)}
        result = await self._request("POST", "/api/integrations/thingspeak/sync", authorization=authorization,
                                     params=params or None)
        if not isinstance(result, dict):
            raise ServiceError(502, "A API Java retornou um resultado de sincronização inválido.")
        return result

    def _service_headers(self) -> dict[str, str]:
        return {"X-Internal-Api-Key": self.settings.internal_api_key.get_secret_value()}

    async def ping(self) -> bool:
        """Verifica se a API Java responde (usado por /ready). Qualquer resposta < 500 conta como "no ar"."""
        try:
            response = await self.client.get(f"{self.base_url}/actuator/health", timeout=3.0)
        except httpx.RequestError as exception:
            logger.warning("API Java inacessível no /ready: %s", type(exception).__name__)
            return False
        return response.status_code < 500

    async def _request(self, method: str, path: str, *, authorization: str | None = None,
                       params: dict[str, str] | None = None, json: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None) -> Any:
        request_headers = dict(headers or {})
        if authorization:
            request_headers["Authorization"] = authorization
        # POST (sincronização) nunca é repetido: poderia disparar a operação duas vezes.
        attempts = self.settings.java_retry_attempts if method == "GET" else 1

        for attempt in range(1, attempts + 1):
            is_last = attempt == attempts
            try:
                response = await self.client.request(method, f"{self.base_url}{path}", params=params,
                                                     json=json, headers=request_headers)
            except httpx.TimeoutException as exception:
                if not is_last:
                    await self._backoff(attempt, attempts, method, path, "timeout")
                    continue
                logger.exception("Timeout na API Java method=%s path=%s tentativas=%d", method, path, attempt)
                raise ServiceError(503, "A API Java excedeu o tempo de resposta.") from exception
            except httpx.RequestError as exception:
                if not is_last:
                    await self._backoff(attempt, attempts, method, path, type(exception).__name__)
                    continue
                logger.exception("Falha de conexão com a API Java method=%s path=%s tentativas=%d",
                                 method, path, attempt)
                raise ServiceError(503, "Não foi possível conectar à API Java.") from exception

            if response.status_code in RETRYABLE_STATUS and not is_last:
                await self._backoff(attempt, attempts, method, path, f"HTTP {response.status_code}")
                continue
            return self._parse(response)

        raise AssertionError("unreachable: o laço sempre retorna ou levanta na última tentativa")

    async def _backoff(self, attempt: int, attempts: int, method: str, path: str, reason: str) -> None:
        delay = self.settings.java_retry_backoff_seconds * (2 ** (attempt - 1))
        logger.warning("API Java falhou (%s) method=%s path=%s tentativa=%d/%d; nova tentativa em %.2fs",
                       reason, method, path, attempt, attempts, delay)
        await asyncio.sleep(delay)

    @staticmethod
    def _parse(response: httpx.Response) -> Any:
        if response.status_code == 401:
            raise ServiceError(401, "Sessão inválida ou expirada.")
        if response.status_code == 403:
            raise ServiceError(403, "Seu perfil não tem permissão para este recurso.")
        if response.status_code == 503:
            raise ServiceError(503, _java_message(response, "A integração com a API Java não está disponível."))
        if response.status_code >= 500:
            raise ServiceError(503, "A API Java está temporariamente indisponível.")
        if response.status_code >= 400:
            raise ServiceError(502, _java_message(response, "A API Java rejeitou a solicitação entre serviços."))
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError as exception:
            raise ServiceError(502, "A API Java retornou conteúdo inválido.") from exception


def _java_message(response: httpx.Response, fallback: str) -> str:
    """Repassa apenas a mensagem amigável do erro Java (sem stack traces ou segredos)."""
    try:
        message = response.json().get("message")
    except (ValueError, AttributeError):
        return fallback
    return message if isinstance(message, str) and message else fallback


def _utc_instant(value: datetime) -> str:
    """ISO 8601 em UTC com sufixo Z, aceito pelo parser Instant do Spring."""
    aware = value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    return aware.strftime("%Y-%m-%dT%H:%M:%SZ")
