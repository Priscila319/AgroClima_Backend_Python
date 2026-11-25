"""Modelos agronômicos por plantação: GDD, colheita, doença e evapotranspiração (Hargreaves)."""

from __future__ import annotations

from datetime import date, timedelta
import math

import pytest

from app.services.agronomy import (
    build_agronomy_summary,
    build_plantation_agronomy,
    extraterrestrial_radiation,
    hargreaves_et0,
)
from app.services.daily import DayStats

UUID_A = "11111111-1111-4111-8111-111111111111"
UUID_B = "22222222-2222-4222-8222-222222222222"


def make_days(n: int, start: date = date(2026, 1, 1), *, temperature: float = 26.0,
              humidity: float = 70.0) -> list[DayStats]:
    days = []
    for i in range(n):
        stats = DayStats(day=start + timedelta(days=i), hourly=[(temperature, humidity)] * 24)
        stats.temperature_min = temperature - 6
        stats.temperature_max = temperature + 6
        days.append(stats)
    return days


def plantation(**overrides):
    # Meio-dia UTC evita a fronteira de fuso (00:00Z vira o dia anterior em UTC-3).
    base = {"id": UUID_A, "variety": "Crimson Seedless", "field": "Talhão A", "status": "Ativa",
            "plantedAt": "2026-01-01T12:00:00Z"}
    base.update(overrides)
    return base


def test_extraterrestrial_radiation_is_positive_and_varies_by_season():
    summer = extraterrestrial_radiation(-9.4, 15)   # janeiro, hemisfério sul
    winter = extraterrestrial_radiation(-9.4, 200)  # julho
    assert summer > 0 and winter > 0
    assert summer > winter  # mais radiação no verão austral


def test_hargreaves_et0_reasonable_for_vale_do_sao_francisco():
    et0 = hargreaves_et0(22, 34, -9.4, 15)
    assert 3.0 < et0 < 9.0  # mm/dia plausível para região semiárida quente
    assert hargreaves_et0(22, 22, -9.4, 15) == 0.0  # sem amplitude térmica -> 0


def test_gdd_accumulates_only_after_planting():
    days = make_days(80)  # 26 °C média, Tmin 20 / Tmax 32 -> GDD = (20+32)/2 - 10 = 16/dia
    result = build_plantation_agronomy(plantation(), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.accumulated_gdd == pytest.approx(16 * 80, abs=1)
    assert result.days_since_planting == 80
    assert result.complete_days == 80
    assert result.gdd_progress_percent == pytest.approx(85.3, abs=0.5)


def test_gdd_ignores_days_before_planting():
    days = make_days(80)
    late = plantation(plantedAt="2026-03-01T00:00:00Z")  # ~60 dias dentro da janela
    result = build_plantation_agronomy(late, days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.complete_days < 80
    assert result.accumulated_gdd < 16 * 80


def test_harvest_is_projected_from_recent_gdd_rate():
    days = make_days(40)  # 16 GDD/dia -> 640 acumulado, alvo 1500
    result = build_plantation_agronomy(plantation(), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.harvest is not None
    assert result.harvest.status == "PROJECTED"
    assert result.harvest.expected is not None
    assert result.harvest.earliest <= result.harvest.expected <= result.harvest.latest


def test_harvest_reached_when_target_met():
    days = make_days(120)  # 16 * 120 = 1920 > 1500
    result = build_plantation_agronomy(plantation(), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.harvest.status == "REACHED"
    assert result.harvest.reached_on is not None
    assert result.gdd_progress_percent == 100.0


def test_irrigation_estimate_uses_et0_and_kc():
    days = make_days(30)
    result = build_plantation_agronomy(plantation(), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    irrigation = result.irrigation
    assert irrigation is not None
    assert irrigation.et0_mm_day is not None and irrigation.et0_mm_day > 0
    assert irrigation.etc_mm_day == pytest.approx(irrigation.et0_mm_day * 0.8, abs=0.05)
    assert irrigation.crop_coefficient == 0.8
    assert irrigation.recent_days > 0
    assert irrigation.vpd_kpa is not None


def test_disease_risk_reflects_humid_hours():
    # Umidade alta (>=90) e temperatura amena (15-30) favorecem míldio.
    days = make_days(10, temperature=22, humidity=95)
    result = build_plantation_agronomy(plantation(), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.downy_mildew_risk == "HIGH"


def test_harvested_plantation_is_flagged():
    days = make_days(40)
    harvested = plantation(id=UUID_B, status="Colhida", harvestedAt="2026-02-01T00:00:00Z")
    result = build_plantation_agronomy(harvested, days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.status == "HARVESTED"
    assert result.message is not None


def test_summary_sorts_growing_first_and_reports_availability():
    days = make_days(50)
    plants = [plantation(id=UUID_B, variety="Uva Itália", status="Colhida", harvestedAt="2026-02-01T00:00:00Z"),
              plantation(id=UUID_A, variety="Crimson Seedless")]
    summary = build_agronomy_summary(plants, days, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert summary.available is True
    assert len(summary.plantations) == 2
    assert summary.plantations[0].status == "GROWING"
    assert summary.assumptions
    assert summary.latitude == -9.4 and summary.crop_coefficient == 0.8


def test_summary_without_plantations_is_unavailable():
    summary = build_agronomy_summary([], make_days(10), latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert summary.available is False
    assert summary.plantations == []
    assert summary.message is not None


def test_summary_without_complete_days_is_unavailable():
    days = make_days(10)
    for day in days:
        day.hourly = day.hourly[:5]  # todos incompletos
    summary = build_agronomy_summary([plantation()], days, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert summary.available is False


def test_plantation_without_planting_date_uses_full_history():
    days = make_days(30)
    result = build_plantation_agronomy(plantation(plantedAt=None), days, base_temp=10, upper_temp=35,
                                       target_gdd=1500, latitude=-9.4, kc=0.8, utc_offset_hours=-3)
    assert result.planted_at is None
    assert result.days_since_planting is None
    assert result.complete_days == 30
    assert result.message is not None


def test_et0_zero_amplitude_and_math_domain():
    # Amplitude negativa não deve quebrar a raiz quadrada.
    assert hargreaves_et0(30, 20, -9.4, 100) == 0.0
    assert math.isfinite(extraterrestrial_radiation(0.0, 80))
