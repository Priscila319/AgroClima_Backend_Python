from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from app.cache import TTLCache, token_key
from app.clients.java_api import JavaApiClient
from app.errors import ServiceError
from app.models import (
    AnalystDashboard,
    ClimateSummary,
    ComparisonSummary,
    ForecastSummary,
    MarketSummary,
    QualitySummary,
    SyncSummary,
    UserProfile,
)
from app.services.dashboard import DashboardService, resolve_window

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
async def current_climate(context: Annotated[RequestContext, Depends(current_context)], request: Request):
    end = datetime.now(UTC)
    start = end - timedelta(hours=24)
    summary, _ = await _service(request).climate_summary(start, end)
    return summary


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
):
    start, end = resolve_window(start, end)
    return await _service(request).climate_history(start, end, resolution)


@router.get("/dashboard/summary")
async def dashboard_summary(context: Annotated[RequestContext, Depends(current_context)], request: Request):
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/producer")
async def producer_dashboard(context: Annotated[RequestContext, Depends(current_context)], request: Request):
    _require_role(context, {"PRODUCER", "ADMIN"})
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/analyst", response_model=AnalystDashboard)
async def analyst_dashboard(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
):
    _require_role(context, {"ANALYST", "ADMIN"})
    return await _service(request).analyst_dashboard(context.authorization, start, end)


@router.get("/dashboard/quality", response_model=QualitySummary)
async def climate_quality(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
):
    _require_role(context, {"ANALYST", "ADMIN"})
    start, end = resolve_window(start, end)
    result = await _service(request).quality_summary(start, end)
    return QualitySummary.model_validate(result)


@router.get("/dashboard/comparison", response_model=ComparisonSummary)
async def comparison_dashboard(context: Annotated[RequestContext, Depends(current_context)], request: Request):
    _require_role(context, {"ANALYST", "ADMIN"})
    summary = await _service(request).dashboard_summary(context.authorization)
    return ComparisonSummary(
        available=summary.climate.available or summary.plantations.total_plantations > 0,
        climate_metrics=summary.climate.metrics,
        plantations_by_variety=summary.plantations.by_variety,
        limitation="Não existe associação entre leituras de canal e variedade; não são inferidos rendimentos por uva.",
    )


@router.get("/dashboard/market", response_model=MarketSummary)
async def market_dashboard(_: Annotated[RequestContext, Depends(current_context)]):
    return MarketSummary()


@router.get("/dashboard/forecast", response_model=ForecastSummary)
async def forecast_dashboard(_: Annotated[RequestContext, Depends(current_context)]):
    return ForecastSummary()


@router.get("/integrations/thingspeak/status")
async def thingspeak_status(context: Annotated[RequestContext, Depends(current_context)], request: Request):
    """Status da integração ThingSpeak, lido do backend Java (ADMIN)."""
    _require_role(context, {"ADMIN"})
    return await _service(request).integration_status(context.authorization)


@router.post("/integrations/thingspeak/sync", response_model=SyncSummary)
async def synchronize_thingspeak(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None, alias="from", description="Início opcional para reprocessar um período."),
    end: datetime | None = Query(default=None, alias="to", description="Fim opcional para reprocessar um período."),
):
    """Dispara a sincronização no backend Java (ADMIN). O Python não acessa o ThingSpeak."""
    _require_role(context, {"ADMIN"})
    return await _service(request).sync_latest(context.authorization, start, end)
