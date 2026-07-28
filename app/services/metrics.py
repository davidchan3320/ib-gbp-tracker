import math
import statistics
from dataclasses import dataclass
from datetime import timedelta

from app.models import OHLCBar
from app.providers.demo import bar_delta

PERIODS_PER_YEAR = {
    "1 min": 252 * 24 * 60,
    "5 mins": 252 * 24 * 12,
    "15 mins": 252 * 24 * 4,
    "1 hour": 252 * 24,
    "4 hours": 252 * 6,
    "1 day": 252,
}


@dataclass(frozen=True, slots=True)
class MetricSnapshot:
    latest_close: float
    change_24h_pct: float | None
    high_24h: float
    low_24h: float
    sma_20: float | None
    atr_14: float | None
    realized_volatility_20_pct: float | None


@dataclass(frozen=True, slots=True)
class PeriodMetricSnapshot:
    open: float
    close: float
    high: float
    low: float
    average_open_close: float
    average_high_low: float


def calculate_period_metrics(
    *,
    open_price: float,
    close_price: float,
    high_price: float,
    low_price: float,
) -> PeriodMetricSnapshot:
    """Calculate derived values shared by daily, monthly, and yearly OHLC metrics."""
    return PeriodMetricSnapshot(
        open=open_price,
        close=close_price,
        high=high_price,
        low=low_price,
        average_open_close=(open_price + close_price) / 2,
        average_high_low=(high_price + low_price) / 2,
    )


def calculate_daily_metrics(bars: list[OHLCBar]) -> PeriodMetricSnapshot | None:
    """Calculate OHLC and midpoint-of-extremes values for one calendar day."""
    if not bars:
        return None

    first_bar = min(bars, key=lambda bar: bar.timestamp)
    last_bar = max(bars, key=lambda bar: bar.timestamp)
    daily_open = float(first_bar.open)
    daily_close = float(last_bar.close)
    daily_high = max(float(bar.high) for bar in bars)
    daily_low = min(float(bar.low) for bar in bars)

    return calculate_period_metrics(
        open_price=daily_open,
        close_price=daily_close,
        high_price=daily_high,
        low_price=daily_low,
    )


def calculate_metrics(bars: list[OHLCBar], bar_size: str) -> MetricSnapshot | None:
    """Calculate a compact metric set. Add calculators here as the product grows."""
    if not bars:
        return None

    closes = [float(bar.close) for bar in bars]
    latest_timestamp = bars[-1].timestamp
    window_start = latest_timestamp - timedelta(hours=24)
    day_bars = [bar for bar in bars if bar.timestamp >= window_start]
    if not day_bars:
        day_bars = bars[-1:]

    baseline_bar = next(
        (bar for bar in reversed(bars) if bar.timestamp <= window_start),
        bars[0] if len(bars) > 1 else None,
    )
    change_24h = None
    if baseline_bar is not None and float(baseline_bar.close) != 0:
        change_24h = ((closes[-1] / float(baseline_bar.close)) - 1) * 100

    sma_20 = statistics.fmean(closes[-20:]) if len(closes) >= 20 else None
    atr_14 = _average_true_range(bars, 14)
    volatility = _realized_volatility(closes, bar_size, 20)

    return MetricSnapshot(
        latest_close=closes[-1],
        change_24h_pct=change_24h,
        high_24h=max(float(bar.high) for bar in day_bars),
        low_24h=min(float(bar.low) for bar in day_bars),
        sma_20=sma_20,
        atr_14=atr_14,
        realized_volatility_20_pct=volatility,
    )


def _average_true_range(bars: list[OHLCBar], period: int) -> float | None:
    if len(bars) < period + 1:
        return None
    ranges: list[float] = []
    selected = bars[-(period + 1) :]
    for previous, current in zip(selected, selected[1:], strict=False):
        ranges.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    return statistics.fmean(ranges)


def _realized_volatility(closes: list[float], bar_size: str, period: int) -> float | None:
    if len(closes) < period + 1:
        return None
    selected = closes[-(period + 1) :]
    returns = [
        math.log(current / previous)
        for previous, current in zip(selected, selected[1:], strict=False)
    ]
    if len(returns) < 2:
        return None
    annualized = statistics.stdev(returns) * math.sqrt(PERIODS_PER_YEAR[bar_size])
    return annualized * 100


def expected_next_bar(timestamp, bar_size: str):
    return timestamp + bar_delta(bar_size)
