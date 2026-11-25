from __future__ import annotations

from app.services.export_market import obter_valor_exportacao_uva
from app.services.grape_market import listar_precos_uva, listar_valor_producao_uva

from dataclasses import dataclass
from datetime import datetime
import logging
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Header, Query, Request

from app.cache import TTLCache, token_key
from app.clients.java_api import JavaApiClient
from app.errors import ServiceError
from app.models import (
    AgronomySummary,
    AlertsSummary,
    AnalystDashboard,
    ClimateSummary,
    ComparisonSummary,
    DashboardSummary,
    ExportIndicators,
    ForecastSummary,
    IntegrationStatus,
    MarketSummary,
    ModelStatus,
    QualitySummary,
    SyncSummary,
    TrainRequest,
    TrainResult,
    UserProfile,
)
from app.services.dashboard import DashboardService
from app.services.indicators import DEFAULT_BASE_TEMP, DEFAULT_TARGET_GDD, DEFAULT_UPPER_TEMP
from app.services.models import ModelService

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


def _models(request: Request) -> ModelService:
    return ModelService(request.app.state.settings, request.app.state.http_client, request.app.state.model_registry)


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
    """Restrição por papel: reservada a operações administrativas (ex.: ThingSpeak, só ADMIN).

    Rotas com permissão própria no catálogo usam apenas _require_permission; não combine os dois.
    """
    if context.user.role_key.upper() not in allowed:
        raise ServiceError(403, "Seu perfil não tem permissão para este recurso.")


def _require_permission(context: RequestContext, permission: str) -> None:
    """RBAC: a mesma permissões do catálogo da API Java (role_permissions).

    O Java valida o JWT e devolve as permissões efetivas em /api/auth/me;
    aqui apenas consumimos esse resultado — não existe catálogo paralelo.
    """
    if permission not in context.user.permission_keys:
        raise ServiceError(403, "Seu perfil não tem permissão para este recurso.")


def _require_any_permission(context: RequestContext, permissions: set[str]) -> None:
    """RBAC: exige ao menos uma das permissões do catálogo (ex.: ler plantações próprias ou de todos)."""
    if not permissions.intersection(context.user.permission_keys):
        raise ServiceError(403, "Seu perfil não tem permissão para este recurso.")


