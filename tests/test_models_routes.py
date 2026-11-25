"""Rotas de modelos preditivos e agronomia: RBAC, treino real, persistência, projeção e auditoria."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest
from rbac_catalog import permissions_for

from app.config import Settings
from app.main import create_app

INTERNAL_KEY = "i" * 40
AUTH = {"Authorization": "Bearer user-jwt"}


def reading(stamp: datetime, temperature: float, humidity: float) -> dict[str, Any]:
    return {"channelId": "1", "entryId": int(stamp.timestamp()), "capturedAt": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "measurements": {"temperature": temperature, "humidity": humidity}, "qualityStatus": "VALID"}


class FakeJava:
    def __init__(self, role: str = "ADMIN", permissions: list[str] | None = None) -> None:
        self.role = role
        self.permissions = permissions_for(role) if permissions is None else permissions
        self.rows: list[dict[str, Any]] = []
        self.plantations: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []

    def seed_days(self, days: int, temperature: float = 26.0, humidity: float = 70.0) -> None:
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
        for hour in range(days * 24):
            stamp = start + timedelta(hours=hour)
            swing = 4.0 if 14 <= (stamp.hour - 3) % 24 <= 16 else (-4.0 if 4 <= (stamp.hour - 3) % 24 <= 6 else 0.0)
            self.rows.append(reading(stamp, temperature + swing, humidity))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@agro.test",
                "roleKey": self.role, "role": self.role, "status": "ACTIVE", "permissionKeys": self.permissions})
        if path == "/api/internal/climate-readings":
            return httpx.Response(200, json=self.rows)
        if path == "/api/plantations":
            return httpx.Response(200, json=self.plantations)
        if path == "/api/audit-events" and request.method == "POST":
            import json
            self.audit.append(json.loads(request.content))
            return httpx.Response(201)
        return httpx.Response(404, json={"message": "not found"})


def make_client(fake: FakeJava, tmp_path, **settings_kwargs):
    settings = Settings(java_api_url="http://java.test", internal_api_key=INTERNAL_KEY,
                        model_storage_dir=str(tmp_path / "models"), model_min_train_days=30, **settings_kwargs)
    app = create_app(settings)
    client = TestClient(app)
    return app, client


def test_new_routes_require_authentication(tmp_path):
    fake = FakeJava()
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        for path in ("/api/v1/models/status", "/api/v1/models/forecast", "/api/v1/agronomy/plantations"):
            assert client.get(path).status_code == 401
        assert client.post("/api/v1/models/train").status_code == 401
        assert client.delete("/api/v1/models/active").status_code == 401


def test_status_requires_analytics_permission(tmp_path):
    fake = FakeJava("PRODUCER", [p for p in permissions_for("PRODUCER") if p != "analytics.view"])
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        assert client.get("/api/v1/models/status", headers=AUTH).status_code == 403


def test_status_reports_insufficient_history_for_ml(tmp_path):
    fake = FakeJava("ANALYST")
    fake.seed_days(10)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        body = client.get("/api/v1/models/status", headers=AUTH).json()
        assert body["mlEnabled"] is False
        assert body["completeDays"] < 30
        assert body["activeModel"] is None
        assert body["statisticalAvailable"] is True


def test_producer_cannot_train_or_delete(tmp_path):
    fake = FakeJava("PRODUCER")
    fake.seed_days(45)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        assert client.post("/api/v1/models/train", headers=AUTH, json={}).status_code == 403
        assert client.delete("/api/v1/models/active", headers=AUTH).status_code == 403
        assert fake.audit == []


def test_train_rejects_insufficient_history_with_409(tmp_path):
    fake = FakeJava("ANALYST")
    fake.seed_days(12)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        response = client.post("/api/v1/models/train", headers=AUTH, json={"algorithm": "random_forest"})
        assert response.status_code == 409
        assert "insuficiente" in response.json()["message"].lower()


def test_train_persists_activates_and_audits_then_forecast_uses_ml(tmp_path):
    fake = FakeJava("ANALYST")
    fake.seed_days(45)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        response = client.post("/api/v1/models/train", headers=AUTH,
                               json={"algorithm": "random_forest", "horizonDays": 3})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"]["algorithm"] == "random_forest"
        assert body["model"]["trainingDays"] >= 30
        assert body["model"]["active"] is True
        assert body["model"]["metrics"]["temperature"]["samples"] > 0
        # Auditoria registrada em nome do usuário.
        assert any(event["action"] == "Treinou modelo preditivo" for event in fake.audit)

        # Status agora reflete o modelo ativo.
        status = client.get("/api/v1/models/status", headers=AUTH).json()
        assert status["activeModel"] is not None
        assert status["mlEnabled"] is True

        # Projeção passa a usar o modelo ML treinado.
        forecast = client.get("/api/v1/models/forecast", headers=AUTH).json()
        assert forecast["available"] is True
        assert forecast["modelKind"] == "ml"
        assert len(forecast["predictions"]) == 3
        assert forecast["modelId"] == body["model"]["modelId"]


def test_delete_active_requires_permission_and_removes_model(tmp_path):
    fake = FakeJava("ADMIN")
    fake.seed_days(45)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        client.post("/api/v1/models/train", headers=AUTH, json={"algorithm": "random_forest"})
        response = client.delete("/api/v1/models/active", headers=AUTH)
        assert response.status_code == 200
        assert response.json()["activeModel"] is None
        assert any(event["action"] == "Removeu modelo preditivo" for event in fake.audit)
        # Sem modelo ML, a projeção volta ao estatístico.
        forecast = client.get("/api/v1/models/forecast", headers=AUTH).json()
        assert forecast["modelKind"] == "statistical"


def test_forecast_falls_back_to_statistical_without_trained_model(tmp_path):
    fake = FakeJava("PRODUCER")
    fake.seed_days(20)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        forecast = client.get("/api/v1/models/forecast", headers=AUTH).json()
        assert forecast["available"] is True
        assert forecast["modelKind"] == "statistical"
        assert len(forecast["predictions"]) == 3


def test_dashboard_forecast_uses_model_service(tmp_path):
    fake = FakeJava("PRODUCER")
    fake.seed_days(20)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        forecast = client.get("/api/v1/dashboard/forecast", headers=AUTH).json()
        assert forecast["available"] is True
        assert forecast["modelKind"] == "statistical"
        assert "metrics" in forecast


def test_agronomy_requires_plantation_permission(tmp_path):
    fake = FakeJava("ANALYST", ["analytics.view"])  # sem plantations.read.all/manage
    fake.seed_days(40)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        assert client.get("/api/v1/agronomy/plantations", headers=AUTH).status_code == 403


def test_agronomy_returns_per_plantation_indicators(tmp_path):
    fake = FakeJava("PRODUCER")
    fake.seed_days(50)
    fake.plantations = [{
        "id": "33333333-3333-4333-8333-333333333333", "variety": "Crimson Seedless", "field": "Talhão A",
        "status": "Ativa", "plantedAt": (datetime.now(UTC) - timedelta(days=40)).strftime("%Y-%m-%dT12:00:00Z"),
    }]
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        response = client.get("/api/v1/agronomy/plantations", headers=AUTH)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["available"] is True
        assert len(body["plantations"]) == 1
        item = body["plantations"][0]
        assert item["variety"] == "Crimson Seedless"
        assert item["accumulatedGdd"] > 0
        assert item["irrigation"]["et0MmDay"] > 0
        assert item["harvest"] is not None
        assert body["assumptions"]


def test_agronomy_validates_parameters(tmp_path):
    fake = FakeJava("ADMIN")
    fake.seed_days(10)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        assert client.get("/api/v1/agronomy/plantations", headers=AUTH,
                          params={"base_temp": 30, "upper_temp": 25}).status_code == 400
        assert client.get("/api/v1/agronomy/plantations", headers=AUTH,
                          params={"target_gdd": 0}).status_code == 400


@pytest.mark.parametrize("role", ["ANALYST", "ADMIN"])
def test_analyst_and_admin_can_train(tmp_path, role):
    fake = FakeJava(role)
    fake.seed_days(45)
    app, client = make_client(fake, tmp_path)
    with client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        response = client.post("/api/v1/models/train", headers=AUTH, json={"algorithm": "random_forest"})
        assert response.status_code == 200, response.text
