from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, field_validator

from app.domain import PriceType


class UTCDateTimeModel(BaseModel):
    @field_validator(
        "timestamp",
        "as_of",
        "start_at",
        "end_at",
        "cursor",
        "started_at",
        "completed_at",
        "updated_at",
        check_fields=False,
    )
    @classmethod
    def ensure_utc(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class BarResponse(UTCDateTimeModel):
    model_config = ConfigDict(from_attributes=True)

    price_type: PriceType
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float | None
    weighted_average_price: float | None
    trade_count: int | None


class BarsEnvelope(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    count: int
    bars: list[BarResponse]


class MetricsResponse(UTCDateTimeModel):
    pair: str
    bar_size: str
    price_type: PriceType
    as_of: datetime
    latest_close: float
    change_24h_pct: float | None
    high_24h: float
    low_24h: float
    sma_20: float | None
    atr_14: float | None
    realized_volatility_20_pct: float | None


class SyncRunResponse(UTCDateTimeModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    provider: str
    status: str
    started_at: datetime
    completed_at: datetime | None
    bars_received: int
    bars_written: int
    message: str | None


class StatusResponse(BaseModel):
    service: str
    provider: str
    pair: str
    bar_size: str
    price_types: list[PriceType]
    database: str
    scheduler_enabled: bool
    sync_interval_seconds: int
    collector_running: bool
    stored_bars: int
    gateway_host: str | None
    gateway_port: int | None
    last_sync: SyncRunResponse | None


class SyncResponse(BaseModel):
    run_id: int
    status: str
    bars_received: int
    bars_written: int
    incremental: bool


class ErrorResponse(BaseModel):
    detail: str
