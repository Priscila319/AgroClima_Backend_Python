"""Etapa 1: chamadas duplicadas ao Java, cache de perfil, histórico agregado, CORS e config."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
import httpx
from pydantic import ValidationError
import pytest

from app.cache import TTLCache, token_key
from app.config import Settings
from app.main import create_app

KEY = "k" * 40


class CountingJava:
    """Java falso que conta as chamadas por rota."""

    def __init__(self, role_key: str = "ANALYST", status: str = "ACTIVE"):
        self.role_key = role_key
        self.status = status
        self.counts: dict[str, int] = {}
        self.readings: list[dict] = []

    def add(self, captured_at: datetime, temperature: float, humidity: float, quality: str = "VALID") -> None:
        self.readings.append({
            "channelId": "1", "entryId": len(self.readings) + 1,
            "capturedAt": captured_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "measurements": {"temperature": temperature, "humidity": humidity}, "qualityStatus": quality,
        })

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.counts[path] = self.counts.get(path, 0) + 1
        if path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@a.test",
                "roleKey": self.role_key, "role": self.role_key, "status": self.status})
        if path == "/api/plantations":
            return httpx.Response(200, json=[{"variety": "Syrah", "quantity": 10, "status": "Ativa"}])
        if path == "/api/internal/climate-readings":
            return httpx.Response(200, json=self.readings)
        return httpx.Response(404, json={"message": "?"})


def _client(fake: CountingJava, **settings):
    app = create_app(Settings(java_api_url="http://java.test", internal_api_key=KEY, **settings))
    return app, TestClient(app)


def _attach(app, fake):
    app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))


AUTH = {"Authorization": "Bearer analyst-jwt"}


# ---------- Melhorias #1: chamadas repetidas ao Java ----------

def test_analyst_dashboard_default_window_fetches_readings_only_once():
    fake = CountingJava()
    fake.add(datetime.now(UTC) - timedelta(hours=1), 25.0, 60.0)
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        response = client.get("/api/v1/dashboard/analyst", headers=AUTH)

    assert response.status_code == 200, response.text
    assert fake.counts["/api/internal/climate-readings"] == 1
    assert fake.counts["/api/plantations"] == 1
    assert fake.counts["/api/auth/me"] == 1
    body = response.json()
    assert body["quality"]["totalRecords"] == 1
    assert body["summary"]["climate"]["records"] == 1
    assert body["comparison"]["available"] is True


def test_analyst_dashboard_custom_window_uses_separate_windows_once_each():
    fake = CountingJava()
    now = datetime.now(UTC)
    fake.add(now - timedelta(hours=1), 25.0, 60.0)
    app, client = _client(fake)
    start = (now - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = (now - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with client:
        _attach(app, fake)
        response = client.get("/api/v1/dashboard/analyst", params={"start": start, "end": end}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert fake.counts["/api/internal/climate-readings"] == 2  # 24 h do resumo + janela pedida


def test_profile_is_cached_between_requests_with_same_token():
    fake = CountingJava()
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        for _ in range(3):
            assert client.get("/api/v1/climate/current", headers=AUTH).status_code == 200
        other = client.get("/api/v1/climate/current", headers={"Authorization": "Bearer other-jwt"})

    assert other.status_code == 200
    assert fake.counts["/api/auth/me"] == 2  # 1 por token, não 1 por request


def test_profile_cache_can_be_disabled():
    fake = CountingJava()
    app, client = _client(fake, profile_cache_ttl_seconds=0)
    with client:
        _attach(app, fake)
        for _ in range(3):
            client.get("/api/v1/climate/current", headers=AUTH)

    assert fake.counts["/api/auth/me"] == 3


def test_inactive_profile_is_rejected_and_not_cached():
    fake = CountingJava(status="INACTIVE")
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        first = client.get("/api/v1/climate/current", headers=AUTH)
        second = client.get("/api/v1/climate/current", headers=AUTH)

    assert first.status_code == second.status_code == 401
    assert fake.counts["/api/auth/me"] == 2


def test_ttl_cache_expires_evicts_and_never_stores_raw_token():
    now = [0.0]
    cache: TTLCache[str] = TTLCache(ttl_seconds=10, max_entries=2, clock=lambda: now[0])
    cache.set("a", "A")
    assert cache.get("a") == "A"
    now[0] = 10.0
    assert cache.get("a") is None  # expirou

    cache.set("a", "A"); cache.set("b", "B"); cache.set("c", "C")
    assert len(cache) == 2 and cache.get("a") is None  # a mais antiga saiu

    assert "secret-token" not in token_key("Bearer secret-token")
    assert TTLCache(ttl_seconds=0).enabled is False


# ---------- Melhorias #2: /climate/history ----------

def _seed_two_days(fake: CountingJava, step_minutes: int = 10) -> tuple[datetime, datetime]:
    end = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    start = end - timedelta(days=3)
    moment = start
    while moment < end:
        fake.add(moment, 20.0 + (moment.hour % 5), 60.0)
        moment += timedelta(minutes=step_minutes)
    return start, end


def _history(client, start, end, **params):
    return client.get("/api/v1/climate/history", headers=AUTH, params={
        "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), **params})


def test_history_auto_aggregates_long_periods_by_hour_keeping_raw_metrics():
    fake = CountingJava()
    start, end = _seed_two_days(fake)  # 3 dias, 432 leituras
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        response = _history(client, start, end)

    body = response.json()
    assert response.status_code == 200, response.text
    assert body["resolution"] == "hour"
    assert body["records"] == 432  # métricas e contagem continuam sobre as leituras brutas
    assert len(body["series"]) == 72  # 3 dias x 24 h
    assert body["metrics"]["temperature"]["samples"] == 432


def test_history_day_resolution_and_short_period_stays_raw():
    fake = CountingJava()
    start, end = _seed_two_days(fake)
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        daily = _history(client, start, end, resolution="day").json()
        short = _history(client, end - timedelta(hours=2), end).json()

    assert daily["resolution"] == "day" and 3 <= len(daily["series"]) <= 4  # dias de calendário UTC tocados
    assert short["resolution"] == "raw"


def test_history_raw_over_limit_is_refused_with_hint():
    fake = CountingJava()
    start, end = _seed_two_days(fake)
    app, client = _client(fake, history_max_raw_points=100)
    with client:
        _attach(app, fake)
        response = _history(client, start, end, resolution="raw")

    assert response.status_code == 400
    assert "resolution=hour" in response.json()["message"]


def test_history_rejects_invalid_resolution():
    fake = CountingJava()
    app, client = _client(fake)
    with client:
        _attach(app, fake)
        response = client.get("/api/v1/climate/history", headers=AUTH, params={"resolution": "minute"})
    assert response.status_code == 400


# ---------- Melhorias #3: segurança e config ----------

def test_dev_defaults_allow_local_origins_without_credentials():
    settings = Settings(_env_file=None)
    assert settings.app_env == "development"
    assert "http://localhost:5173" in settings.configured_origins
    assert settings.cors_allow_credentials is False


def test_cors_response_does_not_allow_credentials_by_default():
    with TestClient(create_app(Settings(_env_file=None))) as client:
        response = client.options("/api/v1/dashboard/summary", headers={
            "Origin": "http://localhost:5173", "Access-Control-Request-Method": "GET"})
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
    assert "access-control-allow-credentials" not in response.headers


def test_production_requires_explicit_non_local_origins():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production")
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_env="production", cors_allowed_origins="http://localhost:3000")
    prod = Settings(_env_file=None, app_env="production", cors_allowed_origins="https://app.exemplo.com.br")
    assert prod.configured_origins == ["https://app.exemplo.com.br"]


def test_wildcard_origin_is_never_accepted():
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_allowed_origins="*")


def test_production_requirements_do_not_ship_pytest():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert "pytest" not in (root / "requirements.txt").read_text()
    dev = (root / "requirements-dev.txt").read_text()
    assert "-r requirements.txt" in dev and "pytest" in dev


def test_env_example_has_no_space_after_equals():
    from pathlib import Path
    for line in (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#") and "=" in line:
            assert not line.split("=", 1)[1].startswith(" "), line
