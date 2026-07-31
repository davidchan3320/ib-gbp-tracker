from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain import PriceType
from app.providers.demo import DemoHistoricalDataProvider


async def test_demo_provider_returns_valid_chronological_bars() -> None:
    provider = DemoHistoricalDataProvider()
    bars = await provider.fetch_bars(pair="GBPUSD", bar_size="1 min", duration="3600 S")

    assert len(bars) == 80 * 3
    assert bars == sorted(bars, key=lambda bar: bar.timestamp)
    assert all(bar.low <= min(bar.open, bar.close) for bar in bars)
    assert all(bar.high >= max(bar.open, bar.close) for bar in bars)
    assert all(bar.volume is not None and bar.volume > 0 for bar in bars)
    assert all(
        bar.weighted_average_price is not None and bar.low <= bar.weighted_average_price <= bar.high
        for bar in bars
    )
    assert all(bar.trade_count is not None and bar.trade_count > 0 for bar in bars)
    assert all(bar.close > 1 for bar in bars)

    by_timestamp = defaultdict(dict)
    for bar in bars:
        by_timestamp[bar.timestamp][bar.price_type] = bar
    assert all(set(group) == set(PriceType) for group in by_timestamp.values())
    assert all(
        group[PriceType.BID].close < group[PriceType.MIDPOINT].close < group[PriceType.ASK].close
        for group in by_timestamp.values()
    )


async def test_demo_provider_can_generate_more_than_old_ten_thousand_bucket_limit() -> None:
    provider = DemoHistoricalDataProvider()
    end_at = datetime(2026, 7, 31, tzinfo=UTC)

    bars = await provider.fetch_bars(
        pair="GBPUSD",
        bar_size="1 min",
        duration="15 D",
        end_at=end_at,
    )

    timestamps = sorted({bar.timestamp for bar in bars})
    assert len(timestamps) > 10_000
    assert {timestamp.weekday() for timestamp in timestamps} <= {0, 1, 2, 3, 4}
    assert timestamps[-1] == end_at - timedelta(minutes=1)
    assert timestamps[0].date().isoformat() == "2026-07-16"


async def test_demo_provider_midpoint_aggregates_to_source_daily_ohlc() -> None:
    provider = DemoHistoricalDataProvider()

    bars = await provider.fetch_bars(
        pair="GBPUSD",
        bar_size="1 min",
        duration="1 D",
        end_at=datetime(2026, 7, 31, tzinfo=UTC),
    )

    midpoint = [bar for bar in bars if bar.price_type is PriceType.MIDPOINT]
    assert len(midpoint) == 24 * 60
    assert midpoint[0].open == Decimal("1.33530000")
    assert midpoint[-1].close == Decimal("1.34520000")
    assert max(bar.high for bar in midpoint) == Decimal("1.34770000")
    assert min(bar.low for bar in midpoint) == Decimal("1.33180000")
    assert provider.corrected_ohlc_rows == 14


async def test_demo_provider_honors_small_explicit_historical_window() -> None:
    provider = DemoHistoricalDataProvider()
    end_at = datetime(2026, 7, 31, tzinfo=UTC)

    bars = await provider.fetch_bars(
        pair="GBPUSD",
        bar_size="1 min",
        duration="60 S",
        end_at=end_at,
    )

    assert len(bars) == 3
    assert {bar.timestamp for bar in bars} == {end_at - timedelta(minutes=1)}


async def test_demo_provider_preserves_market_gaps_and_continues_after_source() -> None:
    provider = DemoHistoricalDataProvider()

    weekend = await provider.fetch_bars(
        pair="GBPUSD",
        bar_size="1 min",
        duration="2 D",
        end_at=datetime(2026, 8, 3, tzinfo=UTC),
    )
    continuation = await provider.fetch_bars(
        pair="GBPUSD",
        bar_size="1 hour",
        duration="1 D",
        end_at=datetime(2026, 8, 5, tzinfo=UTC),
    )

    assert weekend == []
    assert len(continuation) == 24 * 3
    assert {bar.timestamp.date().isoformat() for bar in continuation} == {"2026-08-04"}
    assert all(
        candle.low <= min(candle.open, candle.close)
        and candle.high >= max(candle.open, candle.close)
        for candle in provider.daily_candles.values()
    )
