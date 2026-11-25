"""RBAC: as rotas analíticas exigem as permissões do catálogo da API Java.

O Java é a autoridade (JWT + role_permissions); o Python consome as permissões
efetivas devolvidas em /api/auth/me e bloqueia com 403 quem não as possui.
"""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import httpx
import pytest
from rbac_catalog import permissions_for

from app.config import Settings
from app.main import create_app

KEY = "k" * 40
AUTH = {"Authorization": "Bearer user-jwt"}


def _fake_java(role: str = "PRODUCER", permissions: list[str] | None = None, calls: list | None = None):
    keys = permissions_for(role) if permissions is None else permissions

    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if calls is not None:
            calls.append((request.method, path))
        if path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@a.test",
                "roleKey": role, "role": role, "status": "ACTIVE", "permissionKeys": keys})
        if path == "/api/plantations":
            return httpx.Response(200, json=[])
        if path == "/api/integrations/thingspeak/status":
            return httpx.Response(200, json={
                "configured": True, "channelId": "1", "readKeyConfigured": True,
                "fieldMap": {"temperature": "field1", "humidity": "field2"}})
        if path == "/api/integrations/thingspeak/sync" and request.method == "POST":
            return httpx.Response(200, json={
                "configured": True, "channelId": "1", "received": 0, "normalized": 0, "rejected": 0,
                "inserted": 0, "alreadyPresent": 0, "synchronizedAt": "2026-10-08T22:00:00Z",
                "message": "Sincronização concluída."})
        if path == "/api/internal/climate-readings":
            captured = (datetime.now(UTC) - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            return httpx.Response(200, json=[{
                "channelId": "1", "entryId": 1, "capturedAt": captured,
                "measurements": {"temperature": 28.0, "humidity": 60.0}, "qualityStatus": "VALID"}])
        return httpx.Response(404, json={"message": "?"})

    return handler


def _get(fake, path: str) -> httpx.Response:
    app = create_app(Settings(java_api_url="http://java.test", internal_api_key=KEY))
    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        return client.get(path, headers=AUTH)


def _post(fake, path: str) -> httpx.Response:
    app = create_app(Settings(java_api_url="http://java.test", internal_api_key=KEY))
    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        return client.post(path, headers=AUTH)


def _without(role: str, permission: str) -> list[str]:
    return [key for key in permissions_for(role) if key != permission]


# Endpoint → única permissão que o guarda. O papel NÃO faz parte da regra.
PERMISSION_GUARDED_ENDPOINTS = [
    ("/api/v1/dashboard/producer", "overview.view"),
    ("/api/v1/dashboard/analyst", "analytics.view"),
    ("/api/v1/dashboard/quality", "analytics.view"),
    ("/api/v1/dashboard/comparison", "analytics.view"),
]
ROLES_UNDER_TEST = ["PRODUCER", "ANALYST", "ADMIN", "CUSTOM_ROLE"]

def test_producer_with_monitoring_permission_reads_climate():
    response = _get(_fake_java("PRODUCER"), "/api/v1/climate/current")
    assert response.status_code == 200


def test_producer_without_monitoring_permission_is_forbidden():
    keys = [key for key in permissions_for("PRODUCER") if key != "monitoring.view"]
    response = _get(_fake_java("PRODUCER", keys), "/api/v1/climate/current")
    assert response.status_code == 403
    assert "permissão" in response.json()["message"]


def test_climate_history_requires_monitoring_view():
    keys = _without("PRODUCER", "monitoring.view")
    response = _get(_fake_java("PRODUCER", keys), "/api/v1/climate/history")
    assert response.status_code == 403
    assert "permissão" in response.json()["message"]


def test_climate_history_is_available_with_monitoring_view():
    response = _get(_fake_java("PRODUCER"), "/api/v1/climate/history")
    assert response.status_code == 200


def test_env_example_does_not_ship_a_real_internal_api_key():
    from pathlib import Path

    for line in (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8").splitlines():
        if line.startswith("INTERNAL_API_KEY="):
            assert line.split("=", 1)[1].strip() == ""


def test_profile_without_permissions_is_denied_by_default():
    response = _get(_fake_java("PRODUCER", []), "/api/v1/dashboard/summary")
    assert response.status_code == 403


def test_legacy_profile_without_permission_keys_is_denied():
    async def legacy(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@a.test",
                "roleKey": "PRODUCER", "role": "PRODUCER", "status": "ACTIVE"})
        return httpx.Response(404, json={"message": "?"})

    response = _get(legacy, "/api/v1/dashboard/summary")
    assert response.status_code == 403


def test_producer_without_analytics_permission_cannot_open_forecast_or_market():
    keys = [key for key in permissions_for("PRODUCER") if key != "analytics.view"]
    fake = _fake_java("PRODUCER", keys)
    assert _get(fake, "/api/v1/dashboard/forecast").status_code == 403
    assert _get(fake, "/api/v1/dashboard/market").status_code == 403


def test_admin_keeps_access_to_analytics_and_climate():
    fake = _fake_java("ADMIN")
    assert _get(fake, "/api/v1/climate/current").status_code == 200
    assert _get(fake, "/api/v1/dashboard/analyst").status_code == 200


@pytest.mark.parametrize("role", ROLES_UNDER_TEST)
@pytest.mark.parametrize(("path", "permission"), PERMISSION_GUARDED_ENDPOINTS)
def test_permission_alone_grants_access_regardless_of_role(role, path, permission):
    # Só a permissão exigida pelo endpoint, nenhuma outra: o papel não pode influenciar o resultado.
    fake = _fake_java(role, [permission])
    assert _get(fake, path).status_code == 200


@pytest.mark.parametrize("role", ROLES_UNDER_TEST)
@pytest.mark.parametrize(("path", "permission"), PERMISSION_GUARDED_ENDPOINTS)
def test_missing_permission_is_forbidden_even_with_privileged_role(role, path, permission):
    # Remover _require_role() não pode deixar o endpoint aberto: sem a permissão é 403, inclusive para ADMIN.
    keys = [key for key in permissions_for("ADMIN") if key != permission]
    fake = _fake_java(role, keys)
    response = _get(fake, path)
    assert response.status_code == 403
    assert "permissão" in response.json()["message"]


@pytest.mark.parametrize(("path", "_permission"), PERMISSION_GUARDED_ENDPOINTS)
def test_profile_without_any_permission_is_forbidden_on_guarded_dashboards(path, _permission):
    for role in ("PRODUCER", "ANALYST", "ADMIN"):
        assert _get(_fake_java(role, []), path).status_code == 403


def test_producer_with_analytics_view_can_open_comparison():
    fake = _fake_java("PRODUCER")
    assert "analytics.view" in permissions_for("PRODUCER")
    assert _get(fake, "/api/v1/dashboard/comparison").status_code == 200


def test_non_analyst_role_with_analytics_view_can_open_comparison():
    fake = _fake_java("CUSTOM_ROLE", ["analytics.view"])
    assert _get(fake, "/api/v1/dashboard/comparison").status_code == 200


def test_producer_without_analytics_view_cannot_open_comparison():
    fake = _fake_java("PRODUCER", _without("PRODUCER", "analytics.view"))
    assert _get(fake, "/api/v1/dashboard/comparison").status_code == 403


def test_producer_with_analytics_view_can_open_analyst_dashboard_and_quality():
    # Antes da correção: 403 por papel mesmo com a permissão concedida pelo administrador.
    fake = _fake_java("PRODUCER")
    assert _get(fake, "/api/v1/dashboard/analyst").status_code == 200
    assert _get(fake, "/api/v1/dashboard/quality").status_code == 200


def test_analyst_without_analytics_view_cannot_open_analyst_dashboard():
    fake = _fake_java("ANALYST", _without("ANALYST", "analytics.view"))
    assert _get(fake, "/api/v1/dashboard/analyst").status_code == 403


def test_producer_dashboard_alias_depends_on_overview_view_only():
    assert _get(_fake_java("ANALYST"), "/api/v1/dashboard/producer").status_code == 200
    assert _get(_fake_java("ANALYST", _without("ANALYST", "overview.view")),
                "/api/v1/dashboard/producer").status_code == 403


@pytest.mark.parametrize("role", ["PRODUCER", "ANALYST"])
def test_thingspeak_status_stays_admin_only_even_with_integrations_permission(role):
    calls: list = []
    keys = permissions_for("ADMIN")  # todas as permissões, inclusive integrations.manage
    response = _get(_fake_java(role, keys, calls), "/api/v1/integrations/thingspeak/status")
    assert response.status_code == 403
    assert not any(path.startswith("/api/integrations/thingspeak") for _, path in calls), \
        "o guard do Python deve barrar antes de chamar o Java"


@pytest.mark.parametrize("role", ["PRODUCER", "ANALYST"])
def test_thingspeak_sync_stays_admin_only_even_with_integrations_permission(role):
    calls: list = []
    keys = permissions_for("ADMIN")
    response = _post(_fake_java(role, keys, calls), "/api/v1/integrations/thingspeak/sync")
    assert response.status_code == 403
    assert ("POST", "/api/integrations/thingspeak/sync") not in calls


def test_thingspeak_endpoints_are_available_to_admin():
    calls: list = []
    fake = _fake_java("ADMIN", None, calls)
    assert _get(fake, "/api/v1/integrations/thingspeak/status").status_code == 200
    assert _post(fake, "/api/v1/integrations/thingspeak/sync").status_code == 200
    assert ("POST", "/api/integrations/thingspeak/sync") in calls
