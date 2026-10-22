"""Etapa 3: indicadores de exportação, previsão e alertas."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.main import create_app
from app.services.alerts import (
    build_alerts,
    build_forecast,
    moving_average,
    vapor_pressure_deficit,
    zscores,
)
from app.services.daily import DayStats, build_days
from app.services.indicators import build_export_indicators, daily_gdd, project_harvest, risk_level

INTERNAL_KEY = "i" * 40
OFFSET = -3.0


def make_day(day: date, temperature: float, humidity: float, *, hours: int = 24, spread: float = 5.0) -> DayStats:
    stats = DayStats(day=day, hourly=[(temperature, humidity)] * hours)
    stats.temperature_min = temperature - spread
    stats.temperature_max = temperature + spread
    return stats


def make_days(start: date, temperatures: list[float], humidity: float = 60.0) -> list[DayStats]:
    return [make_day(start + timedelta(days=index), value, humidity) for index, value in enumerate(temperatures)]


def reading(stamp: datetime, temperature: float, humidity: float, quality: str = "VALID") -> dict[str, Any]:
    return {"capturedAt": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"), "qualityStatus": quality,
            "measurements": {"temperature": temperature, "humidity": humidity}}


# --- graus-dia ---------------------------------------------------------------------------------------------

def test_daily_gdd_uses_mean_of_min_and_max_above_base():
    assert daily_gdd(15, 31, base=10, upper=35) == 13.0


def test_daily_gdd_clamps_to_base_and_upper_limits():
    assert daily_gdd(5, 8, base=10, upper=35) == 0.0
    assert daily_gdd(20, 45, base=10, upper=35) == 17.5  # (20 + 35) / 2 - 10


def test_gdd_is_accumulated_only_over_complete_days():
    days = make_days(date(2026, 9, 1), [25, 25, 25])  # Tmin 20, Tmax 30 -> 15 GDD/dia
    days.append(make_day(date(2026, 9, 4), 25, 60, hours=6))  # incompleto
    result = build_export_indicators(days, rejected=0, base_temp=10, upper_temp=35, target_gdd=1000)
    assert result.gdd_accumulated == 45.0
    assert (result.complete_days, result.incomplete_days) == (3, 1)
    assert result.days[2].gdd_accumulated == 45.0
    assert result.days[3].gdd is None and result.days[3].downy_mildew_risk == "INSUFFICIENT_DATA"


# --- risco de doença ---------------------------------------------------------------------------------------

def test_risk_level_thresholds():
    assert [risk_level(hours) for hours in (0, 3, 4, 7, 8, 20)] == ["LOW", "LOW", "MODERATE", "MODERATE", "HIGH", "HIGH"]


def test_downy_mildew_risk_needs_warm_and_very_humid_hours():
    humid = DayStats(day=date(2026, 9, 1), hourly=[(24.0, 95.0)] * 10 + [(24.0, 50.0)] * 14)
    humid.temperature_min, humid.temperature_max = 22.0, 26.0
    dry = make_day(date(2026, 9, 2), 24.0, 50.0)
    result = build_export_indicators([humid, dry], rejected=0, base_temp=10, upper_temp=35, target_gdd=1000)
    assert result.days[0].downy_mildew_hours == 10 and result.days[0].downy_mildew_risk == "HIGH"
    assert result.days[1].downy_mildew_risk == "LOW"
    assert result.current_downy_mildew_risk == "LOW"


# --- janela de colheita ------------------------------------------------------------------------------------

def test_harvest_projection_orders_early_expected_late():
    projection = project_harvest(date(2026, 10, 1), 300.0, [10, 12, 14, 16, 18, 12, 14])
    assert projection.status == "PROJECTED"
    assert projection.earliest and projection.expected and projection.latest
    assert projection.earliest <= projection.expected <= projection.latest


def test_harvest_needs_recent_days_and_has_a_horizon():
    assert project_harvest(date(2026, 10, 1), 300.0, [10, 12]).status == "INSUFFICIENT_DATA"
    assert project_harvest(date(2026, 10, 1), 3000.0, [5, 5, 5]).status == "BEYOND_HORIZON"
    assert project_harvest(date(2026, 10, 1), 300.0, [0, 0, 0]).status == "BEYOND_HORIZON"


def test_harvest_window_reports_target_reached():
    days = make_days(date(2026, 9, 1), [25] * 10)  # 150 GDD
    result = build_export_indicators(days, rejected=0, base_temp=10, upper_temp=35, target_gdd=100)
    assert result.harvest_window is not None
    assert result.harvest_window.status == "REACHED"
    assert result.harvest_window.reached_on == date(2026, 9, 7)  # 15 GDD/dia -> passa de 100 no 7º dia


def test_harvest_window_projects_remaining_days():
    days = make_days(date(2026, 9, 1), [25] * 10)  # 150 GDD, 15/dia
    result = build_export_indicators(days, rejected=0, base_temp=10, upper_temp=35, target_gdd=300)
    window = result.harvest_window
    assert window is not None and window.status == "PROJECTED"
    assert window.remaining_gdd == 150.0
    assert window.expected == date(2026, 9, 10) + timedelta(days=10)


def test_export_indicators_without_readings_is_unavailable():
    result = build_export_indicators([], rejected=2, base_temp=10, upper_temp=35, target_gdd=1500)
    assert result.available is False and result.rejected_records == 2 and result.harvest_window is None


# --- médias móveis e anomalias -----------------------------------------------------------------------------

def test_moving_average_waits_for_a_full_window():
    assert moving_average([1, 2, 3, 4], 3) == [None, None, 2.0, 3.0]
    assert moving_average([1, None, 3, 4], 3) == [None, None, None, None]


def test_zscore_flags_value_far_from_recent_baseline():
    values: list[float | None] = [20.0, 20.5, 19.5, 20.2, 19.8, 20.1, 19.9, 20.0, 31.0]
    scores = zscores(values)
    assert scores[0] is None and scores[6] is None  # baseline < 7 dias
    last = scores[-1]
    assert last is not None and last > 3


def test_zscore_ignores_flat_series():
    assert zscores([20.0] * 10)[-1] is None


# --- geada e estresse hídrico ------------------------------------------------------------------------------

def test_vapor_pressure_deficit_matches_reference_values():
    assert vapor_pressure_deficit(25, 50) == pytest.approx(1.58, abs=0.02)
    assert vapor_pressure_deficit(35, 100) == 0


def test_frost_alert_levels():
    cold = make_day(date(2026, 7, 1), 5.0, 70.0, spread=3.0)  # Tmin 2 -> atenção
    freezing = make_day(date(2026, 7, 2), 3.0, 70.0, spread=4.0)  # Tmin -1 -> crítico
    mild = make_day(date(2026, 7, 3), 20.0, 70.0)
    summary = build_alerts([cold, freezing, mild], window_days=3, average_window=3, z_threshold=2.0,
                           first_day=date(2026, 7, 1))
    frost = {alert.date: alert.severity for alert in summary.alerts if alert.type == "FROST"}
    assert frost == {date(2026, 7, 1): "WARNING", date(2026, 7, 2): "CRITICAL"}


def test_water_stress_alert_from_high_vpd_hours():
    hot_dry = DayStats(day=date(2026, 10, 1), hourly=[(36.0, 25.0)] * 6 + [(25.0, 70.0)] * 18)  # DPV ~4,2 kPa
    hot_dry.temperature_min, hot_dry.temperature_max = 22.0, 37.0
    summary = build_alerts([hot_dry], window_days=1, average_window=3, z_threshold=2.0, first_day=date(2026, 10, 1))
    stress = [alert for alert in summary.alerts if alert.type == "WATER_STRESS"]
    assert len(stress) == 1 and stress[0].severity == "CRITICAL"


def test_anomaly_alert_only_inside_reported_window():
    temperatures = [22.0, 22.4, 21.8, 22.1, 21.9, 22.2, 22.0, 22.3, 29.0]
    days = make_days(date(2026, 9, 1), temperatures)
    summary = build_alerts(days, window_days=3, average_window=3, z_threshold=2.0, first_day=date(2026, 9, 7))
    anomalies = [alert for alert in summary.alerts if alert.type == "TEMPERATURE_ANOMALY"]
    assert [alert.date for alert in anomalies] == [date(2026, 9, 9)]
    assert [point.date for point in summary.series] == [date(2026, 9, 7), date(2026, 9, 8), date(2026, 9, 9)]


# --- previsão ----------------------------------------------------------------------------------------------

def test_forecast_extends_a_linear_trend():
    days = make_days(date(2026, 10, 1), [20.0 + index for index in range(10)])
    forecast = build_forecast(days)
    assert forecast.available is True
    assert [item["date"] for item in forecast.predictions] == ["2026-10-11", "2026-10-12", "2026-10-13"]
    assert forecast.predictions[0]["temperature"] == pytest.approx(30.0, abs=0.01)
    assert forecast.predictions[2]["temperature"] == pytest.approx(32.0, abs=0.01)


def test_forecast_clamps_humidity_and_requires_enough_days():
    days = [make_day(date(2026, 10, 1) + timedelta(days=i), 25.0, 95.0 + i * 2) for i in range(8)]
    for item in build_forecast(days).predictions:
        assert 0 <= float(item["humidityHigh"]) <= 100
    short = build_forecast(make_days(date(2026, 10, 1), [25.0] * 3))
    assert short.available is False and short.predictions == []


# --- agrupamento em dias locais ----------------------------------------------------------------------------

def test_build_days_uses_local_day_and_discards_rejected():
    # 02:00Z de 02/10 ainda é 23:00 de 01/10 em UTC-3.
    readings = [
        reading(datetime(2026, 10, 2, 2, 0, tzinfo=UTC), 20.0, 80.0),
        reading(datetime(2026, 10, 2, 12, 0, tzinfo=UTC), 30.0, 50.0),
        reading(datetime(2026, 10, 2, 13, 0, tzinfo=UTC), 99.0, 50.0, quality="REJECTED"),
        reading(datetime(2026, 10, 2, 14, 0, tzinfo=UTC), 999.0, 50.0),  # fora da faixa física
    ]
    days, rejected = build_days(readings, OFFSET)
    assert rejected == 1
    assert [day.day for day in days] == [date(2026, 10, 1), date(2026, 10, 2)]
    assert days[0].temperature_min == 20.0
    assert days[1].temperature_max == 30.0


# --- rotas -------------------------------------------------------------------------------------------------

class FakeJava:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    def seed_days(self, days: int, temperature: float = 25.0, humidity: float = 70.0) -> None:
        start = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) - timedelta(days=days)
        for hour in range(days * 24):
            stamp = start + timedelta(hours=hour)
            swing = 5.0 if 14 <= (stamp.hour - 3) % 24 <= 16 else (-5.0 if 4 <= (stamp.hour - 3) % 24 <= 6 else 0.0)
            self.rows.append(reading(stamp, temperature + swing, humidity))

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/auth/me":
            return httpx.Response(200, json={
                "id": "11111111-1111-4111-8111-111111111111", "name": "U", "email": "u@agro.test",
                "roleKey": "PRODUCER", "role": "PRODUCER", "status": "ACTIVE",
            })
        if request.url.path == "/api/internal/climate-readings":
            return httpx.Response(200, json=self.rows)
        return httpx.Response(404, json={"message": "not found"})


def _get(fake: FakeJava, path: str, **params: Any) -> httpx.Response:
    app = create_app(Settings(java_api_url="http://java.test", internal_api_key=INTERNAL_KEY))
    with TestClient(app) as client:
        app.state.http_client = httpx.AsyncClient(transport=httpx.MockTransport(fake))
        return client.get(path, params=params, headers={"Authorization": "Bearer jwt"})


def test_export_indicators_route_returns_gdd_and_harvest_window():
    fake = FakeJava()
    fake.seed_days(12)
    response = _get(fake, "/api/v1/indicators/export", target_gdd=300)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True and body["completeDays"] >= 10
    assert body["gddAccumulated"] > 0
    assert body["harvestWindow"]["status"] in {"PROJECTED", "REACHED"}
    assert "downyMildewRisk" in body["days"][0]
    assert body["assumptions"]


def test_export_indicators_validates_parameters():
    fake = FakeJava()
    assert _get(fake, "/api/v1/indicators/export", base_temp=30, upper_temp=25).status_code == 400
    assert _get(fake, "/api/v1/indicators/export", target_gdd=0).status_code == 400
    start = (datetime.now(UTC) - timedelta(days=5)).isoformat()
    assert _get(fake, "/api/v1/indicators/export", start=start).status_code == 400


def test_alerts_route_returns_series_with_moving_average():
    fake = FakeJava()
    fake.seed_days(25)
    response = _get(fake, "/api/v1/dashboard/alerts", days=7, window=3)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["available"] is True and body["movingAverageWindow"] == 3
    assert 1 <= len(body["series"]) <= 7
    assert body["series"][-1]["temperatureMovingAverage"] is not None
    assert _get(fake, "/api/v1/dashboard/alerts", days=0).status_code == 400


def test_forecast_route_is_available_with_enough_history_and_empty_without():
    fake = FakeJava()
    assert _get(fake, "/api/v1/dashboard/forecast").json()["available"] is False
    fake.seed_days(14)
    body = _get(fake, "/api/v1/dashboard/forecast").json()
    assert body["available"] is True and len(body["predictions"]) == 3


def test_new_routes_require_authentication():
    app = create_app(Settings(java_api_url="http://java.test", internal_api_key=INTERNAL_KEY))
    with TestClient(app) as client:
        for path in ("/api/v1/indicators/export", "/api/v1/dashboard/alerts", "/api/v1/dashboard/forecast"):
            assert client.get(path).status_code == 401


def test_readings_are_not_truncated_by_the_java_5000_limit():
    from app.services.dashboard import DashboardService

    end = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
    first = end - timedelta(hours=120)
    rows = [{**reading(first + timedelta(minutes=minute), 25.0, 60.0), "channelId": "1", "entryId": minute}
            for minute in range(120 * 60 + 1)]  # 7201 leituras, uma por minuto

    async def handler(request: httpx.Request) -> httpx.Response:
        start = datetime.fromisoformat(request.url.params["from"].replace("Z", "+00:00"))
        stop = datetime.fromisoformat(request.url.params["to"].replace("Z", "+00:00"))
        inside = [row for row in rows if start <= datetime.fromisoformat(row["capturedAt"].replace("Z", "+00:00")) <= stop]
        return httpx.Response(200, json=inside[:5000])  # o Java devolve as 5000 mais antigas

    import asyncio

    async def run() -> list[dict[str, Any]]:
        settings = Settings(java_api_url="http://java.test", internal_api_key=INTERNAL_KEY)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await DashboardService(settings, client)._readings_without_truncation(first, end)

    result = asyncio.run(run())
    assert len(result) == len(rows)
    assert max(item["capturedAt"] for item in result) == rows[-1]["capturedAt"]
