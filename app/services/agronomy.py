"""Modelos agronômicos por plantação, calculados do histórico climático real da estação.

Para cada plantação ativa, acumula graus-dia (GDD) desde o plantio, projeta a janela de colheita
pelo ritmo recente, estima risco de míldio/oídio e a necessidade hídrica por evapotranspiração
(Hargreaves ET0 × Kc). Reaproveita os cálculos de ``indicators.py`` (GDD, doença, colheita) para
não duplicar regra agronômica.

Limitações honestas: só há temperatura e umidade (sem chuva, molhamento foliar nem umidade do
solo). A ET0 de Hargreaves usa radiação extraterrestre pela latitude e o dia do ano; a necessidade
hídrica é uma estimativa de demanda atmosférica, não uma lâmina de irrigação prescrita.
"""

from __future__ import annotations

from datetime import date, datetime
import math
from typing import Any

from app.models import (
    AgronomySummary,
    HarvestWindow,
    IrrigationEstimate,
    PlantationAgronomy,
    PlantationStatus,
    RiskLevel,
)
from app.services.alerts import vapor_pressure_deficit
from app.services.daily import DayStats
from app.services.indicators import (
    DEFAULT_BASE_TEMP,
    DEFAULT_TARGET_GDD,
    DEFAULT_UPPER_TEMP,
    RECENT_DAYS,
    daily_gdd,
    disease_hours,
    project_harvest,
    risk_level,
)

# Constante solar (MJ m^-2 min^-1) e conversão de MJ para mm de evaporação (FAO-56).
SOLAR_CONSTANT = 0.0820
LATENT_HEAT_FACTOR = 2.45
HARGREAVES_COEFF = 0.0023

AGRONOMY_ASSUMPTIONS = [
    "Graus-dia (GDD) acumulados desde a data de plantio, com base 10 °C e teto 35 °C, só em dias completos.",
    "O alvo de GDD é um valor de referência para uva de mesa; calibre-o para a variedade e o ciclo do vinhedo.",
    "Colheita projetada pelo ritmo recente de GDD (cenários rápido / típico / lento), não por data fixa.",
    "Risco de míldio e oídio por horas de temperatura e umidade favoráveis; sem sensor de molhamento foliar ou chuva.",
    "Necessidade hídrica = ET0 (Hargreaves, pela latitude e dia do ano) × Kc; é demanda atmosférica estimada, "
    "não lâmina de irrigação prescrita (não há sensor de chuva nem de umidade do solo).",
    "Dias contados no fuso local configurado (CLIMATE_UTC_OFFSET_HOURS, padrão -3).",
]


def extraterrestrial_radiation(latitude_deg: float, day_of_year: int) -> float:
    """Radiação extraterrestre diária Ra (MJ m^-2 dia^-1) — FAO-56, eq. 21."""
    phi = math.radians(latitude_deg)
    dr = 1 + 0.033 * math.cos(2 * math.pi * day_of_year / 365)
    delta = 0.409 * math.sin(2 * math.pi * day_of_year / 365 - 1.39)
    ws = math.acos(max(-1.0, min(1.0, -math.tan(phi) * math.tan(delta))))
    ra = (24 * 60 / math.pi) * SOLAR_CONSTANT * dr * (
        ws * math.sin(phi) * math.sin(delta) + math.cos(phi) * math.cos(delta) * math.sin(ws)
    )
    return max(ra, 0.0)


def hargreaves_et0(tmin: float, tmax: float, latitude_deg: float, day_of_year: int) -> float:
    """ET0 de Hargreaves (mm/dia) — FAO-56, eq. 52, com Ra convertida para mm."""
    tmean = (tmin + tmax) / 2
    ra_mm = extraterrestrial_radiation(latitude_deg, day_of_year) / LATENT_HEAT_FACTOR
    spread = max(tmax - tmin, 0.0)
    et0 = HARGREAVES_COEFF * ra_mm * (tmean + 17.8) * math.sqrt(spread)
    return round(max(et0, 0.0), 2)


