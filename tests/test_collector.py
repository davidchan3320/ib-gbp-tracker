from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.providers.demo import DemoHistoricalDataProvider
from app.services.collector import Collector, _demo_chunk_sizes


def test_demo_sync_duration_seeds_missing_and_stale_history() -> None:
    settings = Settings(
        bar_size="1 min",
        demo_history_duration="5 D",
        scheduler_enabled=False,
    )
    collector = Collector(
        settings=settings,
        provider=DemoHistoricalDataProvider(),
        repository=None,
    )
    now = datetime.now(UTC).replace(second=0, microsecond=0)

    assert collector._sync_duration(oldest=None, latest=None) == "5 D"
    short_oldest = now - timedelta(days=1) + timedelta(minutes=1)
    short_plan = collector._sync_plan(oldest=short_oldest, latest=now)
    assert short_plan.duration == "345600 S"
    assert short_plan.end_at == short_oldest
    assert short_plan.started_from is None
    assert (
        collector._sync_duration(
            oldest=now - timedelta(days=5) + timedelta(minutes=1),
            latest=now,
        )
        == "3600 S"
    )
    assert (
        collector._sync_duration(
            oldest=now - timedelta(days=179, hours=12) + timedelta(minutes=1),
            latest=now,
        )
        == "3600 S"
    )
    assert (
        collector._sync_duration(
            oldest=now - timedelta(days=6),
            latest=now - timedelta(hours=1),
        )
        == "3600 S"
    )


def test_demo_long_history_is_split_without_losing_buckets(monkeypatch) -> None:
    monkeypatch.setattr("app.services.collector.MAX_DEMO_BUCKETS", 100)

    assert _demo_chunk_sizes(300) == [100, 100, 100]
    assert _demo_chunk_sizes(201) == [100, 100, 1]


async def test_demo_long_history_chunks_are_stored_contiguously(monkeypatch) -> None:
    monkeypatch.setattr("app.services.collector.MAX_DEMO_BUCKETS", 100)

    class RecordingRepository:
        def __init__(self) -> None:
            self.bars = []

        async def timestamp_bounds(self):
            return None, None

        async def upsert_bars(self, bars):
            self.bars.extend(bars)
            return len(bars)

    repository = RecordingRepository()
    collector = Collector(
        settings=Settings(
            bar_size="1 min",
            demo_history_duration="18000 S",
            scheduler_enabled=False,
        ),
        provider=DemoHistoricalDataProvider(),
        repository=repository,
    )

    result = await collector.sync()

    timestamps = sorted({bar.timestamp for bar in repository.bars})
    assert result.bars_received == 300 * 3
    assert result.bars_written == 300 * 3
    assert len(timestamps) == 300
    assert timestamps[-1] - timestamps[0] == timedelta(minutes=299)
