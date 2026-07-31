import csv
import logging
import math
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

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

MIN_DEMO_BUCKETS = 80
MAX_DEMO_BUCKETS = 50_000
DEMO_DAILY_DATA_PATH = Path(__file__).parents[1] / "demo_data" / "daily.csv"
DAY_SECONDS = 24 * 60 * 60
HIGH_SECOND = 8 * 60 * 60
LOW_SECOND = 16 * 60 * 60

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class DemoDailyCandle:
    day: date
    open: Decimal
    close: Decimal
    high: Decimal
    low: Decimal
    is_complete: bool


def duration_seconds(value: str) -> int:
    match = re.fullmatch(r"\s*(\d+)\s*([SDWMY])\s*", value.upper())
    if not match:
        raise ValueError("duration must use IB format, for example '30 D' or '1 Y'")
    amount, unit = match.groups()
    return int(amount) * DURATION_SECONDS[unit]


class DemoHistoricalDataProvider(HistoricalDataProvider):
    """Replay the bundled GBP/USD daily dataset as deterministic intraday bars."""

    name = "demo"

    def __init__(
        self,
        data_path: Path | None = None,
    ) -> None:
        self.data_path = data_path or DEMO_DAILY_DATA_PATH
        self.daily_candles, self.corrected_ohlc_rows = self._load_daily_candles(self.data_path)
        self.source_anchor_day = max(self.daily_candles)
        complete_rows = sum(candle.is_complete for candle in self.daily_candles.values())
        logger.info(
            "Loaded CSV demo data: path=%s complete_days=%s corrected_ohlc_rows=%s",
            self.data_path,
            complete_rows,
            self.corrected_ohlc_rows,
        )

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
        requested_count = max(1, duration_seconds(duration) // interval)
        count = min(MAX_DEMO_BUCKETS, requested_count)
        if end_at is None:
            count = max(MIN_DEMO_BUCKETS, count)
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
            timestamp = datetime.fromtimestamp(epoch, tz=UTC)
            candle = self._candle_for_day(timestamp.date())
            if candle is None:
                continue

            normalized_candle = self._candle_for_pair(candle, pair)
            open_price, close_price, high, low = self._intraday_ohlc(
                normalized_candle,
                timestamp=timestamp,
                interval_seconds=interval,
            )
            weighted_average_price = (open_price + high + low + close_price) / Decimal(4)
            slot = epoch // interval
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
                        timestamp=timestamp,
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

    def _candle_for_day(self, candle_day: date) -> DemoDailyCandle | None:
        source_candle = self.daily_candles.get(candle_day)
        if source_candle is not None:
            return source_candle
        if candle_day <= self.source_anchor_day or candle_day.weekday() >= 5:
            return None

        anchor = self.daily_candles[self.source_anchor_day]
        offset = (candle_day - self.source_anchor_day).days
        open_price = anchor.close + self._continuation_movement(offset - 1)
        close = anchor.close + self._continuation_movement(offset)
        high = max(open_price, close) + Decimal("0.0038")
        low = min(open_price, close) - Decimal("0.0032")
        return DemoDailyCandle(
            day=candle_day,
            open=open_price,
            close=close,
            high=high,
            low=low,
            is_complete=False,
        )

    @staticmethod
    def _continuation_movement(offset: int) -> Decimal:
        movement = 0.006 * math.sin(offset / 7.0) + 0.003 * math.sin(offset / 29.0)
        return Decimal(str(movement))

    @classmethod
    def _load_daily_candles(
        cls,
        data_path: Path,
    ) -> tuple[dict[date, DemoDailyCandle], int]:
        candles: dict[date, DemoDailyCandle] = {}
        corrected_rows = 0
        with data_path.open(newline="", encoding="utf-8-sig") as source:
            for row_number, row in enumerate(csv.DictReader(source), start=2):
                try:
                    candle_day = date.fromisoformat(row["date"].strip())
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        f"Invalid demo CSV date at {data_path}:{row_number}"
                    ) from exc

                values = {
                    field: cls._optional_decimal(row.get(field))
                    for field in ("open", "close", "high", "low")
                }
                if all(value is not None for value in values.values()):
                    open_price = values["open"]
                    close_price = values["close"]
                    source_high = values["high"]
                    source_low = values["low"]
                    assert open_price is not None
                    assert close_price is not None
                    assert source_high is not None
                    assert source_low is not None
                    high = max(source_high, open_price, close_price)
                    low = min(source_low, open_price, close_price)
                    corrected_rows += high != source_high or low != source_low
                    candle = DemoDailyCandle(
                        day=candle_day,
                        open=open_price,
                        close=close_price,
                        high=high,
                        low=low,
                        is_complete=True,
                    )
                else:
                    partial_open = next(
                        (
                            value
                            for value in (
                                cls._optional_decimal(row.get("openForward")),
                                cls._optional_decimal(row.get("openNow")),
                                cls._optional_decimal(row.get("OpenM1")),
                            )
                            if value is not None
                        ),
                        None,
                    )
                    if partial_open is None:
                        continue
                    candle = cls._partial_candle(candle_day, partial_open)

                if candle_day in candles:
                    raise ValueError(f"Duplicate demo CSV date at {data_path}:{row_number}")
                candles[candle_day] = candle

        if not candles:
            raise ValueError(f"Demo CSV contains no usable OHLC rows: {data_path}")
        return candles, corrected_rows

    @staticmethod
    def _partial_candle(candle_day: date, open_price: Decimal) -> DemoDailyCandle:
        movement = Decimal(str(math.sin(candle_day.toordinal() / 13.0))) * Decimal("0.0035")
        close = open_price + movement
        high = max(open_price, close) + Decimal("0.0038")
        low = min(open_price, close) - Decimal("0.0032")
        return DemoDailyCandle(
            day=candle_day,
            open=open_price,
            close=close,
            high=high,
            low=low,
            is_complete=False,
        )

    @staticmethod
    def _optional_decimal(value: str | None) -> Decimal | None:
        if value is None or not value.strip():
            return None
        parsed = Decimal(value.strip())
        if not parsed.is_finite() or parsed <= 0:
            raise ValueError(f"Invalid demo CSV price: {value!r}")
        return parsed

    @staticmethod
    def _candle_for_pair(candle: DemoDailyCandle, pair: str) -> DemoDailyCandle:
        if pair == "GBPUSD":
            return candle
        if pair == "USDGBP":
            return DemoDailyCandle(
                day=candle.day,
                open=Decimal(1) / candle.open,
                close=Decimal(1) / candle.close,
                high=Decimal(1) / candle.low,
                low=Decimal(1) / candle.high,
                is_complete=candle.is_complete,
            )
        raise ValueError("Bundled demo data supports only GBPUSD or USDGBP")

    @classmethod
    def _intraday_ohlc(
        cls,
        candle: DemoDailyCandle,
        *,
        timestamp: datetime,
        interval_seconds: int,
    ) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        if interval_seconds >= DAY_SECONDS:
            return candle.open, candle.close, candle.high, candle.low

        start_second = timestamp.hour * 3600 + timestamp.minute * 60 + timestamp.second
        end_second = min(DAY_SECONDS, start_second + interval_seconds)
        open_price = cls._intraday_price(candle, start_second)
        close_price = cls._intraday_price(candle, end_second)
        prices = [open_price, close_price]
        if start_second <= HIGH_SECOND <= end_second:
            prices.append(candle.high)
        if start_second <= LOW_SECOND <= end_second:
            prices.append(candle.low)
        return open_price, close_price, max(prices), min(prices)

    @staticmethod
    def _intraday_price(candle: DemoDailyCandle, second: int) -> Decimal:
        if second <= HIGH_SECOND:
            start_second, end_second = 0, HIGH_SECOND
            start_price, end_price = candle.open, candle.high
        elif second <= LOW_SECOND:
            start_second, end_second = HIGH_SECOND, LOW_SECOND
            start_price, end_price = candle.high, candle.low
        else:
            start_second, end_second = LOW_SECOND, DAY_SECONDS
            start_price, end_price = candle.low, candle.close
        progress = Decimal(second - start_second) / Decimal(end_second - start_second)
        return start_price + (end_price - start_price) * progress

    @staticmethod
    def _quantize(value: Decimal) -> Decimal:
        return value.quantize(Decimal("0.00000001"))


def bar_delta(bar_size: str) -> timedelta:
    return timedelta(seconds=BAR_SECONDS[bar_size])
