from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import numpy as np
import pandas as pd

from app.models import ClimatePoint, ClimateSummary, MetricSummary

# Únicas métricas do canal ThingSpeak tratadas: temperatura (field1) e umidade (field2).
SUPPORTED_METRIC_KEYS = frozenset({"temperature", "humidity"})


UNITS = {
    "temperature": "°C",
    "temperaturec": "°C",
    "tempc": "°C",
    "humidity": "%",
    "relativehumidity": "%",
}


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _metric_key(value: str) -> str:
    return value.lower().replace("_", "")


def summarize_climate(readings: list[dict[str, Any]], *, channel_id: str | None = None,
                      rejected_records: int = 0, source: str = "ThingSpeak") -> ClimateSummary:
    rows: list[dict[str, Any]] = []
    measurement_keys: set[str] = set()
    normalized: list[tuple[datetime, dict[str, float | None], str]] = []
    for reading in readings:
        stamp = _timestamp(reading.get("capturedAt", reading.get("captured_at")))
        if stamp is None:
            continue
        measurements = reading.get("measurements")
        if not isinstance(measurements, dict):
            measurements = {}
        values: dict[str, float | None] = {}
        for name, raw_value in measurements.items():
            if _metric_key(str(name)) not in SUPPORTED_METRIC_KEYS:
                continue  # medições antigas de outros campos não entram no resumo
            try:
                number = float(raw_value)
                if not np.isfinite(number):
                    continue
            except (TypeError, ValueError, OverflowError):
                continue
            values[str(name)] = number
            measurement_keys.add(str(name))
        quality = str(reading.get("qualityStatus", reading.get("quality_status", "VALID"))).upper()
        normalized.append((stamp, values, quality))
        row: dict[str, Any] = {"timestamp": stamp}
        row.update(values)
        rows.append(row)

    normalized.sort(key=lambda item: item[0])
    if not normalized:
        return ClimateSummary(available=False, source=source, channel_id=channel_id, records=0,
                              rejected_records=rejected_records, message="Ainda não há leituras climáticas disponíveis.")

    frame = pd.DataFrame(rows).sort_values("timestamp", kind="stable")
    metrics: dict[str, MetricSummary] = {}
    for name in sorted(measurement_keys):
        if name not in frame:
            continue
        series = pd.to_numeric(frame[name], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        present = series[np.isfinite(series)]
        if present.size == 0:
            continue
        latest_value = float(present[-1])
        metrics[name] = MetricSummary(
            current=latest_value,
            average=float(np.nanmean(present)),
            minimum=float(np.nanmin(present)),
            maximum=float(np.nanmax(present)),
            unit=UNITS.get(_metric_key(name), ""),
            samples=int(present.size),
        )

    points = [ClimatePoint(timestamp=stamp, values=values, quality_status=quality if quality in {"VALID", "REVIEW", "REJECTED"} else "REVIEW") for stamp, values, quality in normalized]
    latest_at = normalized[-1][0]
    return ClimateSummary(
        available=True,
        source=source,
        channel_id=channel_id,
        latest_at=latest_at,
        records=len(normalized),
        metrics=metrics,
        series=points,
        rejected_records=rejected_records,
        message=None,
    )


def summarize_quality(readings: list[dict[str, Any]], *, expected_metrics: set[str] | None = None) -> dict[str, Any]:
    total = len(readings)
    valid = review = rejected = 0
    measured_cells = expected_cells = 0
    for reading in readings:
        status = str(reading.get("qualityStatus", reading.get("quality_status", "VALID"))).upper()
        if status == "VALID":
            valid += 1
        elif status == "REVIEW":
            review += 1
        elif status == "REJECTED":
            rejected += 1
        values = reading.get("measurements") if isinstance(reading.get("measurements"), dict) else {}
        expected = expected_metrics or set(values)
        expected_cells += len(expected)
        measured_cells += sum(1 for key in expected if values.get(key) is not None)
    completeness = round(measured_cells / expected_cells * 100, 2) if expected_cells else None
    return {
        "available": total > 0,
        "totalRecords": total,
        "validRecords": valid,
        "reviewRecords": review,
        "rejectedRecords": rejected,
        "completenessPercent": completeness,
    }
