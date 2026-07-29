from datetime import UTC, date, datetime

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
        "start",
        "end",
        "next_cursor",
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


class BarsEnvelope(UTCDateTimeModel):
    pair: str
    bar_size: str
    price_type: PriceType
    start: datetime | None
    end: datetime | None
    count: int
    has_more: bool
    next_cursor: datetime | None
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


class PeriodMetricsResponse(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    timezone: str
    bar_count: int
    open: float
    close: float
    high: float
    low: float
    average_open_close: float
    average_high_low: float


class DailyMetricsResponse(PeriodMetricsResponse):
    day: date


class WeeklyMetricsResponse(PeriodMetricsResponse):
    week: str


class IBDailyBarResponse(BaseModel):
    provider: str
    pair: str
    bar_size: str
    price_type: PriceType
    day: date
    open: float
    close: float
    high: float
    low: float
    stored: bool


class IBWeeklyBarResponse(BaseModel):
    provider: str
    pair: str
    bar_size: str
    price_type: PriceType
    week: str
    open: float
    close: float
    high: float
    low: float


class MonthlyMetricsResponse(PeriodMetricsResponse):
    month: str


class YearlyMetricsResponse(PeriodMetricsResponse):
    year: int


class DailyMetricsBatchResponse(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    timezone: str
    start: date
    end: date
    count: int
    has_more: bool
    next_cursor: date | None
    metrics: list[DailyMetricsResponse]


class WeeklyMetricsBatchResponse(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    timezone: str
    start: str
    end: str
    count: int
    has_more: bool
    next_cursor: str | None
    metrics: list[WeeklyMetricsResponse]


class MonthlyMetricsBatchResponse(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    timezone: str
    start: str
    end: str
    count: int
    has_more: bool
    next_cursor: str | None
    metrics: list[MonthlyMetricsResponse]


class YearlyMetricsBatchResponse(BaseModel):
    pair: str
    bar_size: str
    price_type: PriceType
    timezone: str
    start: int
    end: int
    count: int
    has_more: bool
    next_cursor: int | None
    metrics: list[YearlyMetricsResponse]


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
    metrics_cache_backend: str
    metrics_cache_ttl_seconds: int
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
