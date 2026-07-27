import math
import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.domain import PriceType
from app.providers.base import HistoricalDataProvider, PriceBar

BAR_SECONDS = {
    "1 min": 60,
    "5 mins": 5 * 60,
    "15 mins": 15 * 60,
    "1 hour": 60 * 60,
    "4 hours": 4 * 60 * 60,
    "1 day": 24 * 60 * 60,
}

DURATION_SECONDS = {
    "S": 1,
    "D": 24 * 60 * 60,
    "W": 7 * 24 * 60 * 60,
    "M": 30 * 24 * 60 * 60,
    "Y": 365 * 24 * 60 * 60,
}


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([SDWMY])\s*", value.upper())
    if not match:
        raise ValueError("duration must use IB format, for example '30 D' or '1 Y'")
    amount, unit = match.groups()
    return int(amount) * DURATION_SECONDS[unit]


class DemoHistoricalDataProvider(HistoricalDataProvider):
    """Deterministic synthetic bars for local development and UI evaluation."""

    name = "demo"

    async def fetch_bars(
        self,
        *,
        pair: str,
        bar_size: str,
        duration: str,
        end_at: datetime | None = None,
        allow_empty: bool = False,
    ) -> list[PriceBar]:
        interval = BAR_SECONDS[bar_size]
        count = max(80, min(10_000, duration_seconds(duration) // interval))
        if end_at is None:
            end_epoch = int(datetime.now(UTC).timestamp())
            last_epoch = end_epoch - (end_epoch % interval)
        else:
            normalized_end = end_at.replace(tzinfo=end_at.tzinfo or UTC).astimezone(UTC)
            end_epoch = int(normalized_end.timestamp())
            last_epoch = ((end_epoch - 1) // interval) * interval
        first_epoch = last_epoch - ((count - 1) * interval)

        bars: list[PriceBar] = []
        for index in range(count):
            epoch = first_epoch + index * interval
            slot = epoch // interval
            open_price = self._price_at(slot - 1)
            close_price = self._price_at(slot)
            wick = Decimal(str(0.00028 + 0.00018 * abs(math.sin(slot / 3.7))))
            high = max(open_price, close_price) + wick
            low = min(open_price, close_price) - (wick * Decimal("0.82"))
            weighted_average_price = (open_price + high + low + close_price) / Decimal("4")
            trade_count = 140 + int(abs(math.sin(slot / 4.7)) * 260)
            half_spread = Decimal(str(0.000025 + 0.000015 * abs(math.cos(slot / 6.1))))
            for price_type, offset in (
                (PriceType.BID, -half_spread),
                (PriceType.ASK, half_spread),
                (PriceType.MIDPOINT, Decimal("0")),
            ):
                bars.append(
                    PriceBar(
                        price_type=price_type,
                        timestamp=datetime.fromtimestamp(epoch, tz=UTC),
                        open=self._quantize(open_price + offset),
                        high=self._quantize(high + offset),
                        low=self._quantize(low + offset),
                        close=self._quantize(close_price + offset),
                        volume=Decimal(trade_count) * Decimal("100000"),
                        weighted_average_price=self._quantize(weighted_average_price + offset),
                        trade_count=trade_count,
                    )
                )
        return bars

    @staticmethod
    def _price_at(slot: int) -> Decimal:
        # Multiple cycles make the demo feel market-like while remaining stable per timestamp.
        value = (
            0.7752
            + 0.0048 * math.sin(slot / 19.0)
            + 0.0017 * math.sin(slot / 5.3)
            + 0.0008 * math.cos(slot / 2.1)
        )
        return Decimal(str(value))

    @staticmethod
    def _quantize(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.00000001"))


def bar_delta(bar_size: str) -> timedelta:
    return timedelta(seconds=BAR_SECONDS[bar_size])