def _parse_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
        except ValueError:
            return None
    return None


def _plantation_status(plantation: dict[str, Any]) -> PlantationStatus:
    status = str(plantation.get("status", "")).casefold()
    if plantation.get("archivedAt") or "arquiv" in status:
        return PlantationStatus.ARCHIVED
    if plantation.get("harvestedAt") or "colhid" in status or "harvest" in status:
        return PlantationStatus.HARVESTED
    return PlantationStatus.GROWING


def _irrigation(recent: list[DayStats], latitude: float, kc: float) -> IrrigationEstimate:
    usable = [day for day in recent if day.temperature_min is not None and day.temperature_max is not None]
    if not usable:
        return IrrigationEstimate(
            crop_coefficient=kc, recent_days=0,
            message="Sem dias completos recentes para estimar a evapotranspiração.",
        )
    et0_values = [hargreaves_et0(day.temperature_min, day.temperature_max, latitude, day.day.timetuple().tm_yday)  # type: ignore[arg-type]
                  for day in usable]
    et0 = round(sum(et0_values) / len(et0_values), 2)
    vpd_values = [vapor_pressure_deficit(day.temperature_mean, day.humidity_mean)
                  for day in usable if day.temperature_mean is not None and day.humidity_mean is not None]
    vpd = round(sum(vpd_values) / len(vpd_values), 2) if vpd_values else None
    return IrrigationEstimate(
        et0_mm_day=et0, etc_mm_day=round(et0 * kc, 2), crop_coefficient=kc, vpd_kpa=vpd, recent_days=len(usable),
        message="Demanda atmosférica estimada (ET0 Hargreaves × Kc). Ajuste a irrigação com chuva e "
                "umidade do solo reais.",
    )


def build_plantation_agronomy(plantation: dict[str, Any], days: list[DayStats], *, base_temp: float,
                              upper_temp: float, target_gdd: float, latitude: float, kc: float,
                              utc_offset_hours: float) -> PlantationAgronomy:
    """Indicadores de uma plantação. `days` é a série diária completa da estação (ordenada)."""
    from app.services.daily import local_zone

    plantation_id = plantation.get("id")
    variety = str(plantation.get("variety") or "—")
    field = plantation.get("field")
    status = _plantation_status(plantation)
    planted_at = _parse_date(plantation.get("plantedAt"))
    if planted_at is not None and plantation.get("plantedAt"):
        raw = plantation["plantedAt"]
        if isinstance(raw, str):
            try:
                planted_at = datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(
                    local_zone(utc_offset_hours)).date()
            except ValueError:
                pass
        elif isinstance(raw, datetime):
            planted_at = raw.astimezone(local_zone(utc_offset_hours)).date()

    complete = [day for day in days if day.complete]
    if planted_at is not None:
        scoped = [day for day in complete if day.day >= planted_at]
    else:
        scoped = complete
    days_since = (scoped[-1].day - planted_at).days + 1 if (scoped and planted_at) else None

    accumulated = 0.0
    reached_on: date | None = None
    rates: list[float] = []
    for day in scoped:
        if day.temperature_min is None or day.temperature_max is None:
            continue
        gdd = daily_gdd(day.temperature_min, day.temperature_max, base_temp, upper_temp)
        accumulated = round(accumulated + gdd, 2)
        rates.append(gdd)
        if reached_on is None and accumulated >= target_gdd:
            reached_on = day.day

    recent = scoped[-RECENT_DAYS:]
    high_risk = 0
    downy_risk: RiskLevel = "INSUFFICIENT_DATA"
    powdery_risk: RiskLevel = "INSUFFICIENT_DATA"
    for day in recent:
        downy, powdery = disease_hours(day)
        if risk_level(downy) == "HIGH" or risk_level(powdery) == "HIGH":
            high_risk += 1
    if recent:
        last = recent[-1]
        downy, powdery = disease_hours(last)
        downy_risk, powdery_risk = risk_level(downy), risk_level(powdery)

    recent_rates = [daily_gdd(d.temperature_min, d.temperature_max, base_temp, upper_temp)
                    for d in recent if d.temperature_min is not None and d.temperature_max is not None]
    harvest = _harvest(scoped[-1].day if scoped else None, accumulated, target_gdd, reached_on, recent_rates, high_risk)
    progress = round(min(accumulated / target_gdd * 100, 100.0), 1) if target_gdd > 0 else None
    message = None
    if status != PlantationStatus.GROWING:
        message = "Plantação colhida ou arquivada; indicadores apenas para consulta do ciclo encerrado."
    elif planted_at is None:
        message = "Sem data de plantio cadastrada; GDD acumulado sobre todo o histórico disponível."
    elif not scoped:
        message = "Ainda não há dias completos de leitura após a data de plantio."

    return PlantationAgronomy(
        plantation_id=plantation_id, variety=variety, field=field, status=status, planted_at=planted_at,
        days_since_planting=days_since, target_gdd=target_gdd, accumulated_gdd=accumulated,
        gdd_progress_percent=progress, harvest=harvest, downy_mildew_risk=downy_risk,
        powdery_mildew_risk=powdery_risk, recent_high_risk_days=high_risk,
        irrigation=_irrigation(recent, latitude, kc), complete_days=len(scoped), message=message,
    )


