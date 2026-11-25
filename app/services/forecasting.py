"""Modelos estatísticos de projeção climática com backtesting honesto.

Mantém o ``build_forecast`` linear original (em ``alerts.py``) intacto por compatibilidade.
Aqui ficam o upgrade estatístico (Holt-Winters com tendência amortecida e sazonalidade semanal
quando há histórico) e as métricas de validação walk-forward (MAE / RMSE / R²).

Nada aqui é previsão meteorológica: é a projeção do histórico da própria estação para os
próximos dias, com faixa de confiança derivada dos resíduos do backtest. Sem dados suficientes
o modelo se declara indisponível — nenhum resultado artificial é gerado.
"""

from __future__ import annotations

from datetime import timedelta
import logging
import math
import warnings

import numpy as np
from statsmodels.tsa.holtwinters import ExponentialSmoothing

from app.models import ForecastMetrics, ForecastSummary
from app.services.daily import DayStats

logger = logging.getLogger("agroclima.forecasting")

STAT_MIN_DAYS = 7
STAT_SEASONAL_PERIOD = 7
# Só liga sazonalidade semanal com 3 ciclos completos (21 dias); abaixo disso, tendência amortecida.
STAT_SEASONAL_MIN_DAYS = 21
STAT_MAX_LOOKBACK_DAYS = 120
STAT_BACKTEST_ORIGINS = 6
CONFIDENCE_Z = 1.96  # ~95%
CONFIDENCE_LEVEL = 0.95

STAT_DISCLAIMER = (
    "Projeção estatística do histórico da estação (Holt-Winters), não é previsão meteorológica. "
    "A faixa Low/High é um intervalo de ~95% derivado dos resíduos do backtest walk-forward."
)


def regression_metrics(actual: list[float], predicted: list[float]) -> ForecastMetrics:
    """MAE, RMSE e R² sobre pares (real, previsto) finitos. R² segue a convenção do scikit-learn."""
    pairs = [(float(a), float(p)) for a, p in zip(actual, predicted, strict=False)
             if math.isfinite(float(a)) and math.isfinite(float(p))]
    if not pairs:
        return ForecastMetrics(samples=0)
    a = np.array([item[0] for item in pairs], dtype=float)
    p = np.array([item[1] for item in pairs], dtype=float)
    error = a - p
    mae = float(np.mean(np.abs(error)))
    rmse = float(np.sqrt(np.mean(error ** 2)))
    ss_res = float(np.sum(error ** 2))
    ss_tot = float(np.sum((a - float(np.mean(a))) ** 2))
    if ss_tot > 1e-12:
        r2 = 1.0 - ss_res / ss_tot
    else:
        r2 = 1.0 if ss_res <= 1e-12 else 0.0
    return ForecastMetrics(mae=round(mae, 3), rmse=round(rmse, 3), r2=round(r2, 4), samples=len(pairs))


def _fit_predict(values: list[float], horizon: int, seasonal_periods: int | None) -> tuple[np.ndarray, np.ndarray, str]:
    """Ajusta Holt-Winters e projeta `horizon` passos. Cai para regressão linear se o ajuste falhar.

    Devolve (previsões, valores ajustados in-sample, rótulo do algoritmo).
    """
    arr = np.asarray(values, dtype=float)
    use_seasonal = seasonal_periods if (seasonal_periods and len(arr) >= 2 * seasonal_periods) else None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = ExponentialSmoothing(
                arr, trend="add", damped_trend=True,
                seasonal="add" if use_seasonal else None, seasonal_periods=use_seasonal,
                initialization_method="estimated",
            )
            fit = model.fit(optimized=True)
            pred = np.asarray(fit.forecast(horizon), dtype=float)
            fitted = np.asarray(fit.fittedvalues, dtype=float)
        if np.all(np.isfinite(pred)) and np.all(np.isfinite(fitted)):
            label = "holt-winters-sazonal" if use_seasonal else "holt-amortecido"
            return pred, fitted, label
    except (ValueError, RuntimeError, np.linalg.LinAlgError) as exception:
        logger.debug("Holt-Winters falhou (%s); usando regressão linear.", exception)
    x = np.arange(len(arr), dtype=float)
    slope, intercept = np.polyfit(x, arr, 1)
    future = np.arange(len(arr), len(arr) + horizon, dtype=float)
    return slope * future + intercept, slope * x + intercept, "regressao-linear"


