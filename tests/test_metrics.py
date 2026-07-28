from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain import PriceType
from app.models import OHLCBar
from app.services.metrics import calculate_daily_metrics, calculate_metrics


def test_daily_metrics_use_first_open_last_close_and_daily_extremes() -> None:
    start = datetime(2026, 7, 27, tzinfo=UTC)
    bars = [
        OHLCBar(
            price_type=PriceType.MIDPOINT.value,
            timestamp=start + timedelta(minutes=2),
            open=Decimal("1.3200"),
            high=Decimal("1.3400"),
            low=Decimal("1.3100"),
            close=Decimal("1.3300"),
        ),
        OHLCBar(
            price_type=PriceType.MIDPOINT.value,
            timestamp=start,
            open=Decimal("1.3000"),
            high=Decimal("1.3250"),
            low=Decimal("1.2900"),
            close=Decimal("1.3200"),
        ),
        OHLCBar(
            price_type=PriceType.MIDPOINT.value,
            timestamp=start + timedelta(minutes=1),
            open=Decimal("1.3150"),
            high=Decimal("1.3500"),
            low=Decimal("1.2800"),
            close=Decimal("1.3250"),
        ),
    ]

    snapshot = calculate_daily_metrics(bars)

    assert snapshot is not None
    assert snapshot.open == pytest.approx(1.3)
    assert snapshot.close == pytest.approx(1.33)
    assert snapshot.high == pytest.approx(1.35)
    assert snapshot.low == pytest.approx(1.28)
    assert snapshot.average_open_close == pytest.approx(1.315)
    assert snapshot.average_high_low == pytest.approx(1.315)


def test_metric_snapshot_is_calculated_from_chronological_bars() -> None:
    start = datetime(2026, 7, 1, tzinfo=UTC)
    bars = []
    for index in range(30):
        price = Decimal("0.77000") + Decimal(index) * Decimal("0.00010")
        bars.append(
            OHLCBar(
                price_type=PriceType.MIDPOINT.value,
                timestamp=start + timedelta(hours=index),
                open=price,
                high=price + Decimal("0.00030"),
                low=price - Decimal("0.00020"),
                close=price + Decimal("0.00010"),
            )
        )

    snapshot = calculate_metrics(bars, "1 hour")

    assert snapshot is not None
    assert snapshot.latest_close == pytest.approx(0.773)
    assert snapshot.change_24h_pct is not None
    assert snapshot.change_24h_pct > 0
    assert snapshot.sma_20 is not None
    assert snapshot.atr_14 == pytest.approx(0.0005)
    assert snapshot.realized_volatility_20_pct is not None


def test_metrics_require_bars() -> None:
    assert calculate_metrics([], "1 hour") is None
    assert calculate_daily_metrics([]) is None
