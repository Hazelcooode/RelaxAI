"""Deployment configuration.

Every external value is configuration. Values come from environment variables using
``RELAXAI__<GROUP>__<FIELD>`` (for example ``RELAXAI__DB__URL``) or from a protected env
file named by ``RELAXAI_ENV_FILE`` (default ``.env``). Secrets are ``SecretStr`` so they are
never rendered by ``repr`` or logs.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOCAL_CURSOR_KEY = "local-development-cursor-key-do-not-use-in-production"


class Environment(StrEnum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class DbSettings(BaseModel):
    url: SecretStr = SecretStr("postgresql+psycopg://relaxai@localhost:5432/relaxai")
    pool_size: int = Field(default=10, ge=1)
    max_overflow: int = Field(default=10, ge=0)
    pool_timeout_seconds: float = Field(default=10.0, gt=0)
    statement_timeout_ms: int = Field(default=30_000, ge=0)
    echo: bool = False


class BlobSettings(BaseModel):
    account_url: str | None = None
    container_prefix: str = "relaxai"


class QdrantSettings(BaseModel):
    url: str | None = None
    api_key: SecretStr | None = None
    collection: str = "relaxai_records_v1"


class EntraSettings(BaseModel):
    tenant_id: str | None = None
    authority: str | None = None
    audience: str | None = None
    jwks_url: str | None = None


class GraphSettings(BaseModel):
    client_id: str | None = None
    client_secret: SecretStr | None = None
    redirect_uri: str | None = None


class AiSettings(BaseModel):
    endpoint: str | None = None
    api_key: SecretStr | None = None
    chat_deployment: str | None = None
    embedding_deployment: str | None = None
    document_intelligence_endpoint: str | None = None


class EmailSettings(BaseModel):
    provider: Literal["outbox", "acs"] = "outbox"
    connection_string: SecretStr | None = None
    sender: str | None = None


class CryptoSettings(BaseModel):
    kek_provider: Literal["file", "keyvault"] = "file"
    key_vault_url: str | None = None
    local_kek_dir: str = ".local/kek"
    cursor_signing_key: SecretStr = SecretStr(LOCAL_CURSOR_KEY)


class TelemetrySettings(BaseModel):
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    azure_monitor_connection_string: SecretStr | None = None


class LimitsSettings(BaseModel):
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1)
    page_default_limit: int = Field(default=25, ge=1)
    page_max_limit: int = Field(default=100, ge=1)


class WorkerSettings(BaseModel):
    concurrency: int = Field(default=4, ge=1, description="Jobs one worker process runs at once")
    poll_interval_seconds: float = Field(default=1.0, gt=0)
    shutdown_grace_seconds: float = Field(default=30.0, ge=0)
    reservation_ttl_seconds: int = Field(default=900, ge=1)


class UsageSettings(BaseModel):
    """Default quotas and planning prices. ``quota_policies`` rows override the quotas.

    Prices are planning estimates (EUR per unit) used only for ``estimatedEur``; they are
    not billing data. Defaults keep paid AI/OCR work inside the PRD's EUR 15 ceiling.
    """

    period: Literal["Total", "Monthly"] = "Total"
    default_quotas: dict[str, int] = {
        "AiTokensIn": 20_000_000,
        "AiTokensOut": 4_000_000,
        "EmbeddingTokens": 20_000_000,
        "OcrPages": 2_000,
        "BlobBytes": 5 * 1024**3,
    }
    unit_price_eur: dict[str, float] = {
        "AiTokensIn": 0.15 / 1_000_000,
        "AiTokensOut": 0.60 / 1_000_000,
        "EmbeddingTokens": 0.02 / 1_000_000,
        "OcrPages": 1.50 / 1_000,
        "BlobBytes": 0.02 / 1024**3,
    }
    planning_ceilings_eur: dict[str, float] = {
        "VmDiskNetworking": 35,
        "AzureOpenAiDocumentIntelligence": 15,
        "BlobBackupsKeyVaultMonitor": 5,
        "DomainDeployment": 15,
        "Contingency": 30,
    }


class OperatorSettings(BaseModel):
    # TEMPORARY until T03 (Entra tokens + Platform Operator role): /operator/* accepts
    # ``Authorization: Bearer <bootstrap_token>``. Unset = every operator route is denied.
    bootstrap_token: SecretStr | None = None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RELAXAI__",
        env_nested_delimiter="__",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    environment: Environment = Environment.LOCAL
    firm_id: str = Field(default="frm_local", pattern=r"^frm_[A-Za-z0-9]+$")

    db: DbSettings = DbSettings()
    blob: BlobSettings = BlobSettings()
    qdrant: QdrantSettings = QdrantSettings()
    entra: EntraSettings = EntraSettings()
    graph: GraphSettings = GraphSettings()
    ai: AiSettings = AiSettings()
    email: EmailSettings = EmailSettings()
    crypto: CryptoSettings = CryptoSettings()
    telemetry: TelemetrySettings = TelemetrySettings()
    limits: LimitsSettings = LimitsSettings()
    worker: WorkerSettings = WorkerSettings()
    usage: UsageSettings = UsageSettings()
    operator: OperatorSettings = OperatorSettings()

    @model_validator(mode="after")
    def _production_requires_real_secrets(self) -> Self:
        if self.environment is Environment.PRODUCTION:
            key = self.crypto.cursor_signing_key.get_secret_value()
            if key == LOCAL_CURSOR_KEY or len(key) < 32:
                raise ValueError("crypto.cursor_signing_key must be a unique value of >= 32 chars")
            token = self.operator.bootstrap_token
            if token is not None and len(token.get_secret_value()) < 32:
                raise ValueError("operator.bootstrap_token must be >= 32 chars")
        return self


def load_settings() -> Settings:
    """Load settings from the environment and the protected env file, if present."""
    env_file = os.environ.get("RELAXAI_ENV_FILE", ".env")
    return Settings(_env_file=env_file)
