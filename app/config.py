from __future__ import annotations

from urllib.parse import urlparse

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


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
    cors_allowed_origins: str = (
        "http://localhost,http://localhost:3000,http://localhost:5173,http://localhost:5500,http://localhost:8081,"
        "http://127.0.0.1,http://127.0.0.1:3000,http://127.0.0.1:5173,http://127.0.0.1:5500,http://127.0.0.1:8081"
    )
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

    @property
    def configured_origins(self) -> list[str]:
        return [origin.strip() for origin in self.cors_allowed_origins.split(",") if origin.strip()]

    @property
    def internal_key_configured(self) -> bool:
        return len(self.internal_api_key.get_secret_value()) >= 32

