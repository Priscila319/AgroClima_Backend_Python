"""Etapa 2: rotas unificadas, response_model, enum, request_id, retry com backoff e /ready."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
import logging

from fastapi.testclient import TestClient
import httpx
import pytest

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError
from app.main import create_app
from app.models import PlantationStatus
from app.services.dashboard import classify_plantation

KEY = "k" * 40
AUTH = {"Authorization": "Bearer user-jwt"}


def _settings(**overrides) -> Settings:
    base = {"java_api_url": "http://java.test", "internal_api_key": KEY, "java_retry_backoff_seconds": 0}
    return Settings(**{**base, **overrides})


def _java(role: str = "ADMIN", ready_status: int = 200):
    async def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@a.test",
                "roleKey": role, "role": role, "status": "ACTIVE"})
        if path == "/api/plantations":
            return httpx.Response(200, json=[{"variety": "Syrah", "quantity": 10, "status": "Ativa"}])
        if path == "/api/internal/climate-readings":
            return httpx.Response(200, json=[])
        if path == "/api/integrations/thingspeak/status":
            return httpx.Response(200, json={"configured": True, "channelId": "1", "lastEntryId": 42})
        if path == "/actuator/health":
            return httpx.Response(ready_status, json={})
        return httpx.Response(404, json={"message": "?"})
    return handler


@contextmanager
def _client(handler, **settings) -> Iterator[TestClient]:
    app = create_app(_settings(**settings))
    with TestClient(app, raise_server_exceptions=False) as client:
        # O lifespan já criou o client real; troca por um com transporte falso (como nos testes da Etapa 1).
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        yield client


# ---------- Enum de status ----------

@pytest.mark.parametrize("row, expected", [
    ({"status": "Ativa"}, PlantationStatus.GROWING),
    ({"status": "Colhida"}, PlantationStatus.HARVESTED),
    ({"status": "x", "harvestedAt": "2026-10-01T00:00:00Z"}, PlantationStatus.HARVESTED),
    ({"status": "Arquivada"}, PlantationStatus.ARCHIVED),
    ({"status": "Colhida", "archivedAt": "2026-10-02T00:00:00Z"}, PlantationStatus.ARCHIVED),
    ({}, PlantationStatus.GROWING),
])
def test_plantation_status_is_normalized_to_enum(row, expected):
    assert classify_plantation(row) is expected


# ---------- Rotas unificadas + response_model ----------

def test_every_route_declares_a_response_model_in_openapi():
    schema = create_app(_settings()).openapi()
    missing = []
    for path, methods in schema["paths"].items():
        for method, operation in methods.items():
            ok = operation["responses"].get("200", {}).get("content", {}).get("application/json", {}).get("schema")
            if not ok or ok == {}:
                missing.append(f"{method.upper()} {path}")
    assert missing == []
    assert schema["paths"]["/api/v1/dashboard/producer"]["get"]["deprecated"] is True


def test_producer_alias_returns_same_payload_as_summary_and_keeps_role_check():
    with _client(_java("ADMIN")) as admin:
        summary = admin.get("/api/v1/dashboard/summary", headers=AUTH)
        producer = admin.get("/api/v1/dashboard/producer", headers=AUTH)
    assert summary.status_code == producer.status_code == 200
    assert summary.json()["plantations"] == producer.json()["plantations"]
    with _client(_java("ANALYST")) as analyst:
        assert analyst.get("/api/v1/dashboard/producer", headers=AUTH).status_code == 403


def test_comparison_and_integration_status_use_service_and_keep_extra_java_fields():
    with _client(_java("ADMIN")) as client:
        comparison = client.get("/api/v1/dashboard/comparison", headers=AUTH)
        status = client.get("/api/v1/integrations/thingspeak/status", headers=AUTH)
    assert comparison.status_code == 200
    assert comparison.json()["plantationsByVariety"][0]["variety"] == "Syrah"
    assert status.json() == {"configured": True, "channelId": "1", "message": None, "lastEntryId": 42}


# ---------- request_id + logs ----------

def test_request_id_is_generated_echoed_and_sanitized():
    with _client(_java()) as client:
        generated = client.get("/health")
        echoed = client.get("/health", headers={"X-Request-ID": "abc-12345678"})
        unsafe = client.get("/health", headers={"X-Request-ID": "x\ninjetado"})
    assert len(generated.headers["X-Request-ID"]) == 32
    assert echoed.headers["X-Request-ID"] == "abc-12345678"
    assert "injetado" not in unsafe.headers["X-Request-ID"]


def test_error_payload_carries_request_id():
    with _client(_java()) as client:
        response = client.get("/api/v1/dashboard/summary", headers={"X-Request-ID": "req-123456789"})
    assert response.status_code == 401
    assert response.json()["requestId"] == "req-123456789"
    assert response.headers["X-Request-ID"] == "req-123456789"


def test_unhandled_error_is_logged_with_traceback_and_request_id(caplog):
    app = create_app(_settings())

    @app.get("/boom", include_in_schema=False)
    async def boom():
        raise RuntimeError("falha-simulada")

    caplog.set_level(logging.ERROR, logger="agroclima.api")
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/boom", headers={"X-Request-ID": "req-boom-0001"})

    assert response.status_code == 500
    assert response.json()["message"] == "Ocorreu um erro interno."
    assert response.json()["requestId"] == "req-boom-0001"
    assert "falha-simulada" not in response.text
    record = next(r for r in caplog.records if "Erro não tratado" in r.getMessage())
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert record.request_id == "req-boom-0001"


# ---------- Retry com backoff ----------

def _run(coro):
    return asyncio.run(coro)


def test_get_is_retried_on_transient_errors_and_recovers(monkeypatch):
    delays: list[float] = []

    async def fake_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr("app.clients.java_api.asyncio.sleep", fake_sleep)
    calls = {"n": 0}

    async def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, json={}) if calls["n"] < 3 else httpx.Response(200, json={"id": "ok"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            settings = _settings(java_retry_attempts=3, java_retry_backoff_seconds=0.5)
            return await JavaApiClient(settings, http).current_user("Bearer t")

    assert _run(run()) == {"id": "ok"}
    assert calls["n"] == 3
    assert delays == [0.5, 1.0]  # backoff exponencial


def test_retry_gives_up_after_configured_attempts_with_503():
    calls = {"n": 0}

    async def handler(_: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("recusou")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            await JavaApiClient(_settings(java_retry_attempts=3), http).current_user("Bearer t")

    with pytest.raises(ServiceError) as error:
        _run(run())
    assert error.value.status_code == 503
    assert calls["n"] == 3


def test_post_sync_is_never_retried_and_client_errors_are_not_retried():
    calls = {"post": 0, "get": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            calls["post"] += 1
            return httpx.Response(503, json={"message": "fora"})
        calls["get"] += 1
        return httpx.Response(401, json={})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            java = JavaApiClient(_settings(java_retry_attempts=3), http)
            with pytest.raises(ServiceError):
                await java.thingspeak_sync("Bearer t")
            with pytest.raises(ServiceError):
                await java.current_user("Bearer t")

    _run(run())
    assert calls == {"post": 1, "get": 1}


def test_retry_settings_are_validated():
    with pytest.raises(ValueError):
        Settings(java_retry_attempts=0)
    with pytest.raises(ValueError):
        Settings(java_retry_backoff_seconds=-1)


# ---------- /ready ----------

def test_ready_is_200_when_key_and_java_are_ok():
    with _client(_java()) as client:
        response = client.get("/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "READY"
    assert response.json()["checks"] == {"internalKey": True, "javaApi": True}


def test_ready_is_503_when_java_is_down_or_key_is_missing():
    with _client(_java(ready_status=503)) as down:
        response = down.get("/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "NOT_READY"
    assert response.json()["checks"]["javaApi"] is False
    assert "X-Request-ID" in response.headers

    with _client(_java(), internal_api_key="curta") as no_key:
        assert no_key.get("/ready").json()["checks"]["internalKey"] is False


def test_ready_treats_404_from_java_as_reachable_and_connection_error_as_down():
    with _client(_java(ready_status=404)) as client:
        assert client.get("/ready").status_code == 200

    async def refused(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("recusou")

    with _client(refused) as client:
        assert client.get("/ready").status_code == 503
