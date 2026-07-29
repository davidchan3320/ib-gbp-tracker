import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import Database
from app.domain import PRICE_TYPES, PriceType
from app.providers import build_provider
from app.providers.ib import IBHistoricalDataProvider
from app.schemas import (
    BarResponse,
    BarsEnvelope,
    DailyMetricsBatchResponse,
    DailyMetricsResponse,
    ErrorResponse,
    IBDailyBarResponse,
    IBMonthlyBarResponse,
    IBWeeklyBarResponse,
    MetricsResponse,
    MonthlyMetricsBatchResponse,
    MonthlyMetricsResponse,
    StatusResponse,
    SyncResponse,
    SyncRunResponse,
    WeeklyMetricsBatchResponse,
    WeeklyMetricsResponse,
    YearlyMetricsBatchResponse,
    YearlyMetricsResponse,
)
from app.services.cache import build_metric_cache
from app.services.collector import Collector, SyncAlreadyRunningError
from app.services.metrics import calculate_metrics, calculate_period_metrics
from app.services.repository import BarPeriodSummary, BarRepository, as_utc
from app.services.scheduler import CollectorScheduler

STATIC_DIR = Path(__file__).parent / "static"
MAX_METRIC_BATCH_PERIODS = 10_000


def _parse_month(value: str, parameter_name: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m").replace(tzinfo=UTC)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"'{parameter_name}' must be a valid calendar month in YYYY-MM format",
        ) from exc


def _next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def _parse_week(value: str, parameter_name: str) -> datetime:
    try:
        year_text, week_text = value.split("-W", maxsplit=1)
        week_start = date.fromisocalendar(int(year_text), int(week_text), 1)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"'{parameter_name}' must be a valid ISO week in YYYY-Www format",
        ) from exc
    return datetime.combine(week_start, time.min, tzinfo=UTC)


def _format_week(value: datetime | date) -> str:
    iso_year, iso_week, _iso_weekday = value.isocalendar()
    return f"{iso_year:04}-W{iso_week:02}"


