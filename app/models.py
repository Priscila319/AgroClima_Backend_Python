from __future__ import annotations

from datetime import date as DateValue
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class PlantationStatus(StrEnum):
    """Estado normalizado de uma plantação (o Java pode devolver textos como "Ativa" ou "Colhida")."""

    GROWING = "GROWING"
    HARVESTED = "HARVESTED"
    ARCHIVED = "ARCHIVED"


class UserProfile(ApiModel):
    id: UUID
    name: str
    email: str
    role_key: str
    role: str
    status: str


class MetricSummary(ApiModel):
    current: float | None = None
    average: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    unit: str = ""
    samples: int = 0


class ClimatePoint(ApiModel):
    timestamp: datetime
    values: dict[str, float | None]
    quality_status: Literal["VALID", "REVIEW", "REJECTED"] = "VALID"


Resolution = Literal["raw", "hour", "day"]


class ClimateSummary(ApiModel):
    available: bool
    source: str
    channel_id: str | None = None
    latest_at: datetime | None = None
    records: int = 0
    # Resolução da `series`. As `metrics` são sempre calculadas sobre as leituras brutas.
    resolution: Resolution = "raw"
    metrics: dict[str, MetricSummary] = Field(default_factory=dict)
    series: list[ClimatePoint] = Field(default_factory=list)
    rejected_records: int = 0
    message: str | None = None


class PlantationByVariety(ApiModel):
    variety: str
    plantations: int = 0
    plants: int = 0
    growing: int = 0
    harvested: int = 0


class PlantationSummary(ApiModel):
    total_plantations: int = 0
    growing: int = 0
    harvested: int = 0
    archived: int = 0
    total_plants: int = 0
    by_variety: list[PlantationByVariety] = Field(default_factory=list)


class QualitySummary(ApiModel):
    available: bool
    total_records: int = 0
    valid_records: int = 0
    review_records: int = 0
    rejected_records: int = 0
    completeness_percent: float | None = None


class SourceAvailability(ApiModel):
    source: str
    available: bool
    configured: bool
    message: str


class DashboardSummary(ApiModel):
    climate: ClimateSummary
    plantations: PlantationSummary
    sources: list[SourceAvailability]


class ComparisonSummary(ApiModel):
    available: bool
    climate_metrics: dict[str, MetricSummary] = Field(default_factory=dict)
    plantations_by_variety: list[PlantationByVariety] = Field(default_factory=list)
    limitation: str


class AnalystDashboard(ApiModel):
    summary: DashboardSummary
    quality: QualitySummary
    comparison: ComparisonSummary
    generated_at: datetime


class MarketSummary(ApiModel):
    available: bool = False
    source: str | None = None
    message: str = "Nenhuma fonte de mercado está configurada; a API não retorna valores demonstrativos."
    series: list[dict[str, float | str]] = Field(default_factory=list)


class ForecastSummary(ApiModel):
    available: bool = False
    model: str | None = None
    message: str = "Nenhum modelo preditivo foi configurado para produção."
    predictions: list[dict[str, float | str]] = Field(default_factory=list)


class SyncSummary(ApiModel):
    configured: bool
    channel_id: str | None = None
    received: int = 0
    normalized: int = 0
    rejected: int = 0
    inserted: int = 0
    already_present: int = 0
    synchronized_at: datetime | None = None
    message: str


class IntegrationStatus(ApiModel):
    """Status do ThingSpeak vindo do Java. Campos extras são mantidos: o Java é a fonte do contrato."""

    model_config = ConfigDict(extra="allow")

    configured: bool | None = None
    channel_id: str | None = None
    message: str | None = None


class HealthResponse(ApiModel):
    status: Literal["UP"] = "UP"
    service: str
    java_api_configured: bool
    internal_key_configured: bool


class ReadyResponse(ApiModel):
    status: Literal["READY", "NOT_READY"]
    service: str
    checks: dict[str, bool]
    message: str | None = None


RiskLevel = Literal["LOW", "MODERATE", "HIGH", "INSUFFICIENT_DATA"]


class DailyIndicator(ApiModel):
    date: DateValue
    complete: bool
    hours_with_data: int
    temperature_min: float | None = None
    temperature_mean: float | None = None
    temperature_max: float | None = None
    humidity_mean: float | None = None
    gdd: float | None = None
    gdd_accumulated: float | None = None
    downy_mildew_hours: int | None = None
    downy_mildew_risk: RiskLevel = "INSUFFICIENT_DATA"
    powdery_mildew_hours: int | None = None
    powdery_mildew_risk: RiskLevel = "INSUFFICIENT_DATA"


class HarvestWindow(ApiModel):
    status: Literal["REACHED", "PROJECTED", "BEYOND_HORIZON", "INSUFFICIENT_DATA"]
    target_gdd: float
    accumulated_gdd: float
    remaining_gdd: float
    reached_on: DateValue | None = None
    earliest: DateValue | None = None
    expected: DateValue | None = None
    latest: DateValue | None = None
    recent_daily_gdd: float | None = None
    recent_high_risk_days: int = 0
    message: str


class ExportIndicators(ApiModel):
    available: bool
    period_start: DateValue | None = None
    period_end: DateValue | None = None
    base_temp: float
    upper_temp: float
    target_gdd: float
    complete_days: int = 0
    incomplete_days: int = 0
    rejected_records: int = 0
    gdd_accumulated: float = 0.0
    current_downy_mildew_risk: RiskLevel = "INSUFFICIENT_DATA"
    current_powdery_mildew_risk: RiskLevel = "INSUFFICIENT_DATA"
    harvest_window: HarvestWindow | None = None
    days: list[DailyIndicator] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    message: str | None = None


class DailyTrend(ApiModel):
    date: DateValue
    temperature: float | None = None
    humidity: float | None = None
    temperature_moving_average: float | None = None
    humidity_moving_average: float | None = None
    temperature_zscore: float | None = None
    humidity_zscore: float | None = None


class Alert(ApiModel):
    type: Literal["FROST", "WATER_STRESS", "TEMPERATURE_ANOMALY", "HUMIDITY_ANOMALY"]
    severity: Literal["INFO", "WARNING", "CRITICAL"]
    date: DateValue
    metric: str
    value: float
    threshold: float
    message: str


class AlertsSummary(ApiModel):
    available: bool
    window_days: int
    moving_average_window: int
    z_threshold: float
    complete_days: int = 0
    series: list[DailyTrend] = Field(default_factory=list)
    alerts: list[Alert] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    message: str | None = None
