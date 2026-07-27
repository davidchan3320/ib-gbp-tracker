import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from app.config import Settings
from app.providers.base import HistoricalDataProvider
from app.services.repository import BarRepository

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
            latest = await self.repository.latest_timestamp()
            duration = (
                INCREMENTAL_DURATIONS[self.settings.bar_size]
                if latest is not None
                else self.settings.history_duration
            )
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
                bars = await self.provider.fetch_bars(
                    pair=self.settings.fx_pair,
                    bar_size=self.settings.bar_size,
                    duration=duration,
                )
                written = await self.repository.upsert_bars(bars)
                self._last_run = SyncRunState(
                    id=run_id,
                    provider=self.provider.name,
                    status="succeeded",
                    started_at=started_at,
                    completed_at=datetime.now(UTC),
                    bars_received=len(bars),
                    bars_written=written,
                )
                return SyncResult(
                    run_id=run_id,
                    status="succeeded",
                    bars_received=len(bars),
                    bars_written=written,
                    started_from=latest,
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
