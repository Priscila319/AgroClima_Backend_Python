"""Pipeline de machine learning (scikit-learn) para projeção climática da estação.

Treina um modelo por alvo (temperatura média e umidade média diárias) a partir de features
de defasagem (lags 1–7), médias móveis (3/7) e sazonalidade anual (seno/cosseno do dia do ano).
A validação é walk-forward: treina no passado, prevê o futuro retido e mede MAE/RMSE/R².
A projeção de vários dias é recursiva (a previsão de um dia realimenta o próximo).

O modelo só é habilitado com histórico mínimo (``model_min_train_days``). Abaixo disso o
treinamento é recusado — nenhum modelo artificial é criado. Persistência em joblib + JSON,
com gravação atômica para nunca deixar um modelo pela metade no disco.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
import logging
import math
import os
from pathlib import Path
import tempfile
from typing import Any
from uuid import uuid4

import joblib
import numpy as np
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor

from app.errors import ServiceError
from app.models import ForecastMetrics, ForecastSummary, ModelAlgorithmMetric, TrainedModelInfo
from app.services.daily import DayStats
from app.services.forecasting import CONFIDENCE_LEVEL, CONFIDENCE_Z, regression_metrics

logger = logging.getLogger("agroclima.ml")

TARGETS = ("temperature", "humidity")
LAGS = (1, 2, 3, 4, 5, 6, 7)
ROLLING = (3, 7)
MIN_FEATURE_DAYS = max(LAGS) + 1  # precisa de lags suficientes antes do primeiro alvo
ML_BACKTEST_DAYS = 14
ML_DISCLAIMER = (
    "Projeção por machine learning (scikit-learn) treinada no histórico da estação, não é previsão "
    "meteorológica. A faixa Low/High é um intervalo de ~95% dos resíduos do backtest walk-forward."
)
ALGORITHMS = ("random_forest", "gradient_boosting")


def _complete_series(days: list[DayStats]) -> list[DayStats]:
    return [day for day in days if day.complete and day.temperature_mean is not None and day.humidity_mean is not None]


def _feature_names() -> list[str]:
    names: list[str] = []
    for target in TARGETS:
        names.extend(f"{target}_lag{lag}" for lag in LAGS)
        names.extend(f"{target}_roll{window}" for window in ROLLING)
    names.extend(["doy_sin", "doy_cos"])
    return names


def _feature_row(history: list[tuple[float, float]], day: date) -> list[float]:
    """history: lista (temperatura, umidade) dos dias anteriores, mais recente por último."""
    row: list[float] = []
    for index in range(len(TARGETS)):
        values = [item[index] for item in history]
        for lag in LAGS:
            row.append(values[-lag])
        for window in ROLLING:
            row.append(float(np.mean(values[-window:])))
    angle = 2 * math.pi * (day.timetuple().tm_yday - 1) / 365.25
    row.extend([math.sin(angle), math.cos(angle)])
    return row


def _build_dataset(series: list[DayStats]) -> tuple[np.ndarray, dict[str, np.ndarray], list[date]]:
    """Monta X, y por alvo e as datas-alvo. Cada linha usa só os dias anteriores (sem vazamento)."""
    pairs = [(float(day.temperature_mean), float(day.humidity_mean)) for day in series]  # type: ignore[arg-type]
    dates = [day.day for day in series]
    rows: list[list[float]] = []
    targets: dict[str, list[float]] = {name: [] for name in TARGETS}
    target_dates: list[date] = []
    for index in range(MIN_FEATURE_DAYS, len(series)):
        history = pairs[:index]
        if len(history) < max(LAGS):
            continue
        rows.append(_feature_row(history, dates[index]))
        for position, name in enumerate(TARGETS):
            targets[name].append(pairs[index][position])
        target_dates.append(dates[index])
    x = np.asarray(rows, dtype=float)
    y = {name: np.asarray(values, dtype=float) for name, values in targets.items()}
    return x, y, target_dates


def _make_estimator(algorithm: str) -> Any:
    if algorithm == "random_forest":
        return RandomForestRegressor(n_estimators=200, max_depth=8, min_samples_leaf=2, random_state=42, n_jobs=1)
    if algorithm == "gradient_boosting":
        return GradientBoostingRegressor(n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42)
    raise ServiceError(400, f"Algoritmo desconhecido: {algorithm}.")


def _recursive_predict(models: dict[str, Any], history: list[tuple[float, float]], start: date,
                       horizon: int) -> list[dict[str, float]]:
    """Projeta `horizon` dias realimentando as próprias previsões como defasagens."""
    working = list(history)
    rows: list[dict[str, float]] = []
    for step in range(1, horizon + 1):
        day = start + timedelta(days=step)
        row = _feature_row(working, day)
        point: dict[str, float] = {}
        for name in TARGETS:
            value = float(models[name].predict([row])[0])
            if name == "humidity":
                value = min(max(value, 0.0), 100.0)
            point[name] = value
        rows.append(point)
        working.append((point["temperature"], point["humidity"]))
    return rows


def _walk_forward_backtest(series: list[DayStats], algorithm: str, horizon: int,
                           backtest_days: int) -> tuple[dict[str, ForecastMetrics], dict[str, list[float]]]:
    """Para cada origem recente, treina no passado e projeta `horizon` passos recursivamente.

    Devolve as métricas por alvo e os resíduos por passo (para o intervalo de confiança).
    """
    residuals: dict[str, dict[int, list[float]]] = {name: {step: [] for step in range(horizon)} for name in TARGETS}
    pooled_actual: dict[str, list[float]] = {name: [] for name in TARGETS}
    pooled_pred: dict[str, list[float]] = {name: [] for name in TARGETS}
    pairs = [(float(day.temperature_mean), float(day.humidity_mean)) for day in series]  # type: ignore[arg-type]
    dates = [day.day for day in series]
    n = len(series)
    first_origin = max(MIN_FEATURE_DAYS + 5, n - backtest_days - horizon + 1)
    for origin in range(first_origin, n - horizon + 1):
        train = series[:origin]
        x, y, _ = _build_dataset(train)
        if len(x) < 10:
            continue
        models = {name: _make_estimator(algorithm).fit(x, y[name]) for name in TARGETS}
        history = pairs[:origin]
        points = _recursive_predict(models, history, dates[origin - 1], horizon)
        for step, point in enumerate(points):
            index = origin + step
            if index >= n:
                continue
            for position, name in enumerate(TARGETS):
                actual = pairs[index][position]
                predicted = point[name]
                residuals[name][step].append(actual - predicted)
                pooled_actual[name].append(actual)
                pooled_pred[name].append(predicted)
    metrics = {name: regression_metrics(pooled_actual[name], pooled_pred[name]) for name in TARGETS}
    flat = {name: [value for step in residuals[name].values() for value in step] for name in TARGETS}
    return metrics, flat


def _residual_std(residuals: dict[str, list[float]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for name in TARGETS:
        values = residuals.get(name, [])
        result[name] = float(np.std(np.asarray(values, dtype=float), ddof=1)) if len(values) >= 2 else 0.0
    return result


@dataclass
class TrainOutcome:
    info: TrainedModelInfo
    payload: dict[str, Any]


def train_models(series: list[DayStats], *, algorithm: str, horizon_days: int, min_train_days: int,
                 backtest_days: int = ML_BACKTEST_DAYS) -> TrainOutcome:
    """Treina, valida por walk-forward e escolhe o algoritmo. Síncrono (rodar em thread)."""
    complete = _complete_series(series)
    if len(complete) < min_train_days:
        raise ServiceError(
            409,
            f"Histórico insuficiente para treinar: {len(complete)} dias completos disponíveis, "
            f"mínimo de {min_train_days}. Acumule mais leituras antes de habilitar o machine learning.",
        )
    horizon_days = max(1, min(int(horizon_days), 7))
    candidates = list(ALGORITHMS) if algorithm == "auto" else [algorithm]
    if algorithm not in ("auto", *ALGORITHMS):
        raise ServiceError(400, f"Algoritmo desconhecido: {algorithm}.")

    evaluated: list[ModelAlgorithmMetric] = []
    best_algorithm = candidates[0]
    best_score = math.inf
    best_residuals: dict[str, list[float]] = {}
    for candidate in candidates:
        metrics, residuals = _walk_forward_backtest(complete, candidate, horizon_days, backtest_days)
        scores: list[float] = []
        for name in TARGETS:
            rmse = metrics[name].rmse
            if rmse is not None:
                scores.append(rmse)
        score = float(np.mean(scores)) if scores else math.inf
        evaluated.append(ModelAlgorithmMetric(algorithm=candidate, metrics=metrics, selected=False))
        if score < best_score:
            best_score, best_algorithm, best_residuals = score, candidate, residuals

    for item in evaluated:
        item.selected = item.algorithm == best_algorithm

    x, y, _ = _build_dataset(complete)
    models = {name: _make_estimator(best_algorithm).fit(x, y[name]) for name in TARGETS}
    std = _residual_std(best_residuals)
    selected_metrics = next(item.metrics for item in evaluated if item.selected)
    model_id = uuid4().hex
    info = TrainedModelInfo(
        model_id=model_id, algorithm=best_algorithm, trained_at=datetime.now().astimezone(),
        history_start=complete[0].day, history_end=complete[-1].day, training_days=len(complete),
        horizon_days=horizon_days, targets=list(TARGETS), metrics=selected_metrics, candidates=evaluated,
        feature_count=len(_feature_names()), active=True,
    )
    payload = {"models": models, "residual_std": std, "feature_names": _feature_names(), "horizon_days": horizon_days}
    return TrainOutcome(info=info, payload=payload)


class ModelRegistry:
    """Persistência do modelo ativo em disco (joblib + metadados JSON), com gravação atômica."""

    def __init__(self, storage_dir: str | Path):
        self.dir = Path(storage_dir)
        self._lock = asyncio.Lock()

    @property
    def model_path(self) -> Path:
        return self.dir / "active.joblib"

    @property
    def meta_path(self) -> Path:
        return self.dir / "active.json"

    def load_info(self) -> TrainedModelInfo | None:
        if not self.meta_path.exists():
            return None
        try:
            data = json.loads(self.meta_path.read_text(encoding="utf-8"))
            return TrainedModelInfo.model_validate(data)
        except (ValueError, OSError) as exception:
            logger.warning("Metadados de modelo inválidos: %s", exception)
            return None

    def load_payload(self) -> dict[str, Any] | None:
        if not self.model_path.exists():
            return None
        try:
            payload: dict[str, Any] = joblib.load(self.model_path)
            return payload
        except (ValueError, OSError, EOFError) as exception:
            logger.warning("Modelo persistido inválido: %s", exception)
            return None

    def save(self, outcome: TrainOutcome) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self._atomic_write(self.model_path, lambda path: joblib.dump(outcome.payload, path))
        meta = outcome.info.model_dump(mode="json", by_alias=True)
        self._atomic_write(self.meta_path, lambda path: Path(path).write_text(
            json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"))

    def delete(self) -> bool:
        removed = False
        for path in (self.model_path, self.meta_path):
            if path.exists():
                path.unlink()
                removed = True
        return removed

    @staticmethod
    def _atomic_write(target: Path, writer: Any) -> None:
        fd, temp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
        os.close(fd)
        try:
            writer(temp)
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)

    async def train_and_save(self, series: list[DayStats], *, algorithm: str, horizon_days: int,
                             min_train_days: int) -> TrainOutcome:
        async with self._lock:
            outcome = await asyncio.to_thread(
                train_models, series, algorithm=algorithm, horizon_days=horizon_days, min_train_days=min_train_days,
            )
            await asyncio.to_thread(self.save, outcome)
            return outcome


def predict_with_model(info: TrainedModelInfo, payload: dict[str, Any], series: list[DayStats],
                       horizon_days: int | None = None) -> ForecastSummary:
    """Projeta os próximos dias com o modelo treinado, usando as defasagens reais mais recentes."""
    complete = _complete_series(series)
    horizon = horizon_days or info.horizon_days
    if len(complete) < max(LAGS):
        return ForecastSummary(
            available=False, model_kind="ml", model_id=info.model_id,
            message=f"São necessários ao menos {max(LAGS)} dias completos recentes para projetar com o modelo.",
        )
    models = payload.get("models") or {}
    if not all(name in models for name in TARGETS):
        return ForecastSummary(available=False, model_kind="ml", message="O modelo persistido está incompleto.")
    pairs = [(float(day.temperature_mean), float(day.humidity_mean)) for day in complete]  # type: ignore[arg-type]
    last = complete[-1].day
    points = _recursive_predict(models, pairs, last, horizon)
    std = payload.get("residual_std", {})
    predictions: list[dict[str, float | str]] = []
    for step, point in enumerate(points):
        row: dict[str, float | str] = {"date": (last + timedelta(days=step + 1)).isoformat()}
        for name in TARGETS:
            value = point[name]
            spread = CONFIDENCE_Z * float(std.get(name, 0.0))
            low, high = value - spread, value + spread
            if name == "humidity":
                value, low, high = (min(max(item, 0.0), 100.0) for item in (value, low, high))
            row[name] = round(value, 2)
            row[f"{name}Low"] = round(low, 2)
            row[f"{name}High"] = round(high, 2)
        predictions.append(row)
    return ForecastSummary(
        available=True, model=f"{info.algorithm}-{info.training_days}d", model_kind="ml", model_id=info.model_id,
        trained_at=info.trained_at, horizon_days=horizon, predictions=predictions, metrics=info.metrics,
        confidence_level=CONFIDENCE_LEVEL, message=ML_DISCLAIMER,
    )
