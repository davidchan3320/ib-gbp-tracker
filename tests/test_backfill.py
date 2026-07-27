import asyncio
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.config import Settings
from app.db import Database
from app.domain import PriceType
from app.models import BackfillCheckpoint
from app.providers.base import HistoricalDataProvider, PriceBar
from app.services.backfill import BackfillManager
from app.services.repository import BarRepository


class SignalingRepository(BarRepository):
    def __init__(self, session_factory) -> None:
        super().__init__(session_factory)
        self.chunk_saved = asyncio.Event()

    async def save_backfill_checkpoint(self, checkpoint: BackfillCheckpoint) -> None:
        await super().save_backfill_checkpoint(checkpoint)
        if checkpoint.status == "running" and checkpoint.chunks_completed >= 1:
            self.chunk_saved.set()


class StubHistoricalProvider(HistoricalDataProvider):
    name = "stub"

    def __init__(self, *, failures: int = 0) -> None:
        self.ends: list[datetime] = []
        self.attempts = 0
        self.failures = failures

    async def fetch_bars(
        self,
        *,
        pair: str,
        bar_size: str,
        duration: str,
        end_at: datetime | None = None,
        allow_empty: bool = False,
    ) -> list[PriceBar]:
        assert pair == "GBPUSD"
        assert bar_size == "1 min"
        assert duration == "1 D"
        assert end_at is not None
        assert allow_empty is True
        self.attempts += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("temporary IB throttle")
        self.ends.append(end_at)

        timestamp = end_at - timedelta(minutes=1)
        base = Decimal("1.29000")
        return [
            PriceBar(
                price_type=price_type,
                timestamp=timestamp,
                open=base,
                high=base + Decimal("0.00020"),
                low=base - Decimal("0.00020"),
                close=base + Decimal("0.00010"),
            )
            for price_type in PriceType
        ]


async def test_backfill_is_checkpointed_and_resumes_after_cancellation(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'backfill.db'}")
    await database.create_schema()
    repository = SignalingRepository(database.session_factory)
    provider = StubHistoricalProvider()

    def now() -> datetime:
        return datetime(2026, 7, 27, tzinfo=UTC)

    settings = Settings(
        scheduler_enabled=False,
        backfill_start=date(2026, 7, 24),
        backfill_request_delay_seconds=600,
        backfill_retry_delay_seconds=0,
    )

    first = BackfillManager(
        settings=settings,
        provider=provider,
        repository=repository,
        now=now,
    )
    await first.initialize()
    await first.start()
    await asyncio.wait_for(repository.chunk_saved.wait(), timeout=2)
    cancelled = await first.stop()

    assert cancelled.status == "cancelled"
    assert cancelled.cursor == datetime(2026, 7, 26, tzinfo=UTC)
    assert cancelled.chunks_completed == 1
    assert len(provider.ends) == 1

    resumed_settings = settings.model_copy(update={"backfill_request_delay_seconds": 0.0})
    resumed = BackfillManager(
        settings=resumed_settings,
        provider=provider,
        repository=repository,
        now=now,
    )
    loaded = await resumed.initialize()
    assert loaded.status == "cancelled"
    await resumed.start()
    completed = await resumed.wait()

    assert completed.status == "succeeded"
    assert completed.cursor == datetime(2026, 7, 24, tzinfo=UTC)
    assert completed.total_chunks == 3
    assert completed.chunks_completed == 3
    assert completed.progress_pct == 100
    assert completed.bars_written == 9
    assert provider.ends == [
        datetime(2026, 7, 27, tzinfo=UTC),
        datetime(2026, 7, 26, tzinfo=UTC),
        datetime(2026, 7, 25, tzinfo=UTC),
    ]
    assert await repository.count_bars() == 9

    checkpoint = await repository.get_backfill_checkpoint(completed.checkpoint_key)
    assert checkpoint is not None
    assert checkpoint.status == "succeeded"
    assert checkpoint.chunks_completed == 3

    await database.dispose()


async def test_backfill_retries_a_chunk_without_advancing_its_cursor(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'retry.db'}")
    await database.create_schema()
    repository = BarRepository(database.session_factory)
    provider = StubHistoricalProvider(failures=1)

    def now() -> datetime:
        return datetime(2026, 7, 27, tzinfo=UTC)

    manager = BackfillManager(
        settings=Settings(
            scheduler_enabled=False,
            backfill_start=date(2026, 7, 26),
            backfill_request_delay_seconds=0,
            backfill_max_retries=1,
            backfill_retry_delay_seconds=0,
        ),
        provider=provider,
        repository=repository,
        now=now,
    )
    await manager.start()
    completed = await manager.wait()

    assert completed.status == "succeeded"
    assert completed.chunks_completed == 1
    assert completed.bars_written == 3
    assert provider.attempts == 2
    assert provider.ends == [datetime(2026, 7, 27, tzinfo=UTC)]

    await database.dispose()