def _harvest(last_day: date | None, accumulated: float, target: float, reached_on: date | None,
             recent_rates: list[float], high_risk: int) -> HarvestWindow | None:
    if last_day is None:
        return None
    remaining = round(max(target - accumulated, 0.0), 2)
    recent_rate = round(sum(recent_rates) / len(recent_rates), 2) if recent_rates else None
    common = {"target_gdd": target, "accumulated_gdd": accumulated, "remaining_gdd": remaining,
              "recent_daily_gdd": recent_rate, "recent_high_risk_days": high_risk}
    if reached_on is not None:
        return HarvestWindow(status="REACHED", reached_on=reached_on,
                             message="O alvo de graus-dia já foi atingido desde o plantio.", **common)
    projection = project_harvest(last_day, remaining, recent_rates)
    messages = {
        "PROJECTED": "Colheita projetada pelo ritmo recente de graus-dia desde o plantio.",
        "BEYOND_HORIZON": "O alvo não deve ser atingido no horizonte de projeção no ritmo atual.",
        "INSUFFICIENT_DATA": "São necessários ao menos 3 dias completos recentes para projetar a colheita.",
    }
    return HarvestWindow(status=projection.status, earliest=projection.earliest, expected=projection.expected,
                         latest=projection.latest, message=messages[projection.status], **common)


def build_agronomy_summary(plantations: list[dict[str, Any]], days: list[DayStats], *,
                           base_temp: float = DEFAULT_BASE_TEMP, upper_temp: float = DEFAULT_UPPER_TEMP,
                           target_gdd: float = DEFAULT_TARGET_GDD, latitude: float, kc: float,
                           utc_offset_hours: float) -> AgronomySummary:
    """Monta os indicadores de todas as plantações visíveis ao usuário (escopo definido pela API Java)."""
    items = [
        build_plantation_agronomy(plantation, days, base_temp=base_temp, upper_temp=upper_temp,
                                  target_gdd=target_gdd, latitude=latitude, kc=kc, utc_offset_hours=utc_offset_hours)
        for plantation in plantations
    ]
    items.sort(key=lambda item: (item.status != PlantationStatus.GROWING, item.variety))
    available = bool(items) and any(day.complete for day in days)
    message = None
    if not plantations:
        message = "Nenhuma plantação cadastrada para calcular indicadores agronômicos."
    elif not available:
        message = "Ainda não há dias completos de leitura para calcular os indicadores agronômicos."
    return AgronomySummary(
        available=available, base_temp=base_temp, upper_temp=upper_temp, latitude=latitude, crop_coefficient=kc,
        plantations=items, assumptions=AGRONOMY_ASSUMPTIONS, message=message,
    )
