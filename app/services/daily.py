from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, timezone
import math
from typing import Any

import pandas as pd

from app.services.analysis import _metric_key, _timestamp

TEMPERATURE_KEYS = frozenset({"temperature", "temperaturec", "tempc"})
HUMIDITY_KEYS = frozenset({"humidity", "relativehumidity"})

# Um dia só entra nos cálculos "completos" (graus-dia, anomalias) com leitura em pelo menos 18 das 24 horas.
MIN_HOURS_PER_DAY = 18

# Faixas físicas plausíveis; fora delas a leitura é ignorada (sensor com defeito).
_TEMPERATURE_RANGE = (-40.0, 60.0)
_HUMIDITY_RANGE = (0.0, 100.0)


@dataclass
class DayStats:
    """Um dia local com a média horária de temperatura e umidade."""

    day: date
    hourly: list[tuple[float | None, float | None]] = field(default_factory=list)  # (°C, %UR) por hora
    temperature_min: float | None = None
    temperature_max: float | None = None

    @property
    def hours(self) -> int:
        return sum(1 for temperature, humidity in self.hourly if temperature is not None or humidity is not None)

    @property
    def complete(self) -> bool:
        return self.hours >= MIN_HOURS_PER_DAY

    @property
    def temperature_mean(self) -> float | None:
        return _mean([temperature for temperature, _ in self.hourly])

    @property
    def humidity_mean(self) -> float | None:
        return _mean([humidity for _, humidity in self.hourly])


def _mean(values: list[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return sum(present) / len(present) if present else None


def _number(value: Any, low: float, high: float) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < low or number > high:
        return None
    return number


def local_zone(utc_offset_hours: float) -> timezone:
    return timezone(timedelta(hours=utc_offset_hours))


def local_midnight_utc(reference: datetime, days_back: int, utc_offset_hours: float) -> datetime:
    """00:00 local de `days_back` dias antes de `reference`, convertido para UTC."""
    zone = local_zone(utc_offset_hours)
    local = reference.astimezone(zone) - timedelta(days=days_back)
    return local.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)


def build_days(readings: list[dict[str, Any]], utc_offset_hours: float) -> tuple[list[DayStats], int]:
    """Agrupa as leituras em dias locais (ordenados). Leituras REJECTED são descartadas e contadas."""
    zone = local_zone(utc_offset_hours)
    rows: list[dict[str, Any]] = []
    rejected = 0
    for reading in readings:
        stamp = _timestamp(reading.get("capturedAt", reading.get("captured_at")))
        if stamp is None:
            continue
        quality = str(reading.get("qualityStatus", reading.get("quality_status", "VALID"))).upper()
        if quality == "REJECTED":
            rejected += 1
            continue
        measurements = reading.get("measurements")
        if not isinstance(measurements, dict):
            continue
        temperature = humidity = None
        for name, raw in measurements.items():
            key = _metric_key(str(name))
            if key in TEMPERATURE_KEYS:
                temperature = _number(raw, *_TEMPERATURE_RANGE)
            elif key in HUMIDITY_KEYS:
                humidity = _number(raw, *_HUMIDITY_RANGE)
        if temperature is None and humidity is None:
            continue
        local = stamp.astimezone(zone).replace(tzinfo=None)
        rows.append({"hour": pd.Timestamp(local).floor("h"), "temperature": temperature, "humidity": humidity})

    if not rows:
        return [], rejected

    frame = pd.DataFrame(rows)
    grouped = frame.groupby("hour").agg(
        temperature=("temperature", "mean"), humidity=("humidity", "mean"),
        t_min=("temperature", "min"), t_max=("temperature", "max"),
    )
    days: dict[date, DayStats] = {}
    for hour, item in grouped.iterrows():
        stamp = pd.Timestamp(str(hour))
        stats = days.setdefault(stamp.date(), DayStats(day=stamp.date()))
        stats.hourly.append((_clean(item["temperature"]), _clean(item["humidity"])))
        low, high = _clean(item["t_min"]), _clean(item["t_max"])
        if low is not None and (stats.temperature_min is None or low < stats.temperature_min):
            stats.temperature_min = low
        if high is not None and (stats.temperature_max is None or high > stats.temperature_max):
            stats.temperature_max = high
    return [days[key] for key in sorted(days)], rejected


def _clean(value: Any) -> float | None:
    number = float(value)
    return None if math.isnan(number) else number
