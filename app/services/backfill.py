import asyncio
import contextlib
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, time, timedelta

from app.config import Settings
from app.models import BackfillCheckpoint
from app.providers.base import HistoricalDataProvider, PriceBar
from app.services.repository import BarRepository, as_utc

logger = logging.getLogger(__name__)

BACKFILL_BAR_SIZE = "1 min"
BACKFILL_DURATION = "1 D"
BACKFILL_CHUNK = timedelta(days=1)


class BackfillAlreadyRunningError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BackfillState:
    checkpoint_key: str
    status: str
    start_at: datetime
    end_at: datetime | None
    cursor: datetime | None
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime | None
    total_chunks: int
    chunks_completed: int
    bars_received: int
    bars_written: int
    message: str | None

    @property
    def progress_pct(self) -> float:
        if self.total_chunks == 0:
            return 100.0 if self.status == "succeeded" else 0.0
        return min(100.0, (self.chunks_completed / self.total_chunks) * 100)


class BackfillManager:
    """Resumable one-minute backfill that advances a durable checkpoint backwards."""

    def __init__(
        self,
        *,
        settings: Settings,
        provider: HistoricalDataProvider,
        repository: BarRepository,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.repository = repository
        self._now = now or (lambda: datetime.now(UTC))
        self._task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()

        start_at = datetime.combine(settings.backfill_start, time.min, tzinfo=UTC)
        checkpoint_key = ":".join(
            (
                provider.name,
                settings.fx_pair.lower(),
                "1_min",
                "bid_ask_midpoint",
                settings.backfill_start.isoformat(),
            )
        )
        self._state = BackfillState(
            checkpoint_key=checkpoint_key,
            status="idle",
            start_at=start_at,
            end_at=None,
            cursor=None,
            started_at=None,
            completed_at=None,
            updated_at=None,
            total_chunks=0,
            chunks_completed=0,
            bars_received=0,
            bars_written=0,
            message=None,
        )

    @property
    def state(self) -> BackfillState:
        return self._state

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def initialize(self) -> BackfillState:
        checkpoint = await self.repository.get_backfill_checkpoint(self._state.checkpoint_key)
        if checkpoint is None:
            return self._state

        self._state = self._from_checkpoint(checkpoint)
        return self._state

    async def start(self, *, restart: bool = False) -> BackfillState:
        async with self._lifecycle_lock:
            if self.is_running:
                raise BackfillAlreadyRunningError("The historical backfill is already running")
            if self.settings.bar_size != BACKFILL_BAR_SIZE:
                raise RuntimeError("Historical backfill requires BAR_SIZE='1 min'")

            checkpoint = await self.repository.get_backfill_checkpoint(self._state.checkpoint_key)
            now = self._utc_now().replace(second=0, microsecond=0)
            if checkpoint is None or restart:
                total_chunks = self._chunk_count(self._state.start_at, now)
                state = BackfillState(
                    checkpoint_key=self._state.checkpoint_key,
                    status="running",
                    start_at=self._state.start_at,
                    end_at=now,
                    cursor=now,
                    started_at=self._utc_now(),
                    completed_at=None,
                    updated_at=self._utc_now(),
                    total_chunks=total_chunks,
                    chunks_completed=0,
                    bars_received=0,
                    bars_written=0,
                    message=None,
                )
            else:
                state = replace(
                    self._from_checkpoint(checkpoint),
                    status="running",
                    completed_at=None,
                    updated_at=self._utc_now(),
                    message=None,
                )

            self._state = state
            if state.cursor is None or state.cursor <= state.start_at:
                await self._finish("succeeded", None)
                return self._state

            await self._persist()
            self._task = asyncio.create_task(
                self._run(),
                name="one-minute-history-backfill",
            )
            return self._state

    async def stop(self) -> BackfillState:
        async with self._lifecycle_lock:
            task = self._task
            if task is None or task.done():
                return self._state
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._task = None
            return self._state

    async def wait(self) -> BackfillState:
        task = self._task
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        return self._state

    async def _run(self) -> None:
        try:
            while self._state.cursor is not None and self._state.cursor > self._state.start_at:
                chunk_end = self._state.cursor
                chunk_start = max(self._state.start_at, chunk_end - BACKFILL_CHUNK)
                bars = await self._fetch_with_retry(chunk_end)
                bounded_bars = [
                    bar for bar in bars if chunk_start <= as_utc(bar.timestamp) < chunk_end
                ]
                written = await self.repository.upsert_bars(bounded_bars)
                self._state = replace(
                    self._state,
                    cursor=chunk_start,
                    updated_at=self._utc_now(),
                    chunks_completed=self._state.chunks_completed + 1,
                    bars_received=self._state.bars_received + len(bounded_bars),
                    bars_written=self._state.bars_written + written,
                    message=None,
                )
                await self._persist()

                if chunk_start > self._state.start_at:
                    await asyncio.sleep(self.settings.backfill_request_delay_seconds)

            await self._finish("succeeded", None)
        except asyncio.CancelledError:
            await self._finish("cancelled", "Backfill cancelled; its checkpoint can be resumed.")
            raise
        except Exception as exc:
            logger.exception("Historical backfill failed")
            await self._finish("failed", str(exc)[:1_000])

    async def _fetch_with_retry(self, chunk_end: datetime) -> list[PriceBar]:
        for attempt in range(self.settings.backfill_max_retries + 1):
            try:
                return await self.provider.fetch_bars(
                    pair=self.settings.fx_pair,
                    bar_size=BACKFILL_BAR_SIZE,
                    duration=BACKFILL_DURATION,
                    end_at=chunk_end,
                    allow_empty=True,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if attempt >= self.settings.backfill_max_retries:
                    raise
                delay = self.settings.backfill_retry_delay_seconds * (2**attempt)
                self._state = replace(
                    self._state,
                    updated_at=self._utc_now(),
                    message=(
                        f"Request failed; retry {attempt + 1}/"
                        f"{self.settings.backfill_max_retries} in {delay:g}s: {exc}"
                    )[:1_000],
                )
                await self._persist()
                await asyncio.sleep(delay)
        return []

    async def _finish(self, status: str, message: str | None) -> None:
        self._state = replace(
            self._state,
            status=status,
            completed_at=self._utc_now(),
            updated_at=self._utc_now(),
            message=message,
        )
        await asyncio.shield(self._persist())

    async def _persist(self) -> None:
        state = self._state
        if (
            state.end_at is None
            or state.cursor is None
            or state.started_at is None
            or state.updated_at is None
        ):
            return
        await self.repository.save_backfill_checkpoint(
            BackfillCheckpoint(
                checkpoint_key=state.checkpoint_key,
                status=state.status,
                start_at=state.start_at,
                end_at=state.end_at,
                cursor=state.cursor,
                started_at=state.started_at,
                completed_at=state.completed_at,
                updated_at=state.updated_at,
                total_chunks=state.total_chunks,
                chunks_completed=state.chunks_completed,
                bars_received=state.bars_received,
                bars_written=state.bars_written,
                message=state.message,
            )
        )

    def _from_checkpoint(self, checkpoint: BackfillCheckpoint) -> BackfillState:
        return BackfillState(
            checkpoint_key=checkpoint.checkpoint_key,
            status=checkpoint.status,
            start_at=as_utc(checkpoint.start_at),
            end_at=as_utc(checkpoint.end_at),
            cursor=as_utc(checkpoint.cursor),
            started_at=as_utc(checkpoint.started_at),
            completed_at=(as_utc(checkpoint.completed_at) if checkpoint.completed_at else None),
            updated_at=as_utc(checkpoint.updated_at),
            total_chunks=checkpoint.total_chunks,
            chunks_completed=checkpoint.chunks_completed,
            bars_received=checkpoint.bars_received,
            bars_written=checkpoint.bars_written,
            message=checkpoint.message,
        )

    def _utc_now(self) -> datetime:
        return as_utc(self._now())

    @staticmethod
    def _chunk_count(start_at: datetime, end_at: datetime) -> int:
        seconds = max(0.0, (end_at - start_at).total_seconds())
        return math.ceil(seconds / BACKFILL_CHUNK.total_seconds())
