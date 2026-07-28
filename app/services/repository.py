import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.domain import PriceType
from app.models import BackfillCheckpoint, MetricCacheState, OHLCBar
from app.providers.base import PriceBar
from app.services.cache import MetricCacheBackend, NullMetricCache

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BarPeriodSummary:
    bar_count: int
    open: float
    close: float
    high: float
    low: float
    period_start: datetime | None = None


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class BarRepository:
    UPSERT_BATCH_SIZE = 500

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        metric_cache: MetricCacheBackend | None = None,
        cache_ttl_seconds: int = 300,
    ) -> None:
        self.session_factory = session_factory
        self.metric_cache = metric_cache or NullMetricCache()
        self.cache_ttl_seconds = cache_ttl_seconds
        self._write_lock = asyncio.Lock()

    async def upsert_bars(self, bars: list[PriceBar]) -> int:
        if not bars:
            return 0

        normalized = {(bar.price_type.value, as_utc(bar.timestamp)): bar for bar in bars}
        values = [
            {
                "price_type": price_type,
                "timestamp": timestamp,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "weighted_average_price": bar.weighted_average_price,
                "trade_count": bar.trade_count,
            }
            for (price_type, timestamp), bar in normalized.items()
        ]
        async with self._write_lock:
            async with self.session_factory() as session:
                dialect = session.bind.dialect.name if session.bind is not None else ""
                if dialect == "postgresql":
                    insert = postgresql_insert
                elif dialect == "sqlite":
                    insert = sqlite_insert
                else:
                    raise RuntimeError(f"Unsupported database dialect for upsert: {dialect}")

                for offset in range(0, len(values), self.UPSERT_BATCH_SIZE):
                    statement = insert(OHLCBar).values(
                        values[offset : offset + self.UPSERT_BATCH_SIZE]
                    )
                    statement = statement.on_conflict_do_update(
                        index_elements=[OHLCBar.price_type, OHLCBar.timestamp],
                        set_={
                            "open": statement.excluded.open,
                            "high": statement.excluded.high,
                            "low": statement.excluded.low,
                            "close": statement.excluded.close,
                            "volume": statement.excluded.volume,
                            "weighted_average_price": (statement.excluded.weighted_average_price),
                            "trade_count": statement.excluded.trade_count,
                        },
                    )
                    await session.execute(statement)
                await session.execute(
                    update(MetricCacheState)
                    .where(MetricCacheState.id == 1)
                    .values(generation=MetricCacheState.generation + 1)
                )
                await session.commit()
            try:
                await self.metric_cache.clear()
            except Exception:
                logger.warning("Failed to clear the metric cache after a bar write", exc_info=True)
        return len(normalized)

    async def _read_metric_cache(
        self,
        session: AsyncSession,
        cache_key: str,
    ) -> tuple[int, dict[str, object] | None]:
        generation = int(
            await session.scalar(
                select(MetricCacheState.generation).where(MetricCacheState.id == 1)
            )
            or 0
        )
        if self.cache_ttl_seconds == 0:
            return generation, None
        try:
            entry = await self.metric_cache.get(cache_key)
        except Exception:
            logger.warning("Failed to read the metric cache", exc_info=True)
            return generation, None
        if entry is None or entry.generation != generation:
            return generation, None
        return generation, entry.payload

    async def _write_metric_cache(
        self,
        *,
        cache_key: str,
        generation: int,
        payload: dict[str, object],
    ) -> None:
        if self.cache_ttl_seconds == 0:
            return
        try:
            await self.metric_cache.set(
                cache_key,
                generation=generation,
                payload=payload,
                ttl_seconds=self.cache_ttl_seconds,
            )
        except Exception:
            logger.warning("Failed to write the metric cache", exc_info=True)

    @staticmethod
    def _summary_payload(summary: BarPeriodSummary) -> dict[str, object]:
        return {
            "period_start": summary.period_start.isoformat() if summary.period_start else None,
            "bar_count": summary.bar_count,
            "open": summary.open,
            "close": summary.close,
            "high": summary.high,
            "low": summary.low,
        }

    @staticmethod
    def _summary_from_payload(payload: dict[str, object]) -> BarPeriodSummary:
        period_start_value = payload.get("period_start")
        period_start = (
            as_utc(datetime.fromisoformat(str(period_start_value)))
            if period_start_value is not None
            else None
        )
        return BarPeriodSummary(
            period_start=period_start,
            bar_count=int(str(payload["bar_count"])),
            open=float(str(payload["open"])),
            close=float(str(payload["close"])),
            high=float(str(payload["high"])),
            low=float(str(payload["low"])),
        )

    async def get_backfill_checkpoint(self, checkpoint_key: str) -> BackfillCheckpoint | None:
        async with self.session_factory() as session:
            return await session.get(BackfillCheckpoint, checkpoint_key)

    async def save_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None:
        async with self._write_lock:
            async with self.session_factory() as session:
                await session.merge(checkpoint)
                await session.commit()

    async def list_bars(self, *, price_type: PriceType, limit: int) -> list[OHLCBar]:
        rows, _has_more = await self.page_bars(price_type=price_type, limit=limit)
        return rows

    async def page_bars(
        self,
        *,
        price_type: PriceType,
        limit: int,
        start: datetime | None = None,
        end: datetime | None = None,
        before: datetime | None = None,
    ) -> tuple[list[OHLCBar], bool]:
        async with self.session_factory() as session:
            statement = select(OHLCBar).where(OHLCBar.price_type == price_type.value)
            if start is not None:
                statement = statement.where(OHLCBar.timestamp >= as_utc(start))
            if end is not None:
                statement = statement.where(OHLCBar.timestamp < as_utc(end))
            if before is not None:
                statement = statement.where(OHLCBar.timestamp < as_utc(before))

            result = await session.execute(
                statement.order_by(OHLCBar.timestamp.desc()).limit(limit + 1)
            )
            descending_rows = list(result.scalars().all())
            has_more = len(descending_rows) > limit
            return list(reversed(descending_rows[:limit])), has_more

    async def summarize_bars(
        self,
        *,
        price_type: PriceType,
        start: datetime,
        end: datetime,
    ) -> BarPeriodSummary | None:
        """Summarize every bar in a half-open time range without loading them all."""
        range_start = as_utc(start)
        range_end = as_utc(end)
        cache_key = ":".join(
            (
                "metric",
                "single",
                price_type.value,
                range_start.isoformat(),
                range_end.isoformat(),
            )
        )
        first_bar = aliased(OHLCBar)
        last_bar = aliased(OHLCBar)
        first_open = (
            select(first_bar.open)
            .where(
                first_bar.price_type == price_type.value,
                first_bar.timestamp >= range_start,
                first_bar.timestamp < range_end,
            )
            .order_by(first_bar.timestamp.asc())
            .limit(1)
            .scalar_subquery()
        )
        last_close = (
            select(last_bar.close)
            .where(
                last_bar.price_type == price_type.value,
                last_bar.timestamp >= range_start,
                last_bar.timestamp < range_end,
            )
            .order_by(last_bar.timestamp.desc())
            .limit(1)
            .scalar_subquery()
        )

        async with self.session_factory() as session:
            generation, cached = await self._read_metric_cache(session, cache_key)
            if cached is not None and "summary" in cached:
                cached_summary = cached["summary"]
                return (
                    self._summary_from_payload(cached_summary)
                    if isinstance(cached_summary, dict)
                    else None
                )

            row = (
                await session.execute(
                    select(
                        func.count(OHLCBar.timestamp).label("bar_count"),
                        first_open.label("open"),
                        last_close.label("close"),
                        func.max(OHLCBar.high).label("high"),
                        func.min(OHLCBar.low).label("low"),
                    ).where(
                        OHLCBar.price_type == price_type.value,
                        OHLCBar.timestamp >= range_start,
                        OHLCBar.timestamp < range_end,
                    )
                )
            ).one()
            summary = (
                None
                if row.bar_count == 0
                else BarPeriodSummary(
                    bar_count=int(row.bar_count),
                    open=float(row.open),
                    close=float(row.close),
                    high=float(row.high),
                    low=float(row.low),
                )
            )
            await session.rollback()
            await self._write_metric_cache(
                cache_key=cache_key,
                generation=generation,
                payload={
                    "summary": self._summary_payload(summary) if summary is not None else None
                },
            )
            return summary

    async def summarize_bars_by_period(
        self,
        *,
        price_type: PriceType,
        start: datetime,
        end: datetime,
        period: Literal["day", "month", "year"],
        limit: int,
    ) -> tuple[list[BarPeriodSummary], bool]:
        """Return one keyset page of populated UTC calendar-period summaries."""
        range_start = as_utc(start)
        range_end = as_utc(end)
        cache_key = ":".join(
            (
                "metric",
                "batch",
                price_type.value,
                period,
                range_start.isoformat(),
                range_end.isoformat(),
                str(limit),
            )
        )

        async with self.session_factory() as session:
            generation, cached = await self._read_metric_cache(session, cache_key)
            if cached is not None and isinstance(cached.get("summaries"), list):
                return (
                    [
                        self._summary_from_payload(summary)
                        for summary in cached["summaries"]
                        if isinstance(summary, dict)
                    ],
                    bool(cached.get("has_more", False)),
                )

            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                period_expression = func.date_trunc(
                    period,
                    func.timezone("UTC", OHLCBar.timestamp),
                )
            elif dialect == "sqlite":
                period_formats = {
                    "day": "%Y-%m-%d 00:00:00",
                    "month": "%Y-%m-01 00:00:00",
                    "year": "%Y-01-01 00:00:00",
                }
                period_expression = func.strftime(period_formats[period], OHLCBar.timestamp)
            else:
                raise RuntimeError(f"Unsupported database dialect for period metrics: {dialect}")

            bucketed_bars = (
                select(
                    period_expression.label("period_start"),
                    OHLCBar.open.label("open"),
                    OHLCBar.close.label("close"),
                    OHLCBar.high.label("high"),
                    OHLCBar.low.label("low"),
                    func.row_number()
                    .over(
                        partition_by=period_expression,
                        order_by=OHLCBar.timestamp.asc(),
                    )
                    .label("open_rank"),
                    func.row_number()
                    .over(
                        partition_by=period_expression,
                        order_by=OHLCBar.timestamp.desc(),
                    )
                    .label("close_rank"),
                )
                .where(
                    OHLCBar.price_type == price_type.value,
                    OHLCBar.timestamp >= range_start,
                    OHLCBar.timestamp < range_end,
                )
                .cte("bucketed_bars")
            )
            statement = (
                select(
                    bucketed_bars.c.period_start,
                    func.count().label("bar_count"),
                    func.max(case((bucketed_bars.c.open_rank == 1, bucketed_bars.c.open))).label(
                        "open"
                    ),
                    func.max(case((bucketed_bars.c.close_rank == 1, bucketed_bars.c.close))).label(
                        "close"
                    ),
                    func.max(bucketed_bars.c.high).label("high"),
                    func.min(bucketed_bars.c.low).label("low"),
                )
                .group_by(bucketed_bars.c.period_start)
                .order_by(bucketed_bars.c.period_start.desc())
                .limit(limit + 1)
            )
            descending_rows = (await session.execute(statement)).all()

            has_more = len(descending_rows) > limit
            rows = list(reversed(descending_rows[:limit]))
            summaries: list[BarPeriodSummary] = []
            for row in rows:
                period_start = row.period_start
                if not isinstance(period_start, datetime):
                    period_start = datetime.fromisoformat(str(period_start))
                period_start = period_start.replace(tzinfo=period_start.tzinfo or UTC).astimezone(
                    UTC
                )
                summaries.append(
                    BarPeriodSummary(
                        period_start=period_start,
                        bar_count=int(row.bar_count),
                        open=float(row.open),
                        close=float(row.close),
                        high=float(row.high),
                        low=float(row.low),
                    )
                )
            await session.rollback()
            await self._write_metric_cache(
                cache_key=cache_key,
                generation=generation,
                payload={
                    "has_more": has_more,
                    "summaries": [self._summary_payload(summary) for summary in summaries],
                },
            )
            return summaries, has_more

    async def count_bars(self) -> int:
        async with self.session_factory() as session:
            result = await session.scalar(select(func.count(OHLCBar.timestamp)))
            return int(result or 0)

    async def latest_timestamp(self) -> datetime | None:
        async with self.session_factory() as session:
            return await session.scalar(select(func.max(OHLCBar.timestamp)))
