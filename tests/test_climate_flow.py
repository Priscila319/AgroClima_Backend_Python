"""Fluxo de temperatura e umidade após a reestruturação.

ThingSpeak -> API Java (única integração externa) -> leitura interna pelo Python -> resposta ao frontend.
A API Java é simulada por MockTransport com a mesma persistência em memória e as mesmas rotas
(auth, sincronização, leitura interna). A aplicação Python roda de verdade (FastAPI + rotas + análise).
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

INTERNAL_KEY = "i" * 40
CHANNEL_ID = "3499301"
UTC_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


class FakeJava:
    def __init__(self, role_key: str = "PRODUCER"):
        self.role_key = role_key
        self.stored: dict[tuple[str, int], dict] = {}
        self.calls: list[tuple[str, str]] = []
        self.hosts: set[str] = set()

    def seed(self, captured_at: datetime, entry_id: int, temperature: float, humidity: float) -> None:
        """Simula leituras já persistidas pela sincronização do backend Java."""
        self.stored[(CHANNEL_ID, entry_id)] = {
            "id": f"row-{entry_id}", "channelId": CHANNEL_ID, "entryId": entry_id,
            "sensorCode": f"thingspeak-{CHANNEL_ID}", "capturedAt": captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "measurements": {"temperature": temperature, "humidity": humidity}, "qualityStatus": "VALID",
        }

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.hosts.add(request.url.host)
        self.calls.append((request.method, request.url.path))

        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "Usuário", "email": "u@agro.test",
                "roleKey": self.role_key, "role": self.role_key, "status": "ACTIVE",
            })

        if request.url.path == "/api/integrations/thingspeak/sync" and request.method == "POST":
            if self.role_key != "ADMIN":
                return httpx.Response(403, json={"message": "Acesso negado."})
            return httpx.Response(200, json={
                "configured": True, "channelId": CHANNEL_ID, "received": 3, "normalized": 2, "rejected": 1,
                "inserted": 0, "alreadyPresent": 2, "synchronizedAt": "2026-10-08T22:00:00Z",
                "message": "Sincronização concluída.",
            })

        if request.url.path == "/api/integrations/thingspeak/status":
            return httpx.Response(200, json={
                "configured": True, "channelId": CHANNEL_ID, "readKeyConfigured": True,
                "fieldMap": {"temperature": "field1", "humidity": "field2"},
            })

        if request.url.path == "/api/internal/climate-readings":
            if request.headers.get("X-Internal-Api-Key") != INTERNAL_KEY:
                return httpx.Response(401, json={"message": "Credencial de serviço inválida."})
            assert UTC_Z.match(request.url.params["from"]), request.url.params["from"]
            assert UTC_Z.match(request.url.params["to"]), request.url.params["to"]
            start = datetime.fromisoformat(request.url.params["from"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(request.url.params["to"].replace("Z", "+00:00"))
            rows = [
                item for item in self.stored.values()
                if start <= datetime.fromisoformat(item["capturedAt"].replace("Z", "+00:00")) <= end
            ]
            return httpx.Response(200, json=sorted(rows, key=lambda row: row["capturedAt"]))

        return httpx.Response(404, json={"message": "not found in fake java"})


def _app(fake: FakeJava):
    settings = Settings(java_api_url="http://java.test", internal_api_key=INTERNAL_KEY)
    return create_app(settings)


def test_temperature_and_humidity_flow_from_java_to_frontend_response():
    now = datetime.now(UTC).replace(microsecond=0)
    fake = FakeJava(role_key="ADMIN")
    fake.seed(now - timedelta(minutes=30), 201, 24.5, 68.0)
    fake.seed(now - timedelta(minutes=10), 202, 26.0, 63.5)
    fake.seed(now - timedelta(minutes=3), 203, 27.2, 60.1)
    app = _app(fake)

    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        sync = client.post("/api/v1/integrations/thingspeak/sync", headers={"Authorization": "Bearer admin-jwt"})
        current = client.get("/api/v1/climate/current", headers={"Authorization": "Bearer producer-jwt"})

    assert sync.status_code == 200, sync.text
    assert sync.json()["rejected"] == 1
    assert ("POST", "/api/integrations/thingspeak/sync") in fake.calls
    assert fake.hosts == {"java.test"}, "Python não deve acessar nenhum host externo"

    assert current.status_code == 200, current.text
    body = current.json()
    assert body["available"] is True
    assert body["records"] == 3
    assert body["channelId"] == CHANNEL_ID
    assert body["source"] == "ThingSpeak"
    assert set(body["metrics"]) == {"temperature", "humidity"}
    temperature = body["metrics"]["temperature"]
    assert temperature["current"] == 27.2
    assert temperature["average"] == pytest.approx(25.9)
    assert temperature["minimum"] == 24.5
    assert temperature["maximum"] == 27.2
    assert temperature["unit"] == "°C"
    assert temperature["samples"] == 3
    assert body["metrics"]["humidity"]["current"] == 60.1
    assert body["metrics"]["humidity"]["unit"] == "%"
    assert [point["values"] for point in body["series"]] == [
        {"temperature": 24.5, "humidity": 68.0},
        {"temperature": 26.0, "humidity": 63.5},
        {"temperature": 27.2, "humidity": 60.1},
    ]


def test_climate_reads_use_service_key_and_do_not_need_admin_role():
    now = datetime.now(UTC).replace(microsecond=0)
    fake = FakeJava(role_key="PRODUCER")
    fake.seed(now - timedelta(hours=1), 300, 19.0, 70.0)
    app = _app(fake)
    start = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    end = now.isoformat().replace("+00:00", "Z")

    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        history = client.get("/api/v1/climate/history", params={"start": start, "end": end},
                             headers={"Authorization": "Bearer producer-jwt"})
        forbidden_sync = client.post("/api/v1/integrations/thingspeak/sync",
                                     headers={"Authorization": "Bearer producer-jwt"})

    assert history.status_code == 200, history.text
    assert history.json()["records"] == 1
    assert history.json()["metrics"]["temperature"]["current"] == 19.0
    assert forbidden_sync.status_code == 403


def test_legacy_rainfall_values_from_older_rows_are_not_exposed_to_the_frontend():
    now = datetime.now(UTC).replace(microsecond=0)
    fake = FakeJava(role_key="PRODUCER")
    fake.stored[(CHANNEL_ID, 400)] = {
        "id": "row-400", "channelId": CHANNEL_ID, "entryId": 400, "sensorCode": f"thingspeak-{CHANNEL_ID}",
        "capturedAt": (now - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "measurements": {"temperature": 22.0, "humidity": 65.0, "rainfall": 3.2}, "qualityStatus": "VALID",
    }
    app = _app(fake)

    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        response = client.get("/api/v1/climate/current", headers={"Authorization": "Bearer producer-jwt"})

    assert response.status_code == 200
    assert set(response.json()["metrics"]) == {"temperature", "humidity"}
    assert "rainfall" not in json.dumps(response.json())
