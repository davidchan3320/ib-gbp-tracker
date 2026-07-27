from datetime import date
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
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

    fx_pair: str = "USDGBP"
    bar_size: str = "1 min"
    history_duration: str = "1 D"
    sync_interval_seconds: int = Field(default=60, ge=15)
    sync_on_startup: bool = True
    scheduler_enabled: bool = True

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

    @property
    def display_pair(self) -> str:
        return f"{self.fx_pair[:3]}/{self.fx_pair[3:]}"

    @property
    def database_backend(self) -> str:
        return "postgresql" if self.database_url.startswith("postgresql") else "sqlite"


@lru_cache
def get_settings() -> Settings:
    return Settings()
