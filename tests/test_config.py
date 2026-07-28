import pytest
from pydantic import ValidationError

from app.config import Settings


def test_pair_is_normalized() -> None:
    settings = Settings(fx_pair="gbp/usd")
    assert settings.fx_pair == "GBPUSD"
    assert settings.display_pair == "GBP/USD"


def test_one_minute_bars_are_the_default() -> None:
    settings = Settings()
    assert settings.fx_pair == "GBPUSD"
    assert settings.display_pair == "GBP/USD"
    assert settings.bar_size == "1 min"
    assert settings.sync_interval_seconds == 60
    assert settings.metrics_cache_ttl_seconds == 300
    assert settings.resolved_metrics_cache_backend == "sqlite"
    assert settings.resolved_metrics_cache_url == settings.database_url
    assert settings.backfill_start.isoformat() == "2017-01-01"
    assert settings.ib_backfill_client_id == 22


def test_invalid_bar_size_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(bar_size="7 minutes")


def test_invalid_metrics_cache_ttl_is_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(metrics_cache_ttl_seconds=-1)


def test_metric_cache_backend_aliases_and_urls_are_normalized() -> None:
    postgresql = Settings(
        metrics_cache_backend="pgsql",
        metrics_cache_url="postgresql://user:password@localhost/cache",
    )
    redis = Settings(metrics_cache_backend="redis")

    assert postgresql.resolved_metrics_cache_backend == "postgresql"
    assert postgresql.resolved_metrics_cache_url.startswith("postgresql+asyncpg://")
    assert redis.resolved_metrics_cache_url == "redis://127.0.0.1:6379/0"


def test_postgresql_cache_requires_url_when_source_database_is_sqlite() -> None:
    with pytest.raises(ValidationError):
        Settings(metrics_cache_backend="postgresql")


def test_metric_cache_url_must_match_backend() -> None:
    with pytest.raises(ValidationError):
        Settings(metrics_cache_backend="redis", metrics_cache_url="sqlite:///cache.db")
