"""Pipeline de machine learning: features, treino, backtesting, persistência e projeção recursiva."""

from __future__ import annotations

from datetime import date, timedelta
import math

import pytest

from app.errors import ServiceError
from app.services.daily import DayStats
from app.services.ml import (
    MIN_FEATURE_DAYS,
    ModelRegistry,
    _build_dataset,
    _complete_series,
    _feature_names,
    predict_with_model,
    train_models,
)


def make_days(n: int, start: date = date(2026, 1, 1)) -> list[DayStats]:
    days = []
    for i in range(n):
        temperature = 25 + 3 * math.sin(2 * math.pi * i / 7) + 0.03 * i
        humidity = 65 + 8 * math.cos(2 * math.pi * i / 7)
        stats = DayStats(day=start + timedelta(days=i), hourly=[(temperature, humidity)] * 24)
        stats.temperature_min = temperature - 5
        stats.temperature_max = temperature + 5
        days.append(stats)
    return days


def test_feature_names_cover_lags_rolling_and_seasonality():
    names = _feature_names()
    assert "temperature_lag1" in names and "temperature_lag7" in names
    assert "humidity_roll3" in names and "humidity_roll7" in names
    assert "doy_sin" in names and "doy_cos" in names
    assert len(names) == 20


def test_dataset_has_no_target_leakage_and_needs_warmup():
    series = make_days(40)
    x, y, dates = _build_dataset(series)
    # A primeira linha-alvo só existe após o aquecimento dos lags.
    assert len(x) == len(series) - MIN_FEATURE_DAYS
    assert dates[0] == series[MIN_FEATURE_DAYS].day
    assert x.shape[1] == len(_feature_names())
    assert len(y["temperature"]) == len(x) and len(y["humidity"]) == len(x)


def test_complete_series_filters_incomplete_and_missing():
    series = make_days(5)
    series[2].hourly = series[2].hourly[:6]  # dia incompleto
    assert len(_complete_series(series)) == 4


def test_train_requires_minimum_history():
    with pytest.raises(ServiceError) as excinfo:
        train_models(make_days(20), algorithm="auto", horizon_days=3, min_train_days=60)
    assert excinfo.value.status_code == 409
    assert "insuficiente" in excinfo.value.message.lower()


def test_train_rejects_unknown_algorithm():
    with pytest.raises(ServiceError) as excinfo:
        train_models(make_days(70), algorithm="neural_net", horizon_days=3, min_train_days=60)
    assert excinfo.value.status_code == 400


def test_train_auto_selects_best_and_reports_metrics():
    outcome = train_models(make_days(80), algorithm="auto", horizon_days=3, min_train_days=60)
    info = outcome.info
    assert info.algorithm in ("random_forest", "gradient_boosting")
    assert info.training_days == 80
    assert info.horizon_days == 3
    assert info.feature_count == 20
    assert len(info.candidates) == 2
    selected = [c for c in info.candidates if c.selected]
    assert len(selected) == 1 and selected[0].algorithm == info.algorithm
    for target in ("temperature", "humidity"):
        assert info.metrics[target].samples > 0
        assert info.metrics[target].rmse is not None


def test_train_specific_algorithm_skips_comparison():
    outcome = train_models(make_days(70), algorithm="random_forest", horizon_days=2, min_train_days=60)
    assert outcome.info.algorithm == "random_forest"
    assert len(outcome.info.candidates) == 1
    assert outcome.info.horizon_days == 2


def test_registry_persistence_roundtrip_and_delete(tmp_path):
    outcome = train_models(make_days(70), algorithm="gradient_boosting", horizon_days=3, min_train_days=60)
    registry = ModelRegistry(tmp_path / "models")
    assert registry.load_info() is None
    registry.save(outcome)
    info = registry.load_info()
    payload = registry.load_payload()
    assert info is not None and info.model_id == outcome.info.model_id
    assert payload is not None and "models" in payload
    assert registry.delete() is True
    assert registry.load_info() is None and registry.load_payload() is None
    assert registry.delete() is False


def test_registry_load_survives_corrupt_metadata(tmp_path):
    registry = ModelRegistry(tmp_path)
    registry.dir.mkdir(parents=True, exist_ok=True)
    registry.meta_path.write_text("{ not json", encoding="utf-8")
    assert registry.load_info() is None


def test_predict_with_model_projects_horizon_recursively():
    series = make_days(80)
    outcome = train_models(series, algorithm="gradient_boosting", horizon_days=3, min_train_days=60)
    forecast = predict_with_model(outcome.info, outcome.payload, series, horizon_days=3)
    assert forecast.available is True
    assert forecast.model_kind == "ml"
    assert forecast.model_id == outcome.info.model_id
    assert len(forecast.predictions) == 3
    last = series[-1].day
    assert forecast.predictions[0]["date"] == (last + timedelta(days=1)).isoformat()
    for item in forecast.predictions:
        assert item["temperatureLow"] <= item["temperature"] <= item["temperatureHigh"]
        assert 0 <= item["humidityLow"] and item["humidityHigh"] <= 100


def test_predict_with_model_needs_recent_lags():
    series = make_days(80)
    outcome = train_models(series, algorithm="random_forest", horizon_days=3, min_train_days=60)
    forecast = predict_with_model(outcome.info, outcome.payload, make_days(3))
    assert forecast.available is False
    assert forecast.model_kind == "ml"


@pytest.mark.anyio
async def test_registry_train_and_save_is_locked_and_persists(tmp_path):
    import asyncio

    registry = ModelRegistry(tmp_path)
    outcome = await registry.train_and_save(make_days(70), algorithm="random_forest",
                                            horizon_days=3, min_train_days=60)
    assert registry.load_info() is not None
    assert registry.load_info().model_id == outcome.info.model_id
    # Duas chamadas concorrentes são serializadas pelo lock (sem corrupção).
    await asyncio.gather(
        registry.train_and_save(make_days(70), algorithm="random_forest", horizon_days=3, min_train_days=60),
        registry.train_and_save(make_days(70), algorithm="gradient_boosting", horizon_days=3, min_train_days=60),
    )
    assert registry.load_info() is not None
