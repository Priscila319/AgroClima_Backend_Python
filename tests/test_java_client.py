import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError

KEY = "k" * 40


def _settings() -> Settings:
    return Settings(java_api_url="http://java.test", internal_api_key=KEY)


def test_climate_readings_use_internal_key_and_utc_z_window_without_user_token():
    observed: dict = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        observed["path"] = request.url.path
        observed["params"] = dict(request.url.params)
        observed["internal"] = request.headers.get("X-Internal-Api-Key")
        observed["authorization"] = request.headers.get("Authorization")
        return httpx.Response(200, json=[])

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await JavaApiClient(_settings(), http).climate_readings(
                datetime(2026, 10, 7, 21, 0, tzinfo=UTC), datetime(2026, 10, 8, 3, 30, tzinfo=UTC), "3499301")

    assert asyncio.run(run()) == []
    assert observed["path"] == "/api/internal/climate-readings"
    assert observed["internal"] == KEY
    assert observed["authorization"] is None
    assert observed["params"] == {"from": "2026-10-07T21:00:00Z", "to": "2026-10-08T03:30:00Z", "channelId": "3499301"}


def test_climate_readings_refuse_to_call_java_without_a_strong_internal_key():
    async def handler(_: httpx.Request) -> httpx.Response:  # pragma: no cover - must not be reached
        raise AssertionError("Java must not be called without INTERNAL_API_KEY")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await JavaApiClient(Settings(java_api_url="http://java.test", internal_api_key="curta"), http).climate_readings(
                datetime(2026, 10, 7, tzinfo=UTC), datetime(2026, 10, 8, tzinfo=UTC))

    with pytest.raises(ServiceError) as error:
        asyncio.run(run())
    assert error.value.status_code == 503


def test_java_rejection_is_surfaced_with_java_message_and_no_stack_trace():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"message": "Umidade deve estar entre 0 e 100.", "trace": "secret"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await JavaApiClient(_settings(), http).thingspeak_status("Bearer t")

    with pytest.raises(ServiceError) as error:
        asyncio.run(run())
    assert error.value.status_code == 502
    assert error.value.message == "Umidade deve estar entre 0 e 100."
