import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.config import Settings
from app.providers.base import HistoricalDataProvider
from app.providers.demo import (
    BAR_SECONDS,
    MAX_DEMO_BUCKETS,
    MIN_DEMO_BUCKETS,
    bar_delta,
    duration_seconds,
)
from app.services.repository import BarRepository, as_utc

INCREMENTAL_DURATIONS = {
    "1 min": "3600 S",
    "5 mins": "1 D",
    "15 mins": "2 D",
    "1 hour": "3 D",
    "4 hours": "7 D",
    "1 day": "30 D",
}


class SyncAlreadyRunningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class SyncRunState:
    id: int
    provider: str
    status: str
    started_at: datetime
    completed_at: datetime | None = None
    bars_received: int = 0
    bars_written: int = 0
    message: str | None = None


@dataclass(frozen=True, slots=True)
class SyncResult:
    run_id: int
    status: str
    bars_received: int
    bars_written: int
    started_from: datetime | None


@dataclass(frozen=True, slots=True)
class _SyncPlan:
    duration: str
    end_at: datetime | None
    started_from: datetime | None


class Collector:
    def __init__(
        self,
        *,
        settings: Settings,
        provider: HistoricalDataProvider,
        repository: BarRepository,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.repository = repository
        self._lock = asyncio.Lock()
        self._run_sequence = 0
        self._last_run: SyncRunState | None = None

    @property
    def is_running(self) -> bool:
        return self._lock.locked()

    @property
    def last_run(self) -> SyncRunState | None:
        return self._last_run

    async def sync(self) -> SyncResult:
        if self._lock.locked():
            raise SyncAlreadyRunningError("A data sync is already running")

        async with self._lock:
            oldest, latest = await self.repository.timestamp_bounds()
            plan = self._sync_plan(oldest=oldest, latest=latest)
            self._run_sequence += 1
            run_id = self._run_sequence
            started_at = datetime.now(UTC)
            self._last_run = SyncRunState(
                id=run_id,
                provider=self.provider.name,
                status="running",
                started_at=started_at,
            )
            try:
                received, written = await self._fetch_and_store(
                    plan.duration,
                    end_at=plan.end_at,
                )
                self._last_run = SyncRunState(
                    id=run_id,
                    provider=self.provider.name,
                    status="succeeded",
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    bars_received=received,
                    bars_written=written,
                )
                return SyncResult(
                    run_id=run_id,
                    status="succeeded",
                    bars_received=received,
                    bars_written=written,
                    started_from=plan.started_from,
                )
            except Exception as exc:
                self._last_run = SyncRunState(
                    id=run_id,
                    provider=self.provider.name,
                    status="failed",
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    message=str(exc)[:1_000],
                )
                raise

    def _sync_duration(
        self,
        *,
        oldest: datetime | None,
        latest: datetime | None,
    ) -> str:
        return self._sync_plan(oldest=oldest, latest=latest).duration

    def _sync_plan(
        self,
        *,
        oldest: datetime | None,
        latest: datetime | None,
    ) -> _SyncPlan:
        if self.provider.name != "demo":
            return _SyncPlan(
                duration=(
                    INCREMENTAL_DURATIONS[self.settings.bar_size]
                    if latest is not None
                    else self.settings.history_duration
                ),
                end_at=None,
                started_from=latest,
            )
        if oldest is None or latest is None:
            return _SyncPlan(
                duration=self.settings.demo_history_duration,
                end_at=None,
                started_from=None,
            )

        interval = bar_delta(self.settings.bar_size)
        required_span = timedelta(
            seconds=duration_seconds(self.settings.demo_history_duration)
        )
        coverage = as_utc(latest) - as_utc(oldest) + interval
        coverage_tolerance = min(timedelta(days=3), required_span / 10)
        if coverage < required_span - coverage_tolerance:
            missing_seconds = int((required_span - coverage).total_seconds())
            return _SyncPlan(
                duration=f"{missing_seconds} S",
                end_at=as_utc(oldest),
                started_from=None,
            )

        stale_after = max(
            interval * 2,
            timedelta(seconds=self.settings.sync_interval_seconds * 2),
        )
        age = datetime.now(UTC) - as_utc(latest)
        incremental_duration = INCREMENTAL_DURATIONS[self.settings.bar_size]
        if age <= stale_after:
            return _SyncPlan(
                duration=incremental_duration,
                end_at=None,
                started_from=latest,
            )

        interval_seconds = BAR_SECONDS[self.settings.bar_size]
        incremental_buckets = max(
            MIN_DEMO_BUCKETS,
            duration_seconds(incremental_duration) // interval_seconds,
        )
        gap_buckets = int(age.total_seconds() // interval_seconds) + 1
        catch_up_buckets = max(incremental_buckets, gap_buckets)
        if catch_up_buckets == incremental_buckets:
            return _SyncPlan(
                duration=incremental_duration,
                end_at=None,
                started_from=latest,
            )
        catch_up_seconds = catch_up_buckets * interval_seconds
        if catch_up_seconds >= int(required_span.total_seconds()):
            return _SyncPlan(
                duration=self.settings.demo_history_duration,
                end_at=None,
                started_from=None,
            )
        return _SyncPlan(
            duration=f"{catch_up_seconds} S",
            end_at=None,
            started_from=latest,
        )

    async def _fetch_and_store(
        self,
        duration: str,
        *,
        end_at: datetime | None = None,
    ) -> tuple[int, int]:
        if self.provider.name != "demo":
            bars = await self.provider.fetch_bars(
                pair=self.settings.fx_pair,
                bar_size=self.settings.bar_size,
                duration=duration,
                end_at=end_at,
            )
            return len(bars), await self.repository.upsert_bars(bars)

        interval_seconds = BAR_SECONDS[self.settings.bar_size]
        requested_buckets = max(
            MIN_DEMO_BUCKETS if end_at is None else 1,
            duration_seconds(duration) // interval_seconds,
        )
        chunk_sizes = _demo_chunk_sizes(requested_buckets)
        if end_at is None:
            now_epoch = int(datetime.now(UTC).timestamp())
            next_epoch = ((now_epoch // interval_seconds) + 1) * interval_seconds
            chunk_end = datetime.fromtimestamp(next_epoch, tz=UTC)
        else:
            chunk_end = as_utc(end_at)
        received = 0
        written = 0

        for chunk_size in chunk_sizes:
            bars = await self.provider.fetch_bars(
                pair=self.settings.fx_pair,
                bar_size=self.settings.bar_size,
                duration=f"{chunk_size * interval_seconds} S",
                end_at=chunk_end,
            )
            received += len(bars)
            written += await self.repository.upsert_bars(bars)
            chunk_end -= timedelta(seconds=chunk_size * interval_seconds)

        return received, written


def _demo_chunk_sizes(bucket_count: int) -> list[int]:
    chunks: list[int] = []
    remaining = bucket_count
    while remaining > MAX_DEMO_BUCKETS:
        chunks.append(MAX_DEMO_BUCKETS)
        remaining -= MAX_DEMO_BUCKETS
    if remaining:
        chunks.append(remaining)

    return chunks
