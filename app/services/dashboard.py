from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError
from app.models import (
    ClimateSummary,
    DashboardSummary,
    PlantationByVariety,
    PlantationSummary,
    SourceAvailability,
    SyncSummary,
)
from app.services.analysis import summarize_climate, summarize_quality


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

    async def climate_summary(self, start: datetime, end: datetime) -> tuple[ClimateSummary, list[dict[str, Any]]]:
        self._validate_range(start, end)
        readings = await self.java.climate_readings(start, end)
        channel_ids = {str(reading.get("channelId")) for reading in readings if reading.get("channelId")}
        channel_id = channel_ids.pop() if len(channel_ids) == 1 else None
        summary = summarize_climate(readings, channel_id=channel_id, source="ThingSpeak")
        return summary, readings

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
        climate_task = self.climate_summary(now - timedelta(hours=24), now)
        plantations_task = self.plantation_summary(authorization)
        (climate, _), (plantations, _) = await _gather(climate_task, plantations_task)
        sources = [
            SourceAvailability(source="ThingSpeak", available=climate.available, configured=climate.available,
                               message=climate.message or "Leituras de temperatura e umidade recebidas pela API Java."),
            SourceAvailability(source="API Java", available=True, configured=True,
                               message="Autenticação, plantações e persistência servidas pela API Java."),
        ]
        return DashboardSummary(climate=climate, plantations=plantations, sources=sources)

    async def quality_summary(self, start: datetime, end: datetime) -> dict[str, Any]:
        _, readings = await self.climate_summary(start, end)
        return summarize_quality(readings, expected_metrics={"temperature", "humidity"})

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


async def _gather(first, second):
    results = await asyncio.gather(first, second, return_exceptions=True)
    for result in results:
        if isinstance(result, Exception):
            raise result
    return results[0], results[1]
