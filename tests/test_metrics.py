from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.domain import PriceType
from app.models import OHLCBar
from app.services.metrics import calculate_metrics


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
