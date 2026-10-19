from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError
from app.models import (
    AnalystDashboard,
    ClimateSummary,
    ComparisonSummary,
    DashboardSummary,
    PlantationByVariety,
    PlantationSummary,
    QualitySummary,
    SourceAvailability,
    SyncSummary,
)
from app.services.analysis import summarize_climate, summarize_quality

HistoryResolution = Literal["auto", "raw", "hour", "day"]
EXPECTED_METRICS = {"temperature", "humidity"}


def resolve_window(start: datetime | None, end: datetime | None) -> tuple[datetime, datetime]:
    """Sem datas = últimas 24 h; informar só uma das duas é erro."""
    if start is None and end is None:
        end = datetime.now(UTC)
        return end - timedelta(hours=24), end
    if start is None or end is None:
        raise ServiceError(400, "Informe as duas datas: start e end.")
    return start, end


def pick_resolution(start: datetime, end: datetime) -> Literal["raw", "hour", "day"]:
    """Escolha automática: até 48 h bruto, até 31 dias por hora, acima disso por dia."""
    span = end - start
    if span <= timedelta(hours=48):
        return "raw"
    if span <= timedelta(days=31):
        return "hour"
    return "day"


class DashboardService:
    """Monta indicadores a partir dos dados persistidos pela API Java.

    Não há acesso ao ThingSpeak neste serviço: a coleta e a persistência ficam no backend Java.
    """

    def __init__(self, settings: Settings, client: httpx.AsyncClient):
        self.settings = settings
        self.client = client
        self.java = JavaApiClient(settings, client)

    async def sync_latest(self, authorization: str, start: datetime | None = None,
                          end: datetime | None = None) -> SyncSummary:
        """Aciona a sincronização do ThingSpeak no backend Java (apenas ADMIN; a regra é aplicada pelo Java)."""
        if (start is None) != (end is None):
            raise ServiceError(400, "Informe as duas datas: from e to.")
        if start and end:
            self._validate_range(start, end)
        result = await self.java.thingspeak_sync(authorization, start, end)
        return SyncSummary.model_validate(result)

    async def integration_status(self, authorization: str) -> dict[str, Any]:
        return await self.java.thingspeak_status(authorization)

    async def climate_summary(self, start: datetime, end: datetime,
                              resolution: Literal["raw", "hour", "day"] = "raw",
                              ) -> tuple[ClimateSummary, list[dict[str, Any]]]:
        self._validate_range(start, end)
        readings = await self.java.climate_readings(start, end)
        return self._summarize(readings, resolution), readings

    async def climate_history(self, start: datetime, end: datetime,
                              resolution: HistoryResolution = "auto") -> ClimateSummary:
        """Histórico com a série agregada por hora/dia para não devolver até 366 dias de leituras brutas."""
        self._validate_range(start, end)
        chosen = pick_resolution(start, end) if resolution == "auto" else resolution
        readings = await self.java.climate_readings(start, end)
        limit = self.settings.history_max_raw_points
        if chosen == "raw" and len(readings) > limit:
            raise ServiceError(
                400,
                f"O período tem {len(readings)} leituras (limite bruto: {limit}). "
                "Use resolution=hour ou resolution=day, ou reduza o intervalo.",
            )
        return self._summarize(readings, chosen)

    @staticmethod
    def _summarize(readings: list[dict[str, Any]], resolution: Literal["raw", "hour", "day"]) -> ClimateSummary:
        channel_ids = {str(reading.get("channelId")) for reading in readings if reading.get("channelId")}
        channel_id = channel_ids.pop() if len(channel_ids) == 1 else None
        return summarize_climate(readings, channel_id=channel_id, source="ThingSpeak", resolution=resolution)

    async def plantation_summary(self, authorization: str) -> tuple[PlantationSummary, list[dict[str, Any]]]:
        plantations = await self.java.plantations(authorization)
        grouped: dict[str, dict[str, int]] = {}
        total_plants = growing = harvested = archived = 0
        for plantation in plantations:
            variety = str(plantation.get("variety") or "Não informado")
            quantity = self._int_value(plantation.get("quantity"))
            status = str(plantation.get("status", "")).casefold()
            is_archived = bool(plantation.get("archivedAt")) or "arquiv" in status
            is_harvested = bool(plantation.get("harvestedAt") or plantation.get("harvestDate")) or "colhid" in status or "harvest" in status
            total_plants += quantity
            if is_archived:
                archived += 1
            elif is_harvested:
                harvested += 1
            else:
                growing += 1
            bucket = grouped.setdefault(variety, {"plantations": 0, "plants": 0, "growing": 0, "harvested": 0})
            bucket["plantations"] += 1
            bucket["plants"] += quantity
            if not is_archived:
                bucket["harvested" if is_harvested else "growing"] += 1

        by_variety = [PlantationByVariety(variety=name, **values) for name, values in sorted(grouped.items())]
        result = PlantationSummary(total_plantations=len(plantations), growing=growing, harvested=harvested,
                                   archived=archived, total_plants=total_plants, by_variety=by_variety)
        return result, plantations

    async def dashboard_summary(self, authorization: str) -> DashboardSummary:
        now = datetime.now(UTC)
        (climate, _), (plantations, _) = await asyncio.gather(
            self.climate_summary(now - timedelta(hours=24), now),
            self.plantation_summary(authorization),
        )
        return self._build_dashboard(climate, plantations)

    async def analyst_dashboard(self, authorization: str, start: datetime | None,
                                end: datetime | None) -> AnalystDashboard:
        """Painel do analista. Cada fonte é consultada no Java uma única vez.

        Sem datas, a janela de qualidade é a mesma das últimas 24 h do resumo: as leituras
        buscadas uma vez servem aos dois. Com datas personalizadas são duas janelas distintas,
        buscadas em paralelo.
        """
        default_window = start is None and end is None
        start, end = resolve_window(start, end)
        now = datetime.now(UTC)
        day_start = now - timedelta(hours=24)

        if default_window:
            (climate, readings), (plantations, _) = await asyncio.gather(
                self.climate_summary(day_start, now),
                self.plantation_summary(authorization),
            )
        else:
            (climate, _), (_, readings), (plantations, _) = await asyncio.gather(
                self.climate_summary(day_start, now),
                self.climate_summary(start, end),
                self.plantation_summary(authorization),
            )

        summary = self._build_dashboard(climate, plantations)
        quality = QualitySummary.model_validate(summarize_quality(readings, expected_metrics=EXPECTED_METRICS))
        comparison = ComparisonSummary(
            available=climate.available or plantations.total_plantations > 0,
            climate_metrics=climate.metrics,
            plantations_by_variety=plantations.by_variety,
            limitation="As leituras climáticas são agregadas por canal. Não há vínculo entre sensor e variedade no cadastro atual.",
        )
        return AnalystDashboard(summary=summary, quality=quality, comparison=comparison,
                                generated_at=datetime.now(UTC))

    async def quality_summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        _, readings = await self.climate_summary(start, end)
        return summarize_quality(readings, expected_metrics=EXPECTED_METRICS)

    @staticmethod
    def _build_dashboard(climate: ClimateSummary, plantations: PlantationSummary) -> DashboardSummary:
        sources = [
            SourceAvailability(source="ThingSpeak", available=climate.available, configured=climate.available,
                               message=climate.message or "Leituras de temperatura e umidade recebidas pela API Java."),
            SourceAvailability(source="API Java", available=True, configured=True,
                               message="Autenticação, plantações e persistência servidas pela API Java."),
        ]
        return DashboardSummary(climate=climate, plantations=plantations, sources=sources)

    @staticmethod
    def _validate_range(start: datetime, end: datetime) -> None:
        if start.tzinfo is None or end.tzinfo is None:
            raise ServiceError(400, "As datas devem incluir fuso horário, por exemplo ISO 8601 com Z.")
        if start >= end:
            raise ServiceError(400, "A data inicial deve ser anterior à data final.")
        if end - start > timedelta(days=366):
            raise ServiceError(400, "O intervalo histórico não pode exceder 366 dias.")

    @staticmethod
    def _int_value(value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError, OverflowError):
            return 0

