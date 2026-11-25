from __future__ import annotations

from datetime import date, timedelta
import math

import numpy as np

from app.models import Alert, AlertsSummary, DailyTrend, ForecastSummary
from app.services.daily import MIN_HOURS_PER_DAY, DayStats

FROST_WARNING_C = 3.0
FROST_CRITICAL_C = 0.0
# Déficit de pressão de vapor (kPa): demanda atmosférica por água, usada como aproximação de estresse hídrico.
VPD_WARNING_KPA = 3.0
VPD_CRITICAL_KPA = 4.0
VPD_MIN_HOURS = 4

ANOMALY_BASELINE_DAYS = 14
ANOMALY_MIN_BASELINE = 7
ANOMALY_CRITICAL_Z = 3.0

FORECAST_LOOKBACK_DAYS = 14
FORECAST_MIN_DAYS = 7
FORECAST_HORIZON_DAYS = 3

ALERT_ASSUMPTIONS = [
    f"Geada: temperatura mínima do dia ≤ {FROST_WARNING_C:g} °C (atenção) ou ≤ {FROST_CRITICAL_C:g} °C (crítico).",
    f"Estresse hídrico: aproximação pelo déficit de pressão de vapor (DPV) calculado de temperatura e umidade, "
    f"com ≥ {VPD_MIN_HOURS} h acima de {VPD_WARNING_KPA:g} kPa (atenção) ou {VPD_CRITICAL_KPA:g} kPa (crítico). "
    "Não mede a água no solo.",
    f"Anomalia: z-score da média diária contra os {ANOMALY_BASELINE_DAYS} dias completos anteriores "
    f"(mínimo de {ANOMALY_MIN_BASELINE} dias).",
    "Dias são contados no fuso local configurado (CLIMATE_UTC_OFFSET_HOURS, padrão -3).",
]


def vapor_pressure_deficit(temperature: float, humidity: float) -> float:
    """DPV em kPa (equação de Tetens)."""
    saturation = 0.6108 * math.exp(17.27 * temperature / (temperature + 237.3))
    return saturation * (1 - humidity / 100)


def moving_average(values: list[float | None], window: int) -> list[float | None]:
    """Média móvel simples; None até haver `window` valores válidos consecutivos na janela."""
    result: list[float | None] = []
    for index in range(len(values)):
        chunk = values[max(0, index - window + 1): index + 1]
        present = [value for value in chunk if value is not None]
        result.append(round(sum(present) / window, 2) if len(chunk) == window and len(present) == window else None)
    return result


def zscores(values: list[float | None]) -> list[float | None]:
    """Z-score de cada valor contra os até 14 valores anteriores (sem incluir o próprio)."""
    result: list[float | None] = []
    for index, value in enumerate(values):
        baseline = [item for item in values[max(0, index - ANOMALY_BASELINE_DAYS): index] if item is not None]
        if value is None or len(baseline) < ANOMALY_MIN_BASELINE:
            result.append(None)
            continue
        spread = float(np.std(baseline, ddof=1))
        result.append(None if spread < 0.01 else round((value - float(np.mean(baseline))) / spread, 2))
    return result


def _daily_alerts(day: DayStats) -> list[Alert]:
    alerts: list[Alert] = []
    if day.temperature_min is not None and day.temperature_min <= FROST_WARNING_C:
        critical = day.temperature_min <= FROST_CRITICAL_C
        alerts.append(Alert(
            type="FROST", severity="CRITICAL" if critical else "WARNING", date=day.day, metric="temperature_min",
            value=round(day.temperature_min, 2), threshold=FROST_CRITICAL_C if critical else FROST_WARNING_C,
            message=f"Risco de geada: temperatura mínima de {day.temperature_min:.1f} °C.",
        ))
    deficits = [vapor_pressure_deficit(t, h) for t, h in day.hourly if t is not None and h is not None]
    if deficits:
        peak = max(deficits)
        critical_hours = sum(1 for item in deficits if item >= VPD_CRITICAL_KPA)
        warning_hours = sum(1 for item in deficits if item >= VPD_WARNING_KPA)
        if critical_hours >= VPD_MIN_HOURS:
            alerts.append(_stress_alert(day.day, "CRITICAL", peak, VPD_CRITICAL_KPA, critical_hours))
        elif warning_hours >= VPD_MIN_HOURS:
            alerts.append(_stress_alert(day.day, "WARNING", peak, VPD_WARNING_KPA, warning_hours))
    return alerts


def _stress_alert(day: date, severity: str, peak: float, threshold: float, hours: int) -> Alert:
    return Alert(
        type="WATER_STRESS", severity=severity, date=day, metric="vpd_kpa",
        value=round(peak, 2), threshold=threshold,
        message=f"Alta demanda atmosférica por água: {hours} h com DPV acima de {threshold:g} kPa "
                f"(pico de {peak:.1f} kPa).",
    )


