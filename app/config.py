from __future__ import annotations

from typing import Literal
from urllib.parse import urlparse

from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Origens liberadas apenas em desenvolvimento (front local). Em produção a lista é sempre explícita.
DEV_CORS_ORIGINS = (
    "http://localhost,http://localhost:3000,http://localhost:5173,http://localhost:5500,http://localhost:8081,"
    "http://127.0.0.1,http://127.0.0.1:3000,http://127.0.0.1:5173,http://127.0.0.1:5500,http://127.0.0.1:8081"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]", "0.0.0.0"}


class Settings(BaseSettings):
    """Configuração do serviço analítico.

    O Python não acessa o ThingSpeak: as leituras chegam pela API Java (backend/java),
    autenticando-se com INTERNAL_API_KEY. Variáveis THINGSPEAK_* antigas são ignoradas.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    service_name: str = "AgroClima Analytics API"
    java_api_url: str = "http://localhost:8080"
    internal_api_key: SecretStr = SecretStr("")
    request_timeout_seconds: float = 12.0
    # Retry com backoff exponencial só para GET na API Java (falha de conexão, timeout, 502/503/504).
    java_retry_attempts: int = 3
    java_retry_backoff_seconds: float = 0.25
    app_env: Literal["development", "production"] = "development"
    # Vazio em desenvolvimento = origens locais; em produção é obrigatório informar (sem wildcard).
    cors_allowed_origins: str = ""
    # A autenticação usa o header Authorization (Bearer), não cookies: credenciais ficam desligadas.
    cors_allow_credentials: bool = False
    # Cache curto do perfil (/auth/me) por token. 0 desativa.
    profile_cache_ttl_seconds: float = 45.0
    # Limite de pontos da série bruta em /climate/history; acima disso exige resolution=hour|day.
    history_max_raw_points: int = 5000
    # Fuso local do vinhedo (horas em relação ao UTC) para fechar os dias nos indicadores. Pernambuco: -3.
    climate_utc_offset_hours: float = -3.0
    # Nome da fonte exibido nas respostas; a coleta em si é feita pela API Java.
    climate_source_name: str = "ThingSpeak"
    root_path: str = ""

    @field_validator("java_api_url")
    @classmethod
    def validate_http_url(cls, value: str) -> str:
        parsed = urlparse(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("JAVA_API_URL deve ser HTTP ou HTTPS e incluir host.")
        return value.strip().rstrip("/")

    @field_validator("request_timeout_seconds")
    @classmethod
    def validate_timeout(cls, value: float) -> float:
        if value < 1 or value > 60:
            raise ValueError("REQUEST_TIMEOUT_SECONDS deve estar entre 1 e 60 segundos.")
        return value

    @field_validator("java_retry_attempts")
    @classmethod
    def validate_retry_attempts(cls, value: int) -> int:
        if value < 1 or value > 5:
            raise ValueError("JAVA_RETRY_ATTEMPTS deve estar entre 1 e 5 (1 = sem retry).")
        return value

    @field_validator("java_retry_backoff_seconds")
    @classmethod
    def validate_retry_backoff(cls, value: float) -> float:
        if value < 0 or value > 5:
            raise ValueError("JAVA_RETRY_BACKOFF_SECONDS deve estar entre 0 e 5 segundos.")
        return value

    @field_validator("profile_cache_ttl_seconds")
    @classmethod
    def validate_cache_ttl(cls, value: float) -> float:
        if value < 0 or value > 300:
            raise ValueError("PROFILE_CACHE_TTL_SECONDS deve estar entre 0 e 300 segundos.")
        return value

    @field_validator("history_max_raw_points")
    @classmethod
    def validate_max_raw_points(cls, value: int) -> int:
        if value < 100:
            raise ValueError("HISTORY_MAX_RAW_POINTS deve ser pelo menos 100.")
        return value

    @field_validator("climate_utc_offset_hours")
    @classmethod
    def validate_utc_offset(cls, value: float) -> float:
        if value < -12 or value > 14:
            raise ValueError("CLIMATE_UTC_OFFSET_HOURS deve estar entre -12 e 14.")
        return value

    @model_validator(mode="after")
    def validate_cors(self) -> Settings:
        origins = self._parse_origins(self.cors_allowed_origins)
        if "*" in origins:
            raise ValueError("CORS_ALLOWED_ORIGINS não aceita wildcard (*); liste as origens explicitamente.")
        if self.app_env == "production":
            if not origins:
                raise ValueError("Em produção, defina CORS_ALLOWED_ORIGINS com as origens do front-end.")
            local = [origin for origin in origins if urlparse(origin).hostname in _LOCAL_HOSTS]
            if local:
                raise ValueError(f"Origens locais não são permitidas em produção: {', '.join(local)}.")
        return self

    @staticmethod
    def _parse_origins(raw: str) -> list[str]:
        return [origin.strip() for origin in raw.split(",") if origin.strip()]

    @property
    def configured_origins(self) -> list[str]:
        explicit = self._parse_origins(self.cors_allowed_origins)
        if explicit:
            return explicit
        return self._parse_origins(DEV_CORS_ORIGINS) if self.app_env == "development" else []

    @property
    def internal_key_configured(self) -> bool:
        return len(self.internal_api_key.get_secret_value()) >= 32

