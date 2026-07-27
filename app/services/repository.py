import asyncio
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain import PriceType
from app.models import BackfillCheckpoint, OHLCBar
from app.providers.base import PriceBar


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)


class BarRepository:
    UPSERT_BATCH_SIZE = 500

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self.session_factory = session_factory
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
                await session.commit()
        return len(normalized)

    async def get_backfill_checkpoint(self, checkpoint_key: str) -> BackfillCheckpoint | None:
        async with self.session_factory() as session:
            return await session.get(BackfillCheckpoint, checkpoint_key)

    async def save_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None:
        async with self._write_lock:
            async with self.session_factory() as session:
                await session.merge(checkpoint)
                await session.commit()

    async def list_bars(self, *, price_type: PriceType, limit: int) -> list[OHLCBar]:
        async with self.session_factory() as session:
            result = await session.execute(
                select(OHLCBar)
                .where(OHLCBar.price_type == price_type.value)
                .order_by(OHLCBar.timestamp.desc())
                .limit(limit)
            )
            return list(reversed(result.scalars().all()))

    async def count_bars(self) -> int:
        async with self.session_factory() as session:
            result = await session.scalar(select(func.count(OHLCBar.timestamp)))
            return int(result or 0)

    async def latest_timestamp(self) -> datetime | None:
        async with self.session_factory() as session:
            return await session.scalar(select(func.max(OHLCBar.timestamp)))
