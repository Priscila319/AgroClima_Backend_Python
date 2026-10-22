from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from app.cache import TTLCache, token_key
from app.clients.java_api import JavaApiClient
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
    MarketSummary,
    QualitySummary,
    SyncSummary,
    UserProfile,
)
from app.services.dashboard import DashboardService
from app.services.indicators import DEFAULT_BASE_TEMP, DEFAULT_TARGET_GDD, DEFAULT_UPPER_TEMP

logger = logging.getLogger("agroclima.api")
router = APIRouter(prefix="/api/v1", tags=["Análises e dashboards"])


@dataclass(frozen=True)
class RequestContext:
    user: UserProfile
    authorization: str


def _service(request: Request) -> DashboardService:
    return DashboardService(request.app.state.settings, request.app.state.http_client)


def _java(request: Request) -> JavaApiClient:
    return JavaApiClient(request.app.state.settings, request.app.state.http_client)


async def current_context(
    request: Request,
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
) -> RequestContext:
    """Valida o JWT na API Java: a autenticação e os papéis continuam sendo decididos pelo Java.

    O perfil validado fica em cache por poucos segundos (PROFILE_CACHE_TTL_SECONDS) para não chamar
    /api/auth/me em todo request. Só perfis ativos entram no cache; erros nunca são cacheados.
    """
    if not authorization or not authorization.lower().startswith("bearer ") or not authorization[7:].strip():
        raise ServiceError(401, "Autenticação Bearer obrigatória.")

    cache: TTLCache[UserProfile] | None = getattr(request.app.state, "profile_cache", None)
    key = token_key(authorization)
    profile = cache.get(key) if cache is not None else None

    if profile is None:
        profile_data = await _java(request).current_user(authorization)
        try:
            profile = UserProfile.model_validate(profile_data)
        except Exception as exception:
            logger.exception("Perfil de usuário inválido retornado pela API Java")
            raise ServiceError(502, "A API Java retornou um perfil de usuário inválido.") from exception
        if profile.status.upper() not in {"ACTIVE", "ATIVO"}:
            raise ServiceError(401, "A conta não está ativa.")
        if cache is not None:
            cache.set(key, profile)
    return RequestContext(user=profile, authorization=authorization)


def _require_role(context: RequestContext, allowed: set[str]) -> None:
    if context.user.role_key.upper() not in allowed:
        raise ServiceError(403, "Seu perfil não tem permissão para este recurso.")


@router.get("/climate/current", response_model=ClimateSummary)
async def current_climate(
    _: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ClimateSummary:
    return await _service(request).current_climate()


@router.get("/climate/history", response_model=ClimateSummary)
async def climate_history(
    _: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None, description="Data inicial ISO 8601 com fuso horário."),
    end: datetime | None = Query(default=None, description="Data final ISO 8601 com fuso horário."),
    resolution: Literal["auto", "raw", "hour", "day"] = Query(
        default="auto",
        description=(
            "Resolução da série. `auto`: até 48 h bruto, até 31 dias por hora, acima disso por dia. "
            "`raw` é recusado se o período tiver mais leituras que o limite configurado. "
            "As métricas (média, mínimo, máximo) sempre usam as leituras brutas."
        ),
    ),
) -> ClimateSummary:
    return await _service(request).climate_history(start, end, resolution)


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> DashboardSummary:
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/producer", response_model=DashboardSummary, deprecated=True)
async def producer_dashboard(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> DashboardSummary:
    """Alias de /dashboard/summary restrito a PRODUCER/ADMIN; use /dashboard/summary."""
    _require_role(context, {"PRODUCER", "ADMIN"})
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/analyst", response_model=AnalystDashboard)
async def analyst_dashboard(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> AnalystDashboard:
    _require_role(context, {"ANALYST", "ADMIN"})
    return await _service(request).analyst_dashboard(context.authorization, start, end)


@router.get("/dashboard/quality", response_model=QualitySummary)
async def climate_quality(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> QualitySummary:
    _require_role(context, {"ANALYST", "ADMIN"})
    return await _service(request).quality_summary(start, end)


@router.get("/dashboard/comparison", response_model=ComparisonSummary)
async def comparison_dashboard(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ComparisonSummary:
    _require_role(context, {"ANALYST", "ADMIN"})
    return await _service(request).comparison_summary(context.authorization)


@router.get("/dashboard/market", response_model=MarketSummary)
async def market_dashboard(_: Annotated[RequestContext, Depends(current_context)]) -> MarketSummary:
    return MarketSummary()


@router.get("/dashboard/forecast", response_model=ForecastSummary)
async def forecast_dashboard(
    _: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ForecastSummary:
    """Projeção estatística simples de temperatura e umidade para os próximos 3 dias."""
    return await _service(request).forecast()


@router.get("/dashboard/alerts", response_model=AlertsSummary)
async def alerts_dashboard(
    _: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    days: int = Query(default=7, ge=1, le=60, description="Quantos dias locais recentes avaliar."),
    window: int = Query(default=7, ge=2, le=30, description="Tamanho da média móvel, em dias."),
    z_threshold: float = Query(default=2.0, ge=1.0, le=5.0, description="Limite de z-score para anomalias."),
) -> AlertsSummary:
    """Médias móveis, anomalias, geada e estresse hídrico (aproximado por DPV)."""
    return await _service(request).alerts(days, window, z_threshold)


@router.get("/indicators/export", response_model=ExportIndicators)
async def export_indicators(
    _: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None, description="Início (ex.: data da poda), ISO 8601 com fuso."),
    end: datetime | None = Query(default=None, description="Fim, ISO 8601 com fuso. Sem datas: últimos 30 dias."),
    base_temp: float = Query(default=DEFAULT_BASE_TEMP, ge=0, le=20, description="Temperatura-base, em °C."),
    upper_temp: float = Query(default=DEFAULT_UPPER_TEMP, ge=20, le=50, description="Limite superior, em °C."),
    target_gdd: float = Query(default=DEFAULT_TARGET_GDD, gt=0, le=5000, description="Alvo de graus-dia."),
) -> ExportIndicators:
    """Graus-dia acumulados, risco de míldio/oídio e janela estimada de colheita."""
    return await _service(request).export_indicators(start, end, base_temp, upper_temp, target_gdd)


@router.get("/integrations/thingspeak/status", response_model=IntegrationStatus)
async def thingspeak_status(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> IntegrationStatus:
    """Status da integração ThingSpeak, lido do backend Java (ADMIN)."""
    _require_role(context, {"ADMIN"})
    return await _service(request).integration_status(context.authorization)


@router.post("/integrations/thingspeak/sync", response_model=SyncSummary)
async def synchronize_thingspeak(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(
        default=None, alias="from", description="Início opcional para reprocessar um período."),
    end: datetime | None = Query(
        default=None, alias="to", description="Fim opcional para reprocessar um período."),
) -> SyncSummary:
    """Dispara a sincronização no backend Java (ADMIN). O Python não acessa o ThingSpeak."""
    _require_role(context, {"ADMIN"})
    return await _service(request).sync_latest(context.authorization, start, end)
