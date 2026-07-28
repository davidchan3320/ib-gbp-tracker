from datetime import date
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

SUPPORTED_BAR_SIZES = ("1 min", "5 mins", "15 mins", "1 hour", "4 hours", "1 day")


class Settings(BaseSettings):
    """Runtime settings loaded from environment variables and an optional .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "FX Tape"
    environment: str = "development"
    data_provider: Literal["demo", "ib"] = "demo"

    database_url: str = "sqlite+aiosqlite:///./fx_tape.db"

    ib_host: str = "127.0.0.1"
    ib_port: int = Field(default=4002, ge=1, le=65535)
    ib_client_id: int = Field(default=21, ge=0)
    ib_backfill_client_id: int = Field(default=22, ge=0)
    ib_timeout_seconds: float = Field(default=12.0, gt=0, le=120)

    fx_pair: str = "GBPUSD"
    bar_size: str = "1 min"
    history_duration: str = "1 D"
    sync_interval_seconds: int = Field(default=60, ge=15)
    sync_on_startup: bool = True
    scheduler_enabled: bool = True
    metrics_cache_backend: Literal["sqlite", "postgresql", "redis"] | None = None
    metrics_cache_url: str | None = None
    metrics_cache_ttl_seconds: int = Field(default=300, ge=0, le=86_400)

    backfill_start: date = date(2017, 1, 1)
    backfill_request_delay_seconds: float = Field(default=10.0, ge=0, le=600)
    backfill_max_retries: int = Field(default=3, ge=0, le=10)
    backfill_retry_delay_seconds: float = Field(default=30.0, ge=0, le=3_600)

    @field_validator("fx_pair")
    @classmethod
    def validate_pair(cls, value: str) -> str:
        normalized = value.replace("/", "").upper().strip()
        if len(normalized) != 6 or not normalized.isalpha():
            raise ValueError("fx_pair must contain two three-letter currency codes")
        return normalized

    @field_validator("bar_size")
    @classmethod
    def validate_bar_size(cls, value: str) -> str:
        if value not in SUPPORTED_BAR_SIZES:
            choices = ", ".join(SUPPORTED_BAR_SIZES)
            raise ValueError(f"bar_size must be one of: {choices}")
        return value

    @field_validator("metrics_cache_backend", mode="before")
    @classmethod
    def normalize_metrics_cache_backend(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.lower().strip()
        return "postgresql" if normalized == "pgsql" else normalized

    @model_validator(mode="after")
    def validate_metrics_cache(self) -> Self:
        backend = self.resolved_metrics_cache_backend
        url = self.resolved_metrics_cache_url
        expected_prefixes = {
            "sqlite": ("sqlite://", "sqlite+aiosqlite://"),
            "postgresql": ("postgresql://", "postgresql+asyncpg://", "postgres://"),
            "redis": ("redis://", "rediss://"),
        }
        if not url.startswith(expected_prefixes[backend]):
            raise ValueError(f"metrics_cache_url must use a {backend} URL")
        return self

    @property
    def display_pair(self) -> str:
        return f"{self.fx_pair[:3]}/{self.fx_pair[3:]}"

    @property
    def database_backend(self) -> str:
        return "postgresql" if self.database_url.startswith("postgresql") else "sqlite"

    @property
    def resolved_metrics_cache_backend(self) -> Literal["sqlite", "postgresql", "redis"]:
        if self.metrics_cache_backend is not None:
            return self.metrics_cache_backend
        return "postgresql" if self.database_backend == "postgresql" else "sqlite"

    @property
    def resolved_metrics_cache_url(self) -> str:
        backend = self.resolved_metrics_cache_backend
        if self.metrics_cache_url:
            url = self.metrics_cache_url.strip()
            if backend == "sqlite" and url.startswith("sqlite://"):
                return url.replace("sqlite://", "sqlite+aiosqlite://", 1)
            if backend == "postgresql" and url.startswith("postgresql://"):
                return url.replace("postgresql://", "postgresql+asyncpg://", 1)
            if backend == "postgresql" and url.startswith("postgres://"):
                return url.replace("postgres://", "postgresql+asyncpg://", 1)
            return url
        if backend == self.database_backend:
            return self.database_url
        if backend == "sqlite":
            return "sqlite+aiosqlite:///./fx_tape_cache.db"
        if backend == "redis":
            return "redis://127.0.0.1:6379/0"
        raise ValueError(
            "metrics_cache_url is required when PostgreSQL cache does not use the application "
            "database"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
