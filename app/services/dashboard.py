from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

import httpx

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError
from app.models import (
    AlertsSummary,
    AnalystDashboard,
    ClimateSummary,
    ComparisonSummary,
    DashboardSummary,
    ExportIndicators,
    ForecastSummary,
    IntegrationStatus,
    PlantationByVariety,
    PlantationStatus,
    PlantationSummary,
    QualitySummary,
    SourceAvailability,
    SyncSummary,
)
from app.services.alerts import ANOMALY_BASELINE_DAYS, FORECAST_LOOKBACK_DAYS, build_alerts, build_forecast
from app.services.analysis import summarize_climate, summarize_quality
from app.services.daily import build_days, local_midnight_utc, local_zone
from app.services.indicators import build_export_indicators

HistoryResolution = Literal["auto", "raw", "hour", "day"]
EXPECTED_METRICS = {"temperature", "humidity"}
# A API Java devolve no máximo 5000 leituras por consulta (as mais antigas do período).
JAVA_READINGS_LIMIT = 5000
COMPARISON_LIMITATION = (
    "As leituras climáticas são agregadas por canal. Não há vínculo entre sensor e variedade no cadastro "
    "atual; não são inferidos rendimentos por uva."
)


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


def classify_plantation(plantation: dict[str, Any]) -> PlantationStatus:
    """Normaliza o estado de uma plantação do Java no enum PlantationStatus (arquivada > colhida > ativa)."""
    status = str(plantation.get("status", "")).casefold()
    if plantation.get("archivedAt") or "arquiv" in status:
        return PlantationStatus.ARCHIVED
    if plantation.get("harvestedAt") or plantation.get("harvestDate") or "colhid" in status or "harvest" in status:
        return PlantationStatus.HARVESTED
    return PlantationStatus.GROWING


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

    async def integration_status(self, authorization: str) -> IntegrationStatus:
        return IntegrationStatus.model_validate(await self.java.thingspeak_status(authorization))

    async def current_climate(self) -> ClimateSummary:
        """Últimas 24 h de leituras (janela fixa)."""
        end = datetime.now(UTC)
        summary, _ = await self.climate_summary(end - timedelta(hours=24), end)
        return summary

    async def climate_summary(self, start: datetime, end: datetime,
                              resolution: Literal["raw", "hour", "day"] = "raw",
                              ) -> tuple[ClimateSummary, list[dict[str, Any]]]:
        self._validate_range(start, end)
        readings = await self.java.climate_readings(start, end)
        return self._summarize(readings, resolution), readings

    async def climate_history(self, start: datetime | None, end: datetime | None,
                              resolution: HistoryResolution = "auto") -> ClimateSummary:
        """Histórico com a série agregada por hora/dia para não devolver até 366 dias de leituras brutas."""
        start, end = resolve_window(start, end)
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

    def _summarize(self, readings: list[dict[str, Any]], resolution: Literal["raw", "hour", "day"]) -> ClimateSummary:
        channel_ids = {str(reading.get("channelId")) for reading in readings if reading.get("channelId")}
        channel_id = channel_ids.pop() if len(channel_ids) == 1 else None
        return summarize_climate(readings, channel_id=channel_id, source=self.settings.climate_source_name,
                                 resolution=resolution)

    async def plantation_summary(self, authorization: str) -> tuple[PlantationSummary, list[dict[str, Any]]]:
        plantations = await self.java.plantations(authorization)
        grouped: dict[str, dict[str, int]] = {}
        total_plants = growing = harvested = archived = 0
        for plantation in plantations:
            variety = str(plantation.get("variety") or "Não informado")
            quantity = self._int_value(plantation.get("quantity"))
            status = classify_plantation(plantation)
            total_plants += quantity
            bucket = grouped.setdefault(variety, {"plantations": 0, "plants": 0, "growing": 0, "harvested": 0})
            bucket["plantations"] += 1
            bucket["plants"] += quantity
            if status is PlantationStatus.ARCHIVED:
                archived += 1
            elif status is PlantationStatus.HARVESTED:
                harvested += 1
                bucket["harvested"] += 1
            else:
                growing += 1
                bucket["growing"] += 1

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
        comparison = self._build_comparison(climate, plantations)
        return AnalystDashboard(summary=summary, quality=quality, comparison=comparison,
                                generated_at=datetime.now(UTC))

    async def comparison_summary(self, authorization: str) -> ComparisonSummary:
        summary = await self.dashboard_summary(authorization)
        return self._build_comparison(summary.climate, summary.plantations)

    async def quality_summary(self, start: datetime | None, end: datetime | None) -> QualitySummary:
        start, end = resolve_window(start, end)
        _, readings = await self.climate_summary(start, end)
        return QualitySummary.model_validate(summarize_quality(readings, expected_metrics=EXPECTED_METRICS))

    async def export_indicators(self, start: datetime | None, end: datetime | None, base_temp: float,
                                upper_temp: float, target_gdd: float) -> ExportIndicators:
        """Graus-dia, risco de míldio/oídio e janela de colheita. Sem datas, usa os últimos 30 dias locais."""
        if start is None and end is None:
            end = datetime.now(UTC)
            start = local_midnight_utc(end, 29, self.settings.climate_utc_offset_hours)
        elif start is None or end is None:
            raise ServiceError(400, "Informe as duas datas: start e end.")
        if upper_temp <= base_temp:
            raise ServiceError(400, "upper_temp deve ser maior que base_temp.")
        self._validate_range(start, end)
        readings = await self._readings_without_truncation(start, end)
        days, rejected = build_days(readings, self.settings.climate_utc_offset_hours)
        return build_export_indicators(days, rejected=rejected, base_temp=base_temp, upper_temp=upper_temp,
                                       target_gdd=target_gdd)

    async def alerts(self, days: int, average_window: int, z_threshold: float) -> AlertsSummary:
        """Alertas dos últimos `days` dias locais. Busca dias extras antes do período como base das anomalias."""
        offset = self.settings.climate_utc_offset_hours
        end = datetime.now(UTC)
        first_day = (end.astimezone(local_zone(offset)) - timedelta(days=days - 1)).date()
        start = local_midnight_utc(end, days - 1 + ANOMALY_BASELINE_DAYS, offset)
        readings = await self._readings_without_truncation(start, end)
        series, _ = build_days(readings, offset)
        return build_alerts(series, window_days=days, average_window=average_window, z_threshold=z_threshold,
                            first_day=first_day)

    async def forecast(self) -> ForecastSummary:
        """Projeção de temperatura e umidade para os próximos dias, a partir dos últimos dias completos."""
        end = datetime.now(UTC)
        start = local_midnight_utc(end, FORECAST_LOOKBACK_DAYS, self.settings.climate_utc_offset_hours)
        readings = await self._readings_without_truncation(start, end)
        days, _ = build_days(readings, self.settings.climate_utc_offset_hours)
        return build_forecast(days)

    async def _readings_without_truncation(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Lê o período inteiro mesmo quando o Java corta em 5000 leituras: divide o intervalo ao meio e junta.

        Só divide quando a resposta atinge o limite; períodos curtos continuam em uma única chamada.
        """
        readings = await self.java.climate_readings(start, end)
        if len(readings) < JAVA_READINGS_LIMIT or end - start <= timedelta(hours=1):
            return readings
        middle = start + (end - start) / 2
        middle = middle.replace(microsecond=0)
        first, second = await asyncio.gather(
            self._readings_without_truncation(start, middle),
            self._readings_without_truncation(middle, end),
        )
        unique: dict[Any, dict[str, Any]] = {}
        for reading in (*first, *second):  # o Java inclui as duas pontas: a leitura do meio pode vir duas vezes
            unique.setdefault((reading.get("channelId"), reading.get("entryId"), reading.get("capturedAt")), reading)
        return list(unique.values())

    @staticmethod
    def _build_comparison(climate: ClimateSummary, plantations: PlantationSummary) -> ComparisonSummary:
        return ComparisonSummary(
            available=climate.available or plantations.total_plantations > 0,
            climate_metrics=climate.metrics,
            plantations_by_variety=plantations.by_variety,
            limitation=COMPARISON_LIMITATION,
        )

    def _build_dashboard(self, climate: ClimateSummary, plantations: PlantationSummary) -> DashboardSummary:
        sources = [
            SourceAvailability(source=climate.source, available=climate.available,
                               configured=self.settings.internal_key_configured,
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