logger = logging.getLogger("uvicorn.error")


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    database = Database(runtime_settings.database_url)
    metric_cache = build_metric_cache(runtime_settings, database.session_factory)
    repository = BarRepository(
        database.session_factory,
        metric_cache=metric_cache,
        cache_ttl_seconds=runtime_settings.metrics_cache_ttl_seconds,
    )
    provider = build_provider(runtime_settings)
    collector = Collector(
        settings=runtime_settings,
        provider=provider,
        repository=repository,
    )
    scheduler = CollectorScheduler(collector, runtime_settings.sync_interval_seconds)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await database.create_schema()
        database_url = database.engine.url.render_as_string(hide_password=True)
        logger.info(
            "Database connected: backend=%s url=%s",
            runtime_settings.database_backend,
            database_url,
        )
        try:
            await metric_cache.initialize()
            if runtime_settings.scheduler_enabled:
                scheduler.start(run_immediately=runtime_settings.sync_on_startup)
            yield
        finally:
            await scheduler.stop()
            await provider.close()
            await metric_cache.close()
            await database.dispose()

    app = FastAPI(
        title="FX Tape API",
        version="0.9.0",
        description="Collect and inspect one-minute GBP/USD bid, ask, and midpoint OHLC bars.",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.database = database
    app.state.metric_cache = metric_cache
    app.state.repository = repository
    app.state.collector = collector
    app.state.provider = provider

    def period_metric_fields(summary: BarPeriodSummary) -> dict[str, int | float]:
        snapshot = calculate_period_metrics(
            open_price=summary.open,
            close_price=summary.close,
            high_price=summary.high,
            low_price=summary.low,
        )
        return {"bar_count": summary.bar_count, **asdict(snapshot)}

    def daily_metrics_response(
        summary: BarPeriodSummary,
        period_day: date,
        price_type: PriceType,
    ) -> DailyMetricsResponse:
        return DailyMetricsResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            day=period_day,
            timezone="UTC",
            **period_metric_fields(summary),
        )

    def monthly_metrics_response(
        summary: BarPeriodSummary,
        period_month: str,
        price_type: PriceType,
    ) -> MonthlyMetricsResponse:
        return MonthlyMetricsResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            month=period_month,
            timezone="UTC",
            **period_metric_fields(summary),
        )

    def weekly_metrics_response(
        summary: BarPeriodSummary,
        period_week: str,
        price_type: PriceType,
    ) -> WeeklyMetricsResponse:
        return WeeklyMetricsResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            week=period_week,
            timezone="UTC",
            **period_metric_fields(summary),
        )

    def yearly_metrics_response(
        summary: BarPeriodSummary,
        period_year: int,
        price_type: PriceType,
    ) -> YearlyMetricsResponse:
        return YearlyMetricsResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            year=period_year,
            timezone="UTC",
            **period_metric_fields(summary),
        )

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/api/v1/bars",
        response_model=BarsEnvelope,
        summary="List paginated historical bars",
        description=(
            "Returns bars chronologically within each page. The first page contains the newest "
            "bars in the half-open `[start, end)` range. Pass `next_cursor` back as `cursor` to "
            "request the next older page."
        ),
        tags=["market data"],
    )
    async def get_bars(
        price_type: PriceType = PriceType.MIDPOINT,
        start: Annotated[
            datetime | None,
            Query(
                description=(
                    "Inclusive UTC range start. A timezone-free value is interpreted as UTC."
                )
            ),
        ] = None,
        end: Annotated[
            datetime | None,
            Query(
                description=(
                    "Exclusive UTC range end. A timezone-free value is interpreted as UTC."
                )
            ),
        ] = None,
        cursor: Annotated[
            datetime | None,
            Query(
                description=(
                    "Exclusive upper-bound cursor returned as `next_cursor` by the prior page."
                )
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(
                ge=1,
                le=10_000,
                description="Maximum number of bars returned in one page.",
            ),
        ] = 1_440,
    ) -> BarsEnvelope:
        range_start = as_utc(start) if start is not None else None
        range_end = as_utc(end) if end is not None else None
        page_cursor = as_utc(cursor) if cursor is not None else None
        if range_start is not None and range_end is not None and range_start >= range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'start' must be earlier than 'end'",
            )
        if page_cursor is not None and range_start is not None and page_cursor <= range_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' must be later than 'start'",
            )
        if page_cursor is not None and range_end is not None and page_cursor > range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' cannot be later than 'end'",
            )

        rows, has_more = await repository.page_bars(
            price_type=price_type,
            limit=limit,
            start=range_start,
            end=range_end,
            before=page_cursor,
        )
        return BarsEnvelope(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            start=range_start,
            end=range_end,
            count=len(rows),
            has_more=has_more,
            next_cursor=rows[0].timestamp if has_more and rows else None,
            bars=[BarResponse.model_validate(row) for row in rows],
        )

    @app.get(
        "/api/v1/metrics/daily",
        response_model=DailyMetricsResponse | DailyMetricsBatchResponse,
        responses={404: {"model": ErrorResponse}},
        summary="Calculate metrics for one or more UTC calendar days",
        description=(
            "Pass `day` for one day, or pass both `start` and `end` for a half-open batch range. "
            "`average_open_close` is `(open + close) / 2`; `average_high_low` is "
            "`(high + low) / 2`."
        ),
        tags=["metrics"],
    )
    async def get_daily_metrics(
        day: Annotated[
            date | None,
            Query(description="Single UTC calendar day in `YYYY-MM-DD` format."),
        ] = None,
        start: Annotated[
            date | None,
            Query(description="Inclusive first UTC day in a batch range."),
        ] = None,
        end: Annotated[
            date | None,
            Query(description="Exclusive last UTC day in a batch range."),
        ] = None,
        cursor: Annotated[
            date | None,
            Query(description="Exclusive upper-bound day returned by the previous batch page."),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=1_000, description="Maximum periods returned in one batch page."),
        ] = 100,
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> DailyMetricsResponse | DailyMetricsBatchResponse:
        if day is not None:
            if start is not None or end is not None or cursor is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'day' cannot be combined with 'start', 'end', or 'cursor'",
                )
            if day == date.max:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'day' must be earlier than 9999-12-31",
                )
            day_start = datetime.combine(day, time.min, tzinfo=UTC)
            day_end = day_start + timedelta(days=1)
            summary = await repository.summarize_bars(
                price_type=price_type,
                start=day_start,
                end=day_end,
            )
            if summary is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No {price_type.value} bars stored for {day.isoformat()} UTC.",
                )
            return daily_metrics_response(summary, day, price_type)

        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide either 'day' or both 'start' and 'end'",
            )
        if start >= end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'start' must be earlier than 'end'",
            )
        if (end - start).days > MAX_METRIC_BATCH_PERIODS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Daily ranges cannot exceed {MAX_METRIC_BATCH_PERIODS} periods",
            )
        if cursor is not None and cursor <= start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' must be later than 'start'",
            )
        if cursor is not None and cursor > end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' cannot be later than 'end'",
            )

        summaries, has_more = await repository.summarize_bars_by_period(
            price_type=price_type,
            start=datetime.combine(start, time.min, tzinfo=UTC),
            end=datetime.combine(cursor or end, time.min, tzinfo=UTC),
            period="day",
            limit=limit,
        )
        metrics = [
            daily_metrics_response(summary, summary.period_start.date(), price_type)
            for summary in summaries
            if summary.period_start is not None
        ]
        return DailyMetricsBatchResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            timezone="UTC",
            start=start,
            end=end,
            count=len(metrics),
            has_more=has_more,
            next_cursor=metrics[0].day if has_more and metrics else None,
            metrics=metrics,
        )

    @app.get(
        "/api/v1/metrics/weekly",
        response_model=WeeklyMetricsResponse | WeeklyMetricsBatchResponse,
        responses={404: {"model": ErrorResponse}},
        summary="Calculate metrics for one or more UTC ISO weeks",
        description=(
            "Pass `week` for one Monday-based ISO week, or pass both `start` and `end` for a "
            "half-open batch range. `average_open_close` is `(open + close) / 2`; "
            "`average_high_low` is `(high + low) / 2`."
        ),
        tags=["metrics"],
    )
    async def get_weekly_metrics(
        week: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$",
                description="Single UTC ISO week in `YYYY-Www` format.",
            ),
        ] = None,
        start: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$",
                description="Inclusive first UTC ISO week in a batch range.",
            ),
        ] = None,
        end: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$",
                description="Exclusive last UTC ISO week in a batch range.",
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$",
                description="Exclusive upper-bound ISO week returned by the previous batch page.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=1_000, description="Maximum periods returned in one batch page."),
        ] = 100,
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> WeeklyMetricsResponse | WeeklyMetricsBatchResponse:
        if week is not None:
            if start is not None or end is not None or cursor is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'week' cannot be combined with 'start', 'end', or 'cursor'",
                )
            week_start = _parse_week(week, "week")
            try:
                week_end = week_start + timedelta(days=7)
            except OverflowError as exc:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'week' must have a following ISO week",
                ) from exc
            summary = await repository.summarize_bars(
                price_type=price_type,
                start=week_start,
                end=week_end,
            )
            if summary is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No {price_type.value} bars stored for {week} UTC.",
                )
            return weekly_metrics_response(summary, week, price_type)

        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide either 'week' or both 'start' and 'end'",
            )
        range_start = _parse_week(start, "start")
        range_end = _parse_week(end, "end")
        if range_start >= range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'start' must be earlier than 'end'",
            )
        if (range_end - range_start).days // 7 > MAX_METRIC_BATCH_PERIODS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Weekly ranges cannot exceed {MAX_METRIC_BATCH_PERIODS} periods",
            )
        range_cursor = _parse_week(cursor, "cursor") if cursor is not None else None
        if range_cursor is not None and range_cursor <= range_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' must be later than 'start'",
            )
        if range_cursor is not None and range_cursor > range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' cannot be later than 'end'",
            )

        summaries, has_more = await repository.summarize_bars_by_period(
            price_type=price_type,
            start=range_start,
            end=range_cursor or range_end,
            period="week",
            limit=limit,
        )
        metrics = [
            weekly_metrics_response(summary, _format_week(summary.period_start), price_type)
            for summary in summaries
            if summary.period_start is not None
        ]
        return WeeklyMetricsBatchResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            timezone="UTC",
            start=start,
            end=end,
            count=len(metrics),
            has_more=has_more,
            next_cursor=metrics[0].week if has_more and metrics else None,
            metrics=metrics,
        )

    @app.get(
        "/api/v1/ib/daily",
        response_model=IBDailyBarResponse,
        responses={
            404: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
        summary="Fetch and store one daily OHLC bar from Interactive Brokers",
        description=(
            "Requests a `1 day` historical bar directly from IB Gateway and returns the bar "
            "whose IB session date matches `day`. A successful result is idempotently stored in "
            "the database's dedicated `ib_daily_bars` table."
        ),
        tags=["market data"],
    )
    async def get_ib_daily_bar(
        day: Annotated[
            date,
            Query(description="IB session date in `YYYY-MM-DD` format."),
        ],
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> IBDailyBarResponse:
        if not isinstance(provider, IBHistoricalDataProvider):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Direct IB data requires DATA_PROVIDER=ib.",
            )
        if day == date.max:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'day' must be earlier than 9999-12-31",
            )

        try:
            bar = await provider.fetch_daily_bar(
                pair=runtime_settings.fx_pair,
                day=day,
                price_type=price_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        if bar is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=(
                    f"IB returned no {price_type.value} daily bar for "
                    f"{day.isoformat()}."
                ),
            )
        try:
            await repository.upsert_ib_daily_bar(bar)
        except Exception as exc:
            logger.exception("Failed to store the IB daily bar")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="IB daily bar was fetched but could not be stored.",
            ) from exc
        return IBDailyBarResponse(
            provider="ib",
            pair=runtime_settings.display_pair,
            bar_size="1 day",
            price_type=price_type,
            day=day,
            open=float(bar.open),
            close=float(bar.close),
            high=float(bar.high),
            low=float(bar.low),
            stored=True,
        )

    @app.get(
        "/api/v1/ib/weekly",
        response_model=IBWeeklyBarResponse,
        responses={
            404: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
        summary="Fetch and store one weekly OHLC bar from Interactive Brokers",
        description=(
            "Requests a `1 week` historical bar directly from IB Gateway and returns the bar "
            "whose IB date belongs to the requested Monday-based ISO week. A successful result "
            "is idempotently stored in the database's dedicated `ib_weekly_bars` table."
        ),
        tags=["market data"],
    )
    async def get_ib_weekly_bar(
        week: Annotated[
            str,
            Query(
                pattern=r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$",
                description="IB bar's ISO week in `YYYY-Www` format.",
            ),
        ],
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> IBWeeklyBarResponse:
        if not isinstance(provider, IBHistoricalDataProvider):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Direct IB data requires DATA_PROVIDER=ib.",
            )
        week_start = _parse_week(week, "week")
        week_start_date = week_start.date()
        if week_start_date > date.max - timedelta(days=7):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'week' must have a following ISO week",
            )

        try:
            bar = await provider.fetch_weekly_bar(
                pair=runtime_settings.fx_pair,
                week_start=week_start_date,
                price_type=price_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        if bar is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"IB returned no {price_type.value} weekly bar for {week}.",
            )
        try:
            await repository.upsert_ib_weekly_bar(bar, week_start=week_start_date)
        except Exception as exc:
            logger.exception("Failed to store the IB weekly bar")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="IB weekly bar was fetched but could not be stored.",
            ) from exc
        return IBWeeklyBarResponse(
            provider="ib",
            pair=runtime_settings.display_pair,
            bar_size="1 week",
            price_type=price_type,
            week=week,
            open=float(bar.open),
            close=float(bar.close),
            high=float(bar.high),
            low=float(bar.low),
            stored=True,
        )

    @app.get(
        "/api/v1/ib/monthly",
        response_model=IBMonthlyBarResponse,
        responses={
            404: {"model": ErrorResponse},
            502: {"model": ErrorResponse},
            503: {"model": ErrorResponse},
            500: {"model": ErrorResponse},
        },
        summary="Fetch and store one monthly OHLC bar from Interactive Brokers",
        description=(
            "Requests a `1 month` historical bar directly from IB Gateway and returns the bar "
            "whose IB date belongs to the requested calendar month. A successful result is "
            "idempotently stored in the database's dedicated `ib_monthly_bars` table."
        ),
        tags=["market data"],
    )
    async def get_ib_monthly_bar(
        month: Annotated[
            str,
            Query(
                pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                description="IB bar's calendar month in `YYYY-MM` format.",
            ),
        ],
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> IBMonthlyBarResponse:
        if not isinstance(provider, IBHistoricalDataProvider):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Direct IB data requires DATA_PROVIDER=ib.",
            )
        period_start = _parse_month(month, "month")
        if period_start.year == 9999 and period_start.month == 12:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'month' must be earlier than 9999-12",
            )
        month_start = period_start.date()

        try:
            bar = await provider.fetch_monthly_bar(
                pair=runtime_settings.fx_pair,
                month_start=month_start,
                price_type=price_type,
            )
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        if bar is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"IB returned no {price_type.value} monthly bar for {month}.",
            )
        try:
            await repository.upsert_ib_monthly_bar(bar, month_start=month_start)
        except Exception as exc:
            logger.exception("Failed to store the IB monthly bar")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="IB monthly bar was fetched but could not be stored.",
            ) from exc
        return IBMonthlyBarResponse(
            provider="ib",
            pair=runtime_settings.display_pair,
            bar_size="1 month",
            price_type=price_type,
            month=month,
            open=float(bar.open),
            close=float(bar.close),
            high=float(bar.high),
            low=float(bar.low),
            stored=True,
        )

    @app.get(
        "/api/v1/metrics/monthly",
        response_model=MonthlyMetricsResponse | MonthlyMetricsBatchResponse,
        responses={404: {"model": ErrorResponse}},
        summary="Calculate metrics for one or more UTC calendar months",
        description=(
            "Pass `month` for one month, or pass both `start` and `end` for a half-open batch "
            "range. "
            "`average_open_close` is `(open + close) / 2`; `average_high_low` is "
            "`(high + low) / 2`."
        ),
        tags=["metrics"],
    )
    async def get_monthly_metrics(
        month: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                description="Single UTC calendar month in `YYYY-MM` format.",
            ),
        ] = None,
        start: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                description="Inclusive first UTC month in a batch range.",
            ),
        ] = None,
        end: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                description="Exclusive last UTC month in a batch range.",
            ),
        ] = None,
        cursor: Annotated[
            str | None,
            Query(
                pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
                description="Exclusive upper-bound month returned by the previous batch page.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=1_000, description="Maximum periods returned in one batch page."),
        ] = 100,
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> MonthlyMetricsResponse | MonthlyMetricsBatchResponse:
        if month is not None:
            if start is not None or end is not None or cursor is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'month' cannot be combined with 'start', 'end', or 'cursor'",
                )
            month_start = _parse_month(month, "month")
            if month_start.year == 9999 and month_start.month == 12:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'month' must be earlier than 9999-12",
                )
            summary = await repository.summarize_bars(
                price_type=price_type,
                start=month_start,
                end=_next_month(month_start),
            )
            if summary is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No {price_type.value} bars stored for {month} UTC.",
                )
            return monthly_metrics_response(summary, month, price_type)

        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide either 'month' or both 'start' and 'end'",
            )
        range_start = _parse_month(start, "start")
        range_end = _parse_month(end, "end")
        if range_start >= range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'start' must be earlier than 'end'",
            )
        month_count = (range_end.year - range_start.year) * 12 + (
            range_end.month - range_start.month
        )
        if month_count > MAX_METRIC_BATCH_PERIODS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Monthly ranges cannot exceed {MAX_METRIC_BATCH_PERIODS} periods",
            )
        range_cursor = _parse_month(cursor, "cursor") if cursor is not None else None
        if range_cursor is not None and range_cursor <= range_start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' must be later than 'start'",
            )
        if range_cursor is not None and range_cursor > range_end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' cannot be later than 'end'",
            )

        summaries, has_more = await repository.summarize_bars_by_period(
            price_type=price_type,
            start=range_start,
            end=range_cursor or range_end,
            period="month",
            limit=limit,
        )
        metrics = [
            monthly_metrics_response(
                summary,
                summary.period_start.strftime("%Y-%m"),
                price_type,
            )
            for summary in summaries
            if summary.period_start is not None
        ]
        return MonthlyMetricsBatchResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            timezone="UTC",
            start=start,
            end=end,
            count=len(metrics),
            has_more=has_more,
            next_cursor=metrics[0].month if has_more and metrics else None,
            metrics=metrics,
        )

    @app.get(
        "/api/v1/metrics/yearly",
        response_model=YearlyMetricsResponse | YearlyMetricsBatchResponse,
        responses={404: {"model": ErrorResponse}},
        summary="Calculate metrics for one or more UTC calendar years",
        description=(
            "Pass `year` for one year, or pass both `start` and `end` for a half-open batch range. "
            "`average_open_close` is `(open + close) / 2`; `average_high_low` is "
            "`(high + low) / 2`."
        ),
        tags=["metrics"],
    )
    async def get_yearly_metrics(
        year: Annotated[
            int | None,
            Query(ge=1, le=9999, description="Single UTC calendar year."),
        ] = None,
        start: Annotated[
            int | None,
            Query(ge=1, le=9999, description="Inclusive first UTC year in a batch range."),
        ] = None,
        end: Annotated[
            int | None,
            Query(ge=1, le=9999, description="Exclusive last UTC year in a batch range."),
        ] = None,
        cursor: Annotated[
            int | None,
            Query(
                ge=1,
                le=9999,
                description="Exclusive upper-bound year returned by the previous batch page.",
            ),
        ] = None,
        limit: Annotated[
            int,
            Query(ge=1, le=1_000, description="Maximum periods returned in one batch page."),
        ] = 100,
        price_type: PriceType = PriceType.MIDPOINT,
    ) -> YearlyMetricsResponse | YearlyMetricsBatchResponse:
        if year is not None:
            if start is not None or end is not None or cursor is not None:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'year' cannot be combined with 'start', 'end', or 'cursor'",
                )
            if year == 9999:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail="'year' must be earlier than 9999",
                )
            summary = await repository.summarize_bars(
                price_type=price_type,
                start=datetime(year, 1, 1, tzinfo=UTC),
                end=datetime(year + 1, 1, 1, tzinfo=UTC),
            )
            if summary is None:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"No {price_type.value} bars stored for {year} UTC.",
                )
            return yearly_metrics_response(summary, year, price_type)

        if start is None or end is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="Provide either 'year' or both 'start' and 'end'",
            )
        if start >= end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'start' must be earlier than 'end'",
            )
        if end - start > MAX_METRIC_BATCH_PERIODS:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=f"Yearly ranges cannot exceed {MAX_METRIC_BATCH_PERIODS} periods",
            )
        if cursor is not None and cursor <= start:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' must be later than 'start'",
            )
        if cursor is not None and cursor > end:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="'cursor' cannot be later than 'end'",
            )

        summaries, has_more = await repository.summarize_bars_by_period(
            price_type=price_type,
            start=datetime(start, 1, 1, tzinfo=UTC),
            end=datetime(cursor or end, 1, 1, tzinfo=UTC),
            period="year",
            limit=limit,
        )
        metrics = [
            yearly_metrics_response(summary, summary.period_start.year, price_type)
            for summary in summaries
            if summary.period_start is not None
        ]
        return YearlyMetricsBatchResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            timezone="UTC",
            start=start,
            end=end,
            count=len(metrics),
            has_more=has_more,
            next_cursor=metrics[0].year if has_more and metrics else None,
            metrics=metrics,
        )

    @app.get(
        "/api/v1/metrics",
        response_model=MetricsResponse,
        responses={404: {"model": ErrorResponse}},
        tags=["metrics"],
    )
    async def get_metrics() -> MetricsResponse:
        rows = await repository.list_bars(price_type=PriceType.MIDPOINT, limit=2_000)
        snapshot = calculate_metrics(rows, runtime_settings.bar_size)
        if snapshot is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No bars stored yet. Run a sync to collect data.",
            )
        return MetricsResponse(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=PriceType.MIDPOINT,
            as_of=rows[-1].timestamp,
            **asdict(snapshot),
        )

    @app.get("/api/v1/status", response_model=StatusResponse, tags=["system"])
    async def get_status() -> StatusResponse:
        latest_run = collector.last_run
        stored_bars = await repository.count_bars()
        return StatusResponse(
            service="ok",
            provider=provider.name,
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_types=list(PRICE_TYPES),
            database=runtime_settings.database_backend,
            scheduler_enabled=runtime_settings.scheduler_enabled,
            sync_interval_seconds=runtime_settings.sync_interval_seconds,
            metrics_cache_backend=metric_cache.name,
            metrics_cache_ttl_seconds=runtime_settings.metrics_cache_ttl_seconds,
            collector_running=collector.is_running,
            stored_bars=stored_bars,
            gateway_host=runtime_settings.ib_host if provider.name == "ib" else None,
            gateway_port=runtime_settings.ib_port if provider.name == "ib" else None,
            last_sync=SyncRunResponse.model_validate(latest_run) if latest_run else None,
        )

    @app.post(
        "/api/v1/sync",
        response_model=SyncResponse,
        responses={409: {"model": ErrorResponse}, 502: {"model": ErrorResponse}},
        tags=["market data"],
    )
    async def run_sync() -> SyncResponse:
        try:
            result = await collector.sync()
        except SyncAlreadyRunningError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail=str(exc),
            ) from exc
        return SyncResponse(
            run_id=result.run_id,
            status=result.status,
            bars_received=result.bars_received,
            bars_written=result.bars_written,
            incremental=result.started_from is not None,
        )

    app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")

    @app.get("/", include_in_schema=False)
    async def dashboard(_request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()
