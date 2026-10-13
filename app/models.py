from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


def to_camel(name: str) -> str:
    head, *tail = name.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="ignore")


class UserProfile(ApiModel):
    id: UUID
    name: str
    email: str
    role_key: str
    role: str
    status: str


class NormalizedReading(ApiModel):
    channel_id: str
    entry_id: int
    sensor_code: str
    captured_at: datetime
    measurements: dict[str, Decimal]
    quality_status: Literal["VALID", "REVIEW", "REJECTED"] = "VALID"


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


class ClimateSummary(ApiModel):
    available: bool
    source: str
    channel_id: str | None = None
    latest_at: datetime | None = None
    records: int = 0
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