def _step_std(residuals: list[float], fallback: list[float]) -> float:
    pool = residuals if len(residuals) >= 2 else fallback
    if len(pool) < 2:
        return 0.0
    return float(np.std(np.asarray(pool, dtype=float), ddof=1))


def build_statistical_forecast(days: list[DayStats], horizon_days: int = 3) -> ForecastSummary:
    """Projeção estatística com sazonalidade quando há histórico e métricas de backtest walk-forward."""
    horizon_days = max(1, min(int(horizon_days), 7))
    recent = [day for day in days if day.complete and day.temperature_mean is not None
              and day.humidity_mean is not None][-STAT_MAX_LOOKBACK_DAYS:]
    if len(recent) < STAT_MIN_DAYS:
        return ForecastSummary(
            available=False, model_kind="statistical",
            message=f"São necessários ao menos {STAT_MIN_DAYS} dias completos de leitura para projetar.",
        )

    n = len(recent)
    series = {
        "temperature": [float(day.temperature_mean) for day in recent],  # type: ignore[arg-type]
        "humidity": [float(day.humidity_mean) for day in recent],  # type: ignore[arg-type]
    }
    seasonal = STAT_SEASONAL_PERIOD if n >= STAT_SEASONAL_MIN_DAYS else None

    # Backtest walk-forward: origens recentes, cada uma projeta `horizon` passos à frente.
    origins = list(range(max(STAT_MIN_DAYS, n - STAT_BACKTEST_ORIGINS - horizon_days + 1), n - horizon_days + 1))
    residuals: dict[str, dict[int, list[float]]] = {name: {step: [] for step in range(horizon_days)} for name in series}
    pooled_actual: dict[str, list[float]] = {name: [] for name in series}
    pooled_pred: dict[str, list[float]] = {name: [] for name in series}
    for origin in origins:
        for name, values in series.items():
            pred, _, _ = _fit_predict(values[:origin], horizon_days, seasonal)
            for step in range(horizon_days):
                index = origin + step
                if index < n and math.isfinite(float(pred[step])):
                    residuals[name][step].append(values[index] - float(pred[step]))
                    pooled_actual[name].append(values[index])
                    pooled_pred[name].append(float(pred[step]))

    last = recent[-1].day
    predictions: list[dict[str, float | str]] = [{"date": (last + timedelta(days=step + 1)).isoformat()}
                                                 for step in range(horizon_days)]
    metrics: dict[str, ForecastMetrics] = {}
    labels: list[str] = []
    for name, values in series.items():
        pred, fitted, label = _fit_predict(values, horizon_days, seasonal)
        labels.append(label)
        metrics[name] = regression_metrics(pooled_actual[name], pooled_pred[name])
        in_sample = (values - fitted).tolist()
        for step in range(horizon_days):
            value = float(pred[step])
            std = _step_std(residuals[name][step], in_sample)
            low, high = value - CONFIDENCE_Z * std, value + CONFIDENCE_Z * std
            if name == "humidity":
                value, low, high = (min(max(item, 0.0), 100.0) for item in (value, low, high))
            predictions[step][name] = round(value, 2)
            predictions[step][f"{name}Low"] = round(low, 2)
            predictions[step][f"{name}High"] = round(high, 2)

    unique_labels = set(labels)
    algorithm = next(iter(unique_labels)) if len(unique_labels) == 1 else "misto"
    return ForecastSummary(
        available=True, model=f"{algorithm}-{n}d", model_kind="statistical",
        horizon_days=horizon_days, predictions=predictions, metrics=metrics,
        backtest_days=len(origins), confidence_level=CONFIDENCE_LEVEL, message=STAT_DISCLAIMER,
    )
