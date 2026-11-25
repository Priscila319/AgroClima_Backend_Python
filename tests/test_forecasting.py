"""Modelos estatísticos: Holt-Winters, backtesting walk-forward e métricas (MAE/RMSE/R²)."""

from __future__ import annotations

from datetime import date, timedelta
import math

import pytest

from app.services.daily import DayStats
from app.services.forecasting import (
    STAT_MIN_DAYS,
    build_statistical_forecast,
    regression_metrics,
)


def make_days(n: int, start: date = date(2026, 1, 1), *, seasonal: bool = True) -> list[DayStats]:
    days = []
    for i in range(n):
        trend = 25 + 0.04 * i
        wave = 3 * math.sin(2 * math.pi * i / 7) if seasonal else 0.0
        temperature = trend + wave
        humidity = 65 + (8 * math.cos(2 * math.pi * i / 7) if seasonal else 0.0)
        stats = DayStats(day=start + timedelta(days=i), hourly=[(temperature, humidity)] * 24)
        stats.temperature_min = temperature - 5
        stats.temperature_max = temperature + 5
        days.append(stats)
    return days


def test_regression_metrics_computes_mae_rmse_r2():
    metrics = regression_metrics([10.0, 12.0, 14.0, 16.0], [11.0, 11.0, 15.0, 15.0])
    assert metrics.samples == 4
    assert metrics.mae == pytest.approx(1.0, abs=0.001)
    assert metrics.rmse == pytest.approx(1.0, abs=0.001)
    assert metrics.r2 is not None and metrics.r2 < 1.0


def test_regression_metrics_perfect_and_empty():
    perfect = regression_metrics([1.0, 2.0, 3.0], [1.0, 2.0, 3.0])
    assert perfect.mae == 0.0 and perfect.rmse == 0.0 and perfect.r2 == 1.0
    empty = regression_metrics([], [])
    assert empty.samples == 0 and empty.mae is None and empty.r2 is None


def test_statistical_forecast_requires_minimum_days():
    short = build_statistical_forecast(make_days(STAT_MIN_DAYS - 1))
    assert short.available is False
    assert short.model_kind == "statistical"
    assert str(STAT_MIN_DAYS) in short.message


def test_statistical_forecast_projects_horizon_with_intervals_and_metrics():
    forecast = build_statistical_forecast(make_days(60), horizon_days=3)
    assert forecast.available is True
    assert forecast.model_kind == "statistical"
    assert forecast.horizon_days == 3
    assert len(forecast.predictions) == 3
    assert forecast.confidence_level == 0.95
    assert forecast.backtest_days and forecast.backtest_days > 0
    first = forecast.predictions[0]
    assert first["temperatureLow"] <= first["temperature"] <= first["temperatureHigh"]
    assert first["humidityLow"] <= first["humidity"] <= first["humidityHigh"]
    assert 0 <= first["humidityLow"] and first["humidityHigh"] <= 100
    for target in ("temperature", "humidity"):
        assert target in forecast.metrics
        assert forecast.metrics[target].samples > 0
        assert forecast.metrics[target].rmse is not None


def test_statistical_forecast_uses_seasonality_with_enough_history():
    seasonal = build_statistical_forecast(make_days(60, seasonal=True))
    assert "sazonal" in (seasonal.model or "")
    # Com sazonalidade semanal real, o erro de backtest deve ser baixo.
    assert seasonal.metrics["temperature"].rmse is not None
    assert seasonal.metrics["temperature"].rmse < 1.5


def test_statistical_forecast_without_seasonality_uses_damped_trend():
    forecast = build_statistical_forecast(make_days(10, seasonal=False))
    assert forecast.available is True
    assert "sazonal" not in (forecast.model or "")


def test_statistical_forecast_clamps_humidity_to_valid_range():
    days = make_days(30)
    for day in days:
        day.hourly = [(day.hourly[0][0], 99.5)] * 24
    forecast = build_statistical_forecast(days)
    for item in forecast.predictions:
        assert 0 <= item["humidity"] <= 100
        assert 0 <= item["humidityLow"] and item["humidityHigh"] <= 100


def test_statistical_forecast_horizon_is_bounded():
    assert len(build_statistical_forecast(make_days(40), horizon_days=99).predictions) == 7
    assert len(build_statistical_forecast(make_days(40), horizon_days=1).predictions) == 1
