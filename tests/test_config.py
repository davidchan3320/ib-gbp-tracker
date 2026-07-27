import pytest
from pydantic import ValidationError

from app.config import Settings


def test_pair_is_normalized() -> None:
    settings = Settings(fx_pair="usd/gbp")
    assert settings.fx_pair == "USDGBP"
    assert settings.display_pair == "USD/GBP"


def test_one_minute_bars_are_the_default() -> None:
    settings = Settings()
    assert settings.bar_size == "1 min"
    assert settings.sync_interval_seconds == 60
    assert settings.backfill_start.isoformat() == "2017-01-01"
    assert settings.ib_backfill_client_id == 22


def test_invalid_bar_size_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(bar_size="7 minutes")