def build_alerts(days: list[DayStats], *, window_days: int, average_window: int, z_threshold: float,
                 first_day: date) -> AlertsSummary:
    """Alertas e séries do período `first_day` em diante; os dias anteriores servem só de base para anomalias."""
    complete = [day for day in days if day.complete]
    if not days:
        return AlertsSummary(available=False, window_days=window_days, moving_average_window=average_window,
                             z_threshold=z_threshold, assumptions=ALERT_ASSUMPTIONS,
                             message="Ainda não há leituras suficientes para gerar alertas.")

    temperatures = [day.temperature_mean for day in complete]
    humidities = [day.humidity_mean for day in complete]
    t_ma, h_ma = moving_average(temperatures, average_window), moving_average(humidities, average_window)
    t_z, h_z = zscores(temperatures), zscores(humidities)

    series: list[DailyTrend] = []
    alerts: list[Alert] = []
    for index, day in enumerate(complete):
        if day.day < first_day:
            continue
        series.append(DailyTrend(
            date=day.day, temperature=_round(temperatures[index]), humidity=_round(humidities[index]),
            temperature_moving_average=t_ma[index], humidity_moving_average=h_ma[index],
            temperature_zscore=t_z[index], humidity_zscore=h_z[index],
        ))
        for kind, label, unit, value, z in (
            ("TEMPERATURE_ANOMALY", "temperatura", "°C", temperatures[index], t_z[index]),
            ("HUMIDITY_ANOMALY", "umidade", "%", humidities[index], h_z[index]),
        ):
            if z is not None and value is not None and abs(z) >= z_threshold:
                direction = "acima" if z > 0 else "abaixo"
                alerts.append(Alert(
                    type=kind, severity="CRITICAL" if abs(z) >= ANOMALY_CRITICAL_Z else "WARNING",
                    date=day.day, metric="temperature_mean" if kind == "TEMPERATURE_ANOMALY" else "humidity_mean",
                    value=round(value, 2), threshold=z_threshold,
                    message=f"Média diária de {label} ({value:.1f} {unit}) {direction} do padrão recente "
                            f"(z = {z:+.1f}).",
                ))
    for day in days:
        if day.day >= first_day:
            alerts.extend(_daily_alerts(day))

    order = {"CRITICAL": 0, "WARNING": 1, "INFO": 2}
    alerts.sort(key=lambda item: (-item.date.toordinal(), order[item.severity]))
    return AlertsSummary(
        available=True, window_days=window_days, moving_average_window=average_window, z_threshold=z_threshold,
        complete_days=len([day for day in complete if day.day >= first_day]),
        series=series, alerts=alerts, assumptions=ALERT_ASSUMPTIONS,
        message=None if series else f"Nenhum dia completo (≥ {MIN_HOURS_PER_DAY} h de leitura) no período.",
    )


def build_forecast(days: list[DayStats]) -> ForecastSummary:
    """Projeção estatística simples: regressão linear sobre os últimos dias completos, para os próximos dias."""
    recent = [day for day in days if day.complete and day.temperature_mean is not None
              and day.humidity_mean is not None][-FORECAST_LOOKBACK_DAYS:]
    if len(recent) < FORECAST_MIN_DAYS:
        return ForecastSummary(
            available=False,
            message=f"São necessários ao menos {FORECAST_MIN_DAYS} dias completos de leitura para projetar.",
        )
    x = np.array([day.day.toordinal() for day in recent], dtype=float)
    fits = {}
    for name, values in (("temperature", [day.temperature_mean for day in recent]),
                         ("humidity", [day.humidity_mean for day in recent])):
        y = np.array(values, dtype=float)
        slope, intercept = np.polyfit(x, y, 1)
        residual = float(np.std(y - (slope * x + intercept), ddof=2)) if len(y) > 2 else 0.0
        fits[name] = (float(slope), float(intercept), residual)

    last = recent[-1].day
    predictions: list[dict[str, float | str]] = []
    for step in range(1, FORECAST_HORIZON_DAYS + 1):
        target = last + timedelta(days=step)
        row: dict[str, float | str] = {"date": target.isoformat()}
        for name, (slope, intercept, residual) in fits.items():
            value = slope * target.toordinal() + intercept
            low, high = value - residual, value + residual
            if name == "humidity":
                value, low, high = (min(max(item, 0.0), 100.0) for item in (value, low, high))
            row[name] = round(value, 2)
            row[f"{name}Low"] = round(low, 2)
            row[f"{name}High"] = round(high, 2)
        predictions.append(row)
    return ForecastSummary(
        available=True, model=f"regressao-linear-{len(recent)}d", predictions=predictions,
        message="Projeção estatística simples (tendência dos últimos dias), não é previsão meteorológica. "
                "A faixa Low/High é ± 1 desvio-padrão dos resíduos do ajuste.",
    )


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
