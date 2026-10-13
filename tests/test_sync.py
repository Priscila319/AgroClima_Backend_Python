import asyncio
from datetime import UTC, datetime

import httpx
import pytest

from app.config import Settings
from app.errors import ServiceError
from app.services.dashboard import DashboardService

JAVA_SYNC_RESULT = {
    "configured": True,
    "channelId": "3499301",
    "received": 3,
    "normalized": 2,
    "rejected": 1,
    "inserted": 2,
    "alreadyPresent": 0,
    "synchronizedAt": "2026-10-08T22:00:00Z",
    "message": "Sincronização concluída.",
}


def test_manual_sync_is_delegated_to_java_and_python_never_contacts_thingspeak():
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        assert request.url.host == "java.test", "Python deve falar apenas com a API Java"
        return httpx.Response(200, json=JAVA_SYNC_RESULT)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DashboardService(Settings(java_api_url="http://java.test"), client).sync_latest("Bearer admin")

    result = asyncio.run(run())
    assert result.inserted == 2
    assert result.rejected == 1
    assert result.channel_id == "3499301"
    assert observed[0].method == "POST"
    assert observed[0].url.path == "/api/integrations/thingspeak/sync"
    assert observed[0].headers["Authorization"] == "Bearer admin"
    assert "from" not in observed[0].url.params


def test_sync_window_is_forwarded_to_java_in_utc_z_format():
    observed: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        observed.append(request)
        return httpx.Response(200, json=JAVA_SYNC_RESULT)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DashboardService(Settings(java_api_url="http://java.test"), client).sync_latest(
                "Bearer admin", datetime(2026, 10, 7, tzinfo=UTC), datetime(2026, 10, 8, tzinfo=UTC))

    asyncio.run(run())
    assert dict(observed[0].url.params) == {"from": "2026-10-07T00:00:00Z", "to": "2026-10-08T00:00:00Z"}


def test_sync_window_requires_both_dates():
    async def run():
        async with httpx.AsyncClient() as client:
            await DashboardService(Settings(java_api_url="http://java.test"), client).sync_latest(
                "Bearer admin", datetime(2026, 10, 7, tzinfo=UTC), None)

    with pytest.raises(ServiceError) as error:
        asyncio.run(run())
    assert error.value.status_code == 400
