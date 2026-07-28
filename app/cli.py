import argparse
import asyncio
import fcntl
import hashlib
import json
import sys
import tempfile
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from app.config import Settings
from app.db import Database
from app.providers import build_provider
from app.services.backfill import BackfillManager, BackfillState
from app.services.cache import build_metric_cache
from app.services.repository import BarRepository


class BackfillLockError(RuntimeError):
    pass


class BackfillRunLock:
    """Prevent concurrent workers for the same database checkpoint."""

    def __init__(self, database: Database, checkpoint_key: str) -> None:
        self.database = database
        self.checkpoint_key = checkpoint_key
        self._connection: AsyncConnection | None = None
        self._lock_id: int | None = None
        self._file: IO[str] | None = None

    async def acquire(self) -> None:
        if self.database.engine.dialect.name == "postgresql":
            await self._acquire_postgresql()
            return
        self._acquire_file()

    async def release(self) -> None:
        if self._connection is not None:
            try:
                await self._connection.execute(
                    text("SELECT pg_advisory_unlock(:lock_id)"),
                    {"lock_id": self._lock_id},
                )
            finally:
                await self._connection.close()
                self._connection = None
                self._lock_id = None

        if self._file is not None:
            try:
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
            finally:
                self._file.close()
                self._file = None

    async def _acquire_postgresql(self) -> None:
        digest = hashlib.blake2b(
            self.checkpoint_key.encode("utf-8"),
            digest_size=8,
        ).digest()
        lock_id = int.from_bytes(digest, byteorder="big", signed=True)
        connection = await self.database.engine.connect()
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_lock(:lock_id)"),
            {"lock_id": lock_id},
        )
        if not acquired:
            await connection.close()
            raise BackfillLockError("Another backfill CLI is already running for this checkpoint")
        self._connection = connection
        self._lock_id = lock_id

    def _acquire_file(self) -> None:
        database_url = self.database.engine.url.render_as_string(hide_password=True)
        identity = f"{database_url}:{self.checkpoint_key}"
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        lock_path = Path(tempfile.gettempdir()) / f"fx-tape-backfill-{digest}.lock"
        lock_file = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            lock_file.close()
            raise BackfillLockError(
                "Another backfill CLI is already running for this checkpoint"
            ) from exc
        self._file = lock_file


def _backfill_settings(settings: Settings) -> Settings:
    if settings.data_provider != "ib":
        return settings
    return settings.model_copy(update={"ib_client_id": settings.ib_backfill_client_id})


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat().replace("+00:00", "Z")


def backfill_payload(state: BackfillState) -> dict[str, Any]:
    return {
        "checkpoint_key": state.checkpoint_key,
        "status": state.status,
        "start_at": _iso_or_none(state.start_at),
        "end_at": _iso_or_none(state.end_at),
        "cursor": _iso_or_none(state.cursor),
        "started_at": _iso_or_none(state.started_at),
        "completed_at": _iso_or_none(state.completed_at),
        "updated_at": _iso_or_none(state.updated_at),
        "total_chunks": state.total_chunks,
        "chunks_completed": state.chunks_completed,
        "progress_pct": round(state.progress_pct, 2),
        "bars_received": state.bars_received,
        "bars_written": state.bars_written,
        "message": state.message,
    }


def _print_state(state: BackfillState, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(backfill_payload(state), separators=(",", ":")))
        return

    print(f"Status: {state.status}")
    print(f"Checkpoint: {state.checkpoint_key}")
    print(
        "Progress: "
        f"{state.chunks_completed}/{state.total_chunks} chunks "
        f"({state.progress_pct:.2f}%)"
    )
    print(f"Cursor: {_iso_or_none(state.cursor) or '-'}")
    print(f"Bars: {state.bars_written} written / {state.bars_received} received")
    if state.message:
        print(f"Message: {state.message}")


async def show_backfill_status(settings: Settings, *, json_output: bool = False) -> int:
    runtime_settings = _backfill_settings(settings)
    database = Database(runtime_settings.database_url)
    provider = build_provider(runtime_settings)
    repository = BarRepository(database.session_factory)
    manager = BackfillManager(
        settings=runtime_settings,
        provider=provider,
        repository=repository,
    )
    try:
        await database.create_schema()
        state = await manager.initialize()
        _print_state(state, json_output=json_output)
        return 0
    finally:
        await provider.close()
        await database.dispose()


async def run_backfill(
    settings: Settings,
    *,
    restart: bool = False,
    json_output: bool = False,
) -> int:
    runtime_settings = _backfill_settings(settings)
    database = Database(runtime_settings.database_url)
    metric_cache = build_metric_cache(runtime_settings, database.session_factory)
    provider = build_provider(runtime_settings)
    repository = BarRepository(
        database.session_factory,
        metric_cache=metric_cache,
        cache_ttl_seconds=runtime_settings.metrics_cache_ttl_seconds,
    )
    manager = BackfillManager(
        settings=runtime_settings,
        provider=provider,
        repository=repository,
    )
    run_lock = BackfillRunLock(database, manager.state.checkpoint_key)
    interrupted = False

    try:
        await database.create_schema()
        await metric_cache.initialize()
        await run_lock.acquire()
        await manager.initialize()
        state = await manager.start(restart=restart)
        last_reported_chunks = -1

        while manager.is_running:
            state = manager.state
            if not json_output and state.chunks_completed != last_reported_chunks:
                print(
                    f"{state.chunks_completed}/{state.total_chunks} chunks "
                    f"({state.progress_pct:.2f}%), cursor={_iso_or_none(state.cursor)}, "
                    f"bars={state.bars_written}",
                    flush=True,
                )
                last_reported_chunks = state.chunks_completed
            await asyncio.sleep(0.5)

        state = await manager.wait()
        _print_state(state, json_output=json_output)
        return 0 if state.status == "succeeded" else 1
    except asyncio.CancelledError:
        interrupted = True
        await manager.stop()
        raise
    finally:
        if not interrupted:
            await manager.stop()
        await provider.close()
        await run_lock.release()
        await metric_cache.close()
        await database.dispose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="fx-tape",
        description="FX Tape database and historical-data commands",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    backfill = commands.add_parser("backfill", help="Manage the resumable one-minute backfill")
    backfill_commands = backfill.add_subparsers(dest="backfill_command", required=True)

    run = backfill_commands.add_parser("run", help="Start or resume the backfill")
    run.add_argument(
        "--restart",
        action="store_true",
        help="Discard progress and restart from the current minute",
    )
    run.add_argument("--json", action="store_true", help="Print the final state as JSON")

    status = backfill_commands.add_parser("status", help="Show the durable checkpoint")
    status.add_argument("--json", action="store_true", help="Print the state as JSON")
    return parser


def cli(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings()
    try:
        if args.backfill_command == "run":
            return asyncio.run(
                run_backfill(
                    settings,
                    restart=args.restart,
                    json_output=args.json,
                )
            )
        return asyncio.run(show_backfill_status(settings, json_output=args.json))
    except BackfillLockError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Backfill interrupted; the durable checkpoint was preserved.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(cli())


if __name__ == "__main__":
    main()
