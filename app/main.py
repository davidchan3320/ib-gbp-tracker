from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import Settings, get_settings
from app.db import Database
from app.domain import PRICE_TYPES, PriceType
from app.providers import build_provider
from app.schemas import (
    BarResponse,
    BarsEnvelope,
    ErrorResponse,
    MetricsResponse,
    StatusResponse,
    SyncResponse,
    SyncRunResponse,
)
from app.services.collector import Collector, SyncAlreadyRunningError
from app.services.metrics import calculate_metrics
from app.services.repository import BarRepository
from app.services.scheduler import CollectorScheduler

STATIC_DIR = Path(__file__).parent / "static"


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    database = Database(runtime_settings.database_url)
    repository = BarRepository(database.session_factory)
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
        try:
            if runtime_settings.scheduler_enabled:
                scheduler.start(run_immediately=runtime_settings.sync_on_startup)
            yield
        finally:
            await scheduler.stop()
            await provider.close()
            await database.dispose()

    app = FastAPI(
        title="FX Tape API",
        version="0.4.0",
        description="Collect and inspect one-minute GBP/USD bid, ask, and midpoint OHLC bars.",
        lifespan=lifespan,
    )
    app.state.settings = runtime_settings
    app.state.database = database
    app.state.repository = repository
    app.state.collector = collector

    @app.get("/healthz", tags=["system"])
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/v1/bars", response_model=BarsEnvelope, tags=["market data"])
    async def get_bars(
        price_type: PriceType = PriceType.MIDPOINT,
        limit: int = Query(default=1_440, ge=1, le=10_000),
    ) -> BarsEnvelope:
        rows = await repository.list_bars(price_type=price_type, limit=limit)
        return BarsEnvelope(
            pair=runtime_settings.display_pair,
            bar_size=runtime_settings.bar_size,
            price_type=price_type,
            count=len(rows),
            bars=[BarResponse.model_validate(row) for row in rows],
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
