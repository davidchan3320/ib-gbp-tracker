import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.cli import (
    BackfillLockError,
    BackfillRunLock,
    _backfill_settings,
    run_backfill,
    show_backfill_status,
)
from app.config import Settings
from app.db import Database


async def test_cli_run_persists_a_checkpoint_and_status_reads_it(
    tmp_path: Path,
    capsys,
) -> None:
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'cli.db'}",
        scheduler_enabled=False,
        backfill_start=date.today() + timedelta(days=1),
        backfill_request_delay_seconds=0,
    )

    assert await run_backfill(settings, json_output=True) == 0
    completed = json.loads(capsys.readouterr().out)
    assert completed["status"] == "succeeded"
    assert completed["progress_pct"] == 100.0
    assert completed["chunks_completed"] == 0

    assert await show_backfill_status(settings, json_output=True) == 0
    persisted = json.loads(capsys.readouterr().out)
    assert persisted == completed


async def test_sqlite_backfill_lock_rejects_a_second_worker(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'lock.db'}")
    first = BackfillRunLock(database, "demo:usdgbp:1_min")
    second = BackfillRunLock(database, "demo:usdgbp:1_min")
    try:
        await first.acquire()
        with pytest.raises(BackfillLockError):
            await second.acquire()
    finally:
        await second.release()
        await first.release()
        await database.dispose()


def test_ib_cli_uses_a_dedicated_client_id() -> None:
    settings = Settings(
        data_provider="ib",
        ib_client_id=21,
        ib_backfill_client_id=22,
    )

    cli_settings = _backfill_settings(settings)

    assert cli_settings.ib_client_id == 22
    assert settings.ib_client_id == 21