@router.get("/climate/current", response_model=ClimateSummary)
async def current_climate(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ClimateSummary:
    _require_permission(context, "monitoring.view")
    return await _service(request).current_climate()


@router.get("/climate/history", response_model=ClimateSummary)
async def climate_history(
    context: Annotated[RequestContext, Depends(current_context)],
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
    _require_permission(context, "monitoring.view")
    return await _service(request).climate_history(start, end, resolution)


@router.get("/dashboard/summary", response_model=DashboardSummary)
async def dashboard_summary(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> DashboardSummary:
    _require_permission(context, "overview.view")
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/producer", response_model=DashboardSummary, deprecated=True)
async def producer_dashboard(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> DashboardSummary:
    """Alias de /dashboard/summary; use /dashboard/summary. O acesso é definido pela permissão, não pelo papel."""
    _require_permission(context, "overview.view")
    return await _service(request).dashboard_summary(context.authorization)


@router.get("/dashboard/analyst", response_model=AnalystDashboard)
async def analyst_dashboard(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> AnalystDashboard:
    _require_permission(context, "analytics.view")
    return await _service(request).analyst_dashboard(context.authorization, start, end)


@router.get("/dashboard/quality", response_model=QualitySummary)
async def climate_quality(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None),
    end: datetime | None = Query(default=None),
) -> QualitySummary:
    _require_permission(context, "analytics.view")
    return await _service(request).quality_summary(start, end)


@router.get("/dashboard/comparison", response_model=ComparisonSummary)
async def comparison_dashboard(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ComparisonSummary:
    _require_permission(context, "analytics.view")
    return await _service(request).comparison_summary(context.authorization)


@router.get("/dashboard/market", response_model=MarketSummary)
async def market_dashboard(context: Annotated[RequestContext, Depends(current_context)]) -> MarketSummary:
    _require_permission(context, "analytics.view")
    return MarketSummary()


@router.get("/dashboard/forecast", response_model=ForecastSummary)
async def forecast_dashboard(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
    horizon_days: int = Query(default=3, ge=1, le=7, description="Dias à frente para projetar."),
) -> ForecastSummary:
    """Projeção de temperatura e umidade: modelo ML ativo quando existe, senão o estatístico (Holt-Winters)."""
    _require_permission(context, "analytics.view")
    return await _models(request).forecast(horizon_days)


@router.get("/dashboard/alerts", response_model=AlertsSummary)
async def alerts_dashboard(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    days: int = Query(default=7, ge=1, le=60, description="Quantos dias locais recentes avaliar."),
    window: int = Query(default=7, ge=2, le=30, description="Tamanho da média móvel, em dias."),
    z_threshold: float = Query(default=2.0, ge=1.0, le=5.0, description="Limite de z-score para anomalias."),
) -> AlertsSummary:
    """Médias móveis, anomalias, geada e estresse hídrico (aproximado por DPV)."""
    _require_permission(context, "monitoring.view")
    return await _service(request).alerts(days, window, z_threshold)


@router.get("/indicators/export", response_model=ExportIndicators)
async def export_indicators(
    context: Annotated[RequestContext, Depends(current_context)],
    request: Request,
    start: datetime | None = Query(default=None, description="Início (ex.: data da poda), ISO 8601 com fuso."),
    end: datetime | None = Query(default=None, description="Fim, ISO 8601 com fuso. Sem datas: últimos 30 dias."),
    base_temp: float = Query(default=DEFAULT_BASE_TEMP, ge=0, le=20, description="Temperatura-base, em °C."),
    upper_temp: float = Query(default=DEFAULT_UPPER_TEMP, ge=20, le=50, description="Limite superior, em °C."),
    target_gdd: float = Query(default=DEFAULT_TARGET_GDD, gt=0, le=5000, description="Alvo de graus-dia."),
) -> ExportIndicators:
    """Graus-dia acumulados, risco de míldio/oídio e janela estimada de colheita."""
    _require_permission(context, "reports.view")
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


@router.get("/models/status", response_model=ModelStatus)
async def models_status(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ModelStatus:
    """Estado dos modelos: disponibilidade estatística, histórico completo e modelo ML ativo."""
    _require_permission(context, "analytics.view")
    return await _models(request).status()


@router.post("/models/train", response_model=TrainResult,
              responses={409: {"description": "Histórico insuficiente para treinar."}})
async def models_train(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
    body: TrainRequest | None = None,
) -> TrainResult:
    """Treina um modelo de machine learning no histórico real e o ativa. Exige models.manage."""
    _require_permission(context, "models.manage")
    return await _models(request).train(body or TrainRequest(), context.authorization)


@router.get("/models/forecast", response_model=ForecastSummary)
async def models_forecast(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
    horizon_days: int = Query(default=3, ge=1, le=7, description="Dias à frente para projetar."),
) -> ForecastSummary:
    """Projeção com o modelo ativo (ML treinado ou estatístico), com métricas de backtest."""
    _require_permission(context, "analytics.view")
    return await _models(request).forecast(horizon_days)


@router.delete("/models/active", response_model=ModelStatus)
async def models_delete_active(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
) -> ModelStatus:
    """Remove o modelo ML ativo; a projeção volta ao modelo estatístico. Exige models.manage."""
    _require_permission(context, "models.manage")
    return await _models(request).delete_active(context.authorization)


@router.get("/agronomy/plantations", response_model=AgronomySummary)
async def agronomy_plantations(
    context: Annotated[RequestContext, Depends(current_context)], request: Request,
    base_temp: float = Query(default=DEFAULT_BASE_TEMP, ge=0, le=20, description="Temperatura-base, em °C."),
    upper_temp: float = Query(default=DEFAULT_UPPER_TEMP, ge=20, le=50, description="Limite superior, em °C."),
    target_gdd: float = Query(default=DEFAULT_TARGET_GDD, gt=0, le=5000, description="Alvo de graus-dia por ciclo."),
) -> AgronomySummary:
    """GDD/colheita, risco de doença e necessidade hídrica por plantação real (escopo definido pelo Java)."""
    _require_any_permission(context, {"plantations.manage", "plantations.read.all"})
    return await _models(request).agronomy(
        context.authorization, base_temp=base_temp, upper_temp=upper_temp, target_gdd=target_gdd,
    )

@router.get("/market/grape-prices")
async def get_grape_prices(
    context: Annotated[RequestContext, Depends(current_context)],
    ano_safra: str | None = Query(default=None, description="Ex.: 2026"),
    variedade_grupo: str | None = Query(default=None, description="Ex.: Isabel ou Viniferas"),
    regiao_praca: str | None = Query(default=None, description="Ex.: Bento Goncalves ou Sul"),
) -> dict[str, Any]:
    """Retorna os preços de uva para vinho a partir do CSV em backend-python/data."""
    _require_permission(context, "analytics.view")
    try:
        itens = listar_precos_uva(
            ano_safra=ano_safra,
            variedade_grupo=variedade_grupo,
            regiao_praca=regiao_praca,
        )
    except FileNotFoundError as exc:
        raise ServiceError(404, str(exc)) from exc

    return {
        "total": len(itens),
        "items": itens,
    }

@router.get("/market/grape-production-value")
async def get_grape_production_value(
    context: Annotated[RequestContext, Depends(current_context)],
    localidade: str | None = Query(default=None, description="Ex.: Pernambuco ou Bahia"),
    unidade: str | None = Query(default=None, description="Ex.: Mil Reais"),
) -> dict[str, Any]:
    """Retorna o valor da produção de uva (2025) a partir do CSV em backend-python/data."""
    _require_permission(context, "analytics.view")
    try:
        itens = listar_valor_producao_uva(
            localidade=localidade,
            unidade=unidade,
        )
    except FileNotFoundError as exc:
        raise ServiceError(404, str(exc)) from exc

    return {
        "total": len(itens),
        "items": itens,
    }

@router.get("/market/export-value")
async def get_export_value(
    context: Annotated[RequestContext, Depends(current_context)],
    ano: int | None = Query(
        default=None,
        description="Ano da exportação (ex: 2025). Padrão = ano atual",
    ),
) -> dict[str, Any]:
    """
    Retorna o valor de exportação de uvas frescas (NCM 08061000)
    com base na API oficial do ComexStat (MDIC).
    """
    _require_permission(context, "analytics.view")
    try:
        return await obter_valor_exportacao_uva(ano=ano)
    except Exception as exc:
        raise ServiceError(502, f"Erro ao consultar ComexStat: {exc}") from exc
