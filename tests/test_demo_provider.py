from collections import defaultdict

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
