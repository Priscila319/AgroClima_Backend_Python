import asyncio

import httpx

from app.config import Settings
from app.services.dashboard import DashboardService


def test_plantation_dashboard_summarizes_java_api_rows_by_variety():
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer user-jwt"
        assert request.url.path == "/api/plantations"
        return httpx.Response(200, json=[
            {"variety": "Syrah", "quantity": 120, "status": "Ativa", "harvestedAt": None, "archivedAt": None},
            {"variety": "Syrah", "quantity": 80, "status": "Colhida", "harvestedAt": "2026-10-01T00:00:00Z", "archivedAt": None},
            {"variety": "Isabel", "quantity": 40, "status": "Arquivada", "harvestedAt": None, "archivedAt": "2026-10-02T00:00:00Z"},
        ])

    async def run():
        settings = Settings(java_api_url="http://java.test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DashboardService(settings, client).plantation_summary("Bearer user-jwt")

    summary, _ = asyncio.run(run())
    assert summary.total_plantations == 3
    assert summary.growing == 1
    assert summary.harvested == 1
    assert summary.archived == 1
    assert summary.total_plants == 240
    assert summary.by_variety[1].variety == "Syrah"
    assert summary.by_variety[1].plants == 200
    assert summary.by_variety[1].growing == 1
    assert summary.by_variety[1].harvested == 1


def test_java_authentication_error_is_not_hidden_as_a_success():
    async def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "invalid"})

    async def run():
        settings = Settings(java_api_url="http://java.test")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            from app.clients.java_api import JavaApiClient
            from app.errors import ServiceError
            try:
                await JavaApiClient(settings, client).current_user("Bearer expired")
            except ServiceError as error:
                return error.status_code
        return None

    assert asyncio.run(run()) == 401
