"""Orquestra os modelos preditivos: estatístico, machine learning e agronômico.

Carrega o histórico real pela API Java (reaproveitando o ``DashboardService``), decide qual
modelo usar (ML ativo > estatístico), persiste o modelo treinado no registro em disco e
registra auditoria das operações de escrita. Nenhuma decisão de RBAC acontece aqui: as rotas
validam as permissões do catálogo Java antes de chamar este serviço.
"""

from __future__ import annotations

import logging

import httpx

from app.clients.java_api import JavaApiClient
from app.config import Settings
from app.errors import ServiceError
from app.models import (
    AgronomySummary,
    ForecastSummary,
    ModelStatus,
    TrainRequest,
    TrainResult,
)
from app.services.agronomy import build_agronomy_summary
from app.services.dashboard import DashboardService
from app.services.forecasting import STAT_MAX_LOOKBACK_DAYS, build_statistical_forecast
from app.services.indicators import DEFAULT_BASE_TEMP, DEFAULT_TARGET_GDD, DEFAULT_UPPER_TEMP
from app.services.ml import ModelRegistry, _complete_series, predict_with_model

logger = logging.getLogger("agroclima.models")


class ModelService:
    def __init__(self, settings: Settings, client: httpx.AsyncClient, registry: ModelRegistry):
        self.settings = settings
        self.dashboard = DashboardService(settings, client)
        self.java = JavaApiClient(settings, client)
        self.registry = registry

    async def status(self) -> ModelStatus:
        """Estado do registro: disponibilidade estatística, histórico completo e modelo ML ativo."""
        days, _ = await self.dashboard.load_days(self.settings.model_max_train_days)
        complete = _complete_series(days)
        statistical = build_statistical_forecast(days)
        active = self.registry.load_info()
        ml_enabled = len(complete) >= self.settings.model_min_train_days
        if active is not None:
            message = f"Modelo de machine learning ativo ({active.algorithm}, {active.training_days} dias de treino)."
        elif ml_enabled:
            message = (
                f"Histórico suficiente ({len(complete)} dias completos). Treine um modelo de machine learning "
                "para projeções mais precisas; até lá, a projeção estatística é usada."
            )
        else:
            message = (
                f"Machine learning desabilitado: {len(complete)} dias completos disponíveis, mínimo de "
                f"{self.settings.model_min_train_days}. A projeção estatística segue disponível quando há dados."
            )
        return ModelStatus(
            statistical_available=statistical.available, statistical_message=statistical.message,
            ml_enabled=ml_enabled, ml_min_train_days=self.settings.model_min_train_days,
            complete_days=len(complete), active_model=active, storage_dir=str(self.registry.dir), message=message,
        )

    async def train(self, request: TrainRequest, authorization: str) -> TrainResult:
        """Treina no histórico real, persiste e audita. Recusa (409) sem histórico mínimo."""
        days, _ = await self.dashboard.load_days(self.settings.model_max_train_days)
        outcome = await self.registry.train_and_save(
            days, algorithm=request.algorithm, horizon_days=request.horizon_days,
            min_train_days=self.settings.model_min_train_days,
        )
        info = outcome.info
        await self._audit(
            authorization, "Treinou modelo preditivo", "Modelos Preditivos",
            f"Algoritmo {info.algorithm}; {info.training_days} dias de treino; "
            f"horizonte {info.horizon_days} dias; modelo {info.model_id}.",
        )
        return TrainResult(
            model=info,
            message=f"Modelo treinado e ativado ({info.algorithm}, {info.training_days} dias). "
                    "Métricas de validação walk-forward disponíveis no painel.",
        )

    async def forecast(self, horizon_days: int = 3) -> ForecastSummary:
        """Projeção dos próximos dias: usa o modelo ML ativo quando existe; senão, o estatístico."""
        days, _ = await self.dashboard.load_days(max(STAT_MAX_LOOKBACK_DAYS, self.settings.model_min_train_days))
        info = self.registry.load_info()
        if info is not None:
            payload = self.registry.load_payload()
            if payload is not None:
                result = predict_with_model(info, payload, days, horizon_days)
                if result.available:
                    return result
                logger.warning("Modelo ML ativo não pôde projetar (%s); usando estatístico.", result.message)
        return build_statistical_forecast(days, horizon_days)

    async def delete_active(self, authorization: str) -> ModelStatus:
        """Remove o modelo ML ativo (a projeção volta a ser estatística) e audita."""
        info = self.registry.load_info()
        removed = self.registry.delete()
        if removed and info is not None:
            await self._audit(
                authorization, "Removeu modelo preditivo", "Modelos Preditivos",
                f"Modelo {info.model_id} ({info.algorithm}) removido; projeção volta ao modelo estatístico.",
            )
        return await self.status()

    async def agronomy(self, authorization: str, *, base_temp: float = DEFAULT_BASE_TEMP,
                       upper_temp: float = DEFAULT_UPPER_TEMP,
                       target_gdd: float = DEFAULT_TARGET_GDD) -> AgronomySummary:
        """Indicadores agronômicos por plantação. O escopo das plantações é definido pela API Java."""
        if upper_temp <= base_temp:
            raise ServiceError(400, "upper_temp deve ser maior que base_temp.")
        if target_gdd <= 0:
            raise ServiceError(400, "target_gdd deve ser maior que zero.")
        plantations = await self.java.plantations(authorization)
        days, _ = await self.dashboard.load_days(self.settings.model_max_train_days)
        return build_agronomy_summary(
            plantations, days, base_temp=base_temp, upper_temp=upper_temp, target_gdd=target_gdd,
            latitude=self.settings.vineyard_latitude, kc=self.settings.crop_coefficient,
            utc_offset_hours=self.settings.climate_utc_offset_hours,
        )

    async def _audit(self, authorization: str, action: str, resource: str, details: str) -> None:
        try:
            await self.java.record_audit(authorization, action, resource, details)
        except ServiceError as exception:
            logger.warning("Falha ao registrar auditoria (%s): %s", action, exception.message)
