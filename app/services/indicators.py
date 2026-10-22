from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
import math

import numpy as np

from app.models import DailyIndicator, ExportIndicators, HarvestWindow, RiskLevel
from app.services.daily import MIN_HOURS_PER_DAY, DayStats

DEFAULT_BASE_TEMP = 10.0
DEFAULT_UPPER_TEMP = 35.0
# Valor de referência para uvas de mesa/vinho no Vale do São Francisco; deve ser calibrado por variedade.
DEFAULT_TARGET_GDD = 1500.0

# Faixas usadas como aproximação (só há temperatura e umidade, sem molhamento foliar nem chuva).
DOWNY_MILDEW_TEMP = (15.0, 30.0)
DOWNY_MILDEW_MIN_RH = 90.0
POWDERY_MILDEW_TEMP = (20.0, 27.0)
POWDERY_MILDEW_MIN_RH = 60.0
RISK_MODERATE_HOURS = 4
RISK_HIGH_HOURS = 8

HARVEST_HORIZON_DAYS = 120
RECENT_DAYS = 7
MIN_RECENT_DAYS = 3

ASSUMPTIONS = [
    "Graus-dia pela média de Tmin e Tmax, com limites inferior e superior (base_temp e upper_temp).",
    "O alvo de graus-dia (target_gdd) é um valor de referência; calibre-o para a variedade e o ciclo do vinhedo.",
    "Dias com menos de 18 horas de leitura não entram na soma; se houver dias assim, o acumulado é um mínimo.",
    "Risco de míldio e oídio é uma aproximação por horas de temperatura e umidade favoráveis; "
    "não há sensor de molhamento foliar nem de chuva.",
    "Dias são contados no fuso local configurado (CLIMATE_UTC_OFFSET_HOURS, padrão -3).",
]


def daily_gdd(temperature_min: float, temperature_max: float, base: float, upper: float) -> float:
    """Graus-dia do dia: Tmin e Tmax são limitados a [base, upper] antes da média."""
    low = min(max(temperature_min, base), upper)
    high = min(max(temperature_max, base), upper)
    return round((low + high) / 2 - base, 2)


def risk_level(hours: int) -> RiskLevel:
    if hours >= RISK_HIGH_HOURS:
        return "HIGH"
    if hours >= RISK_MODERATE_HOURS:
        return "MODERATE"
    return "LOW"


def disease_hours(day: DayStats) -> tuple[int, int]:
    """Horas favoráveis ao míldio e ao oídio no dia."""
    downy = powdery = 0
    for temperature, humidity in day.hourly:
        if temperature is None or humidity is None:
            continue
        if DOWNY_MILDEW_TEMP[0] <= temperature <= DOWNY_MILDEW_TEMP[1] and humidity >= DOWNY_MILDEW_MIN_RH:
            downy += 1
        if POWDERY_MILDEW_TEMP[0] <= temperature <= POWDERY_MILDEW_TEMP[1] and humidity >= POWDERY_MILDEW_MIN_RH:
            powdery += 1
    return downy, powdery


@dataclass(frozen=True)
class _Projection:
    status: str
    earliest: date | None = None
    expected: date | None = None
    latest: date | None = None


def project_harvest(last_day: date, remaining: float, recent_rates: list[float]) -> _Projection:
    """Projeta a data em que o alvo é atingido usando o ritmo recente de graus-dia (P75 / média / P25)."""
    if len(recent_rates) < MIN_RECENT_DAYS:
        return _Projection("INSUFFICIENT_DATA")
    rates = np.array(recent_rates, dtype=float)
    fast, typical, slow = float(np.percentile(rates, 75)), float(rates.mean()), float(np.percentile(rates, 25))
    if typical <= 0.1 or remaining / typical > HARVEST_HORIZON_DAYS:
        return _Projection("BEYOND_HORIZON")

    def when(rate: float) -> date | None:
        if rate <= 0.1 or remaining / rate > HARVEST_HORIZON_DAYS:
            return None
        return last_day + timedelta(days=math.ceil(remaining / rate))

    return _Projection("PROJECTED", earliest=when(fast), expected=when(typical), latest=when(slow))


