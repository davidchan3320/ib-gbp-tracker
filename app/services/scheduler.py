import asyncio
import contextlib
import logging

from app.services.collector import Collector, SyncAlreadyRunningError

logger = logging.getLogger(__name__)


class CollectorScheduler:
    def __init__(self, collector: Collector, interval_seconds: int) -> None:
        self.collector = collector
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self, *, run_immediately: bool) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(
            self._run(run_immediately=run_immediately),
            name="ohlc-collector-scheduler",
        )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    async def _run(self, *, run_immediately: bool) -> None:
        if run_immediately:
            await self._sync_safely()
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                await self._sync_safely()

    async def _sync_safely(self) -> None:
        try:
            await self.collector.sync()
        except SyncAlreadyRunningError:
            logger.info("Skipping scheduled sync because another sync is running")
        except Exception:
            logger.exception("Scheduled OHLC sync failed")