def build_export_indicators(days: list[DayStats], *, rejected: int, base_temp: float, upper_temp: float,
                            target_gdd: float) -> ExportIndicators:
    if not days:
        return ExportIndicators(
            available=False, base_temp=base_temp, upper_temp=upper_temp, target_gdd=target_gdd,
            rejected_records=rejected, assumptions=ASSUMPTIONS,
            message="Ainda não há leituras suficientes no período para calcular os indicadores.",
        )

    items: list[DailyIndicator] = []
    accumulated = 0.0
    reached_on: date | None = None
    rates: list[float] = []
    for day in days:
        gdd = cumulative = None
        if day.complete and day.temperature_min is not None and day.temperature_max is not None:
            gdd = daily_gdd(day.temperature_min, day.temperature_max, base_temp, upper_temp)
            accumulated = round(accumulated + gdd, 2)
            cumulative = accumulated
            rates.append(gdd)
            if reached_on is None and accumulated >= target_gdd:
                reached_on = day.day
        downy, powdery = disease_hours(day)
        items.append(DailyIndicator(
            date=day.day, complete=day.complete, hours_with_data=day.hours,
            temperature_min=_round(day.temperature_min), temperature_mean=_round(day.temperature_mean),
            temperature_max=_round(day.temperature_max), humidity_mean=_round(day.humidity_mean),
            gdd=gdd, gdd_accumulated=cumulative,
            downy_mildew_hours=downy if day.complete else None,
            downy_mildew_risk=risk_level(downy) if day.complete else "INSUFFICIENT_DATA",
            powdery_mildew_hours=powdery if day.complete else None,
            powdery_mildew_risk=risk_level(powdery) if day.complete else "INSUFFICIENT_DATA",
        ))

    complete = [item for item in items if item.complete]
    recent = complete[-RECENT_DAYS:]
    high_risk = sum(1 for item in recent if "HIGH" in (item.downy_mildew_risk, item.powdery_mildew_risk))
    recent_rate = round(sum(item.gdd or 0 for item in recent) / len(recent), 2) if recent else None
    harvest = _harvest_window(complete[-1].date if complete else None, accumulated, target_gdd, reached_on,
                              [item.gdd or 0 for item in recent], recent_rate, high_risk)
    latest = complete[-1] if complete else None
    return ExportIndicators(
        available=bool(complete),
        period_start=days[0].day, period_end=days[-1].day,
        base_temp=base_temp, upper_temp=upper_temp, target_gdd=target_gdd,
        complete_days=len(complete), incomplete_days=len(items) - len(complete), rejected_records=rejected,
        gdd_accumulated=accumulated,
        current_downy_mildew_risk=latest.downy_mildew_risk if latest else "INSUFFICIENT_DATA",
        current_powdery_mildew_risk=latest.powdery_mildew_risk if latest else "INSUFFICIENT_DATA",
        harvest_window=harvest, days=items, assumptions=ASSUMPTIONS,
        message=None if complete else f"Nenhum dia com pelo menos {MIN_HOURS_PER_DAY} horas de leitura no período.",
    )


def _harvest_window(last_day: date | None, accumulated: float, target: float, reached_on: date | None,
                    recent_rates: list[float], recent_rate: float | None, high_risk_days: int) -> HarvestWindow | None:
    if last_day is None:
        return None
    remaining = round(max(target - accumulated, 0.0), 2)
    common = {"target_gdd": target, "accumulated_gdd": accumulated, "remaining_gdd": remaining,
              "recent_daily_gdd": recent_rate, "recent_high_risk_days": high_risk_days}
    if reached_on is not None:
        return HarvestWindow(status="REACHED", reached_on=reached_on,
                             message="O alvo de graus-dia já foi atingido no período.", **common)
    projection = project_harvest(last_day, remaining, recent_rates)
    messages = {
        "PROJECTED": "Projeção pelo ritmo recente de graus-dia: do cenário mais rápido ao mais lento.",
        "BEYOND_HORIZON": f"O alvo não deve ser atingido nos próximos {HARVEST_HORIZON_DAYS} dias no ritmo atual.",
        "INSUFFICIENT_DATA": f"São necessários ao menos {MIN_RECENT_DAYS} dias completos recentes para projetar.",
    }
    return HarvestWindow(status=projection.status, earliest=projection.earliest, expected=projection.expected,
                         latest=projection.latest, message=messages[projection.status], **common)


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 2)
