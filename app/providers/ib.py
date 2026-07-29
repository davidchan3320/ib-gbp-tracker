import asyncio
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from app.config import Settings
from app.domain import PriceType
from app.providers.base import HistoricalDataProvider, PriceBar

IB_PRICE_TYPES = {
    PriceType.BID: "BID",
    PriceType.ASK: "ASK",
    PriceType.MIDPOINT: "MIDPOINT",
}


class IBConnectionError(RuntimeError):
    pass


class IBHistoricalDataProvider(HistoricalDataProvider):
    """Historical bid, ask, and midpoint data from the IB Gateway socket API."""

    name = "ib"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._request_lock = asyncio.Lock()
        self._client: Any | None = None
        self._contracts: dict[str, Any] = {}

    async def fetch_bars(
        self,
        *,
        pair: str,
        bar_size: str,
        duration: str,
        end_at: datetime | None = None,
        allow_empty: bool = False,
    ) -> list[PriceBar]:
        async with self._request_lock:
            try:
                client, contract = await self._connected_contract(pair)
                results: dict[PriceType, list[Any]] = {}
                for price_type, what_to_show in IB_PRICE_TYPES.items():
                    results[price_type] = list(
                        await client.reqHistoricalDataAsync(
                            contract,
                            endDateTime=end_at or "",
                            durationStr=duration,
                            barSizeSetting=bar_size,
                            whatToShow=what_to_show,
                            useRTH=False,
                            formatDate=2,
                            keepUpToDate=False,
                            timeout=self.settings.ib_timeout_seconds,
                        )
                    )

                populated = {price_type for price_type, rows in results.items() if rows}
                if populated and len(populated) != len(IB_PRICE_TYPES):
                    missing = ", ".join(
                        price_type.value
                        for price_type in IB_PRICE_TYPES
                        if price_type not in populated
                    )
                    raise RuntimeError(f"IB returned incomplete historical data; missing {missing}")
                if not populated:
                    if allow_empty:
                        return []
                    raise RuntimeError(
                        "IB returned no historical bars. Check the contract and market-data "
                        "permissions."
                    )

                bars = [
                    self._to_price_bar(bar, price_type)
                    for price_type, rows in results.items()
                    for bar in rows
                ]
                return sorted(bars, key=lambda bar: (bar.timestamp, bar.price_type.value))
            except IBConnectionError:
                raise
            except Exception as exc:
                raise RuntimeError(f"IB historical data request failed: {exc}") from exc

    async def fetch_daily_bar(
        self,
        *,
        pair: str,
        day: date,
        price_type: PriceType,
    ) -> PriceBar | None:
        """Fetch the daily bar whose IB session date matches ``day``."""
        if day == date.max:
            raise ValueError("day must be earlier than 9999-12-31")

        end_at = datetime.combine(day + timedelta(days=1), time.min, tzinfo=UTC)
        async with self._request_lock:
            try:
                client, contract = await self._connected_contract(pair)
                rows = list(
                    await client.reqHistoricalDataAsync(
                        contract,
                        endDateTime=end_at,
                        durationStr="2 D",
                        barSizeSetting="1 day",
                        whatToShow=IB_PRICE_TYPES[price_type],
                        useRTH=False,
                        formatDate=2,
                        keepUpToDate=False,
                        timeout=self.settings.ib_timeout_seconds,
                    )
                )
                matching = [
                    self._to_price_bar(row, price_type)
                    for row in rows
                    if self._timestamp_utc(row.date).date() == day
                ]
                return matching[-1] if matching else None
            except IBConnectionError:
                raise
            except Exception as exc:
                raise RuntimeError(f"IB daily historical data request failed: {exc}") from exc

    async def fetch_weekly_bar(
        self,
        *,
        pair: str,
        week_start: date,
        price_type: PriceType,
    ) -> PriceBar | None:
        """Fetch the weekly bar whose IB date belongs to the requested ISO week."""
        if week_start.isoweekday() != 1:
            raise ValueError("week_start must be a Monday")
        try:
            end_day = week_start + timedelta(days=7)
        except OverflowError as exc:
            raise ValueError("week_start must have a following ISO week") from exc

        end_at = datetime.combine(end_day, time.min, tzinfo=UTC)
        target_week = week_start.isocalendar()[:2]
        async with self._request_lock:
            try:
                client, contract = await self._connected_contract(pair)
                rows = list(
                    await client.reqHistoricalDataAsync(
                        contract,
                        endDateTime=end_at,
                        durationStr="2 W",
                        barSizeSetting="1 week",
                        whatToShow=IB_PRICE_TYPES[price_type],
                        useRTH=False,
                        formatDate=2,
                        keepUpToDate=False,
                        timeout=self.settings.ib_timeout_seconds,
                    )
                )
                matching = [
                    self._to_price_bar(row, price_type)
                    for row in rows
                    if self._timestamp_utc(row.date).date().isocalendar()[:2] == target_week
                ]
                return matching[-1] if matching else None
            except IBConnectionError:
                raise
            except Exception as exc:
                raise RuntimeError(f"IB weekly historical data request failed: {exc}") from exc

    async def fetch_monthly_bar(
        self,
        *,
        pair: str,
        month_start: date,
        price_type: PriceType,
    ) -> PriceBar | None:
        """Fetch the monthly bar whose IB date belongs to the requested month."""
        if month_start.day != 1:
            raise ValueError("month_start must be the first day of a month")
        if month_start.year == 9999 and month_start.month == 12:
            raise ValueError("month_start must have a following month")

        if month_start.month == 12:
            end_day = month_start.replace(year=month_start.year + 1, month=1)
        else:
            end_day = month_start.replace(month=month_start.month + 1)

        end_at = datetime.combine(end_day, time.min, tzinfo=UTC)
        target_month = (month_start.year, month_start.month)
        async with self._request_lock:
            try:
                client, contract = await self._connected_contract(pair)
                rows = list(
                    await client.reqHistoricalDataAsync(
                        contract,
                        endDateTime=end_at,
                        durationStr="2 M",
                        barSizeSetting="1 month",
                        whatToShow=IB_PRICE_TYPES[price_type],
                        useRTH=False,
                        formatDate=2,
                        keepUpToDate=False,
                        timeout=self.settings.ib_timeout_seconds,
                    )
                )
                matching = []
                for row in rows:
                    bar_day = self._timestamp_utc(row.date).date()
                    if (bar_day.year, bar_day.month) == target_month:
                        matching.append(self._to_price_bar(row, price_type))
                return matching[-1] if matching else None
            except IBConnectionError:
                raise
            except Exception as exc:
                raise RuntimeError(f"IB monthly historical data request failed: {exc}") from exc

    async def _connected_contract(self, pair: str) -> tuple[Any, Any]:
        # Import lazily so demo mode stays usable even if the Gateway stack changes.
        from ib_async import IB, Forex

        if self._client is None:
            self._client = IB()
        client = self._client
        if not client.isConnected():
            self._contracts.clear()
            try:
                await client.connectAsync(
                    self.settings.ib_host,
                    self.settings.ib_port,
                    clientId=self.settings.ib_client_id,
                    timeout=self.settings.ib_timeout_seconds,
                    readonly=True,
                )
            except (ConnectionError, OSError, TimeoutError) as exc:
                raise IBConnectionError(
                    "Could not connect to IB Gateway. Check that it is running, API socket "
                    "access is enabled, and the host/port match your session."
                ) from exc

        if pair not in self._contracts:
            contract = Forex(pair)
            qualified = await client.qualifyContractsAsync(contract)
            if not qualified or qualified[0] is None:
                raise IBConnectionError(f"IB could not qualify the forex contract {pair}")
            self._contracts[pair] = qualified[0]
        return client, self._contracts[pair]

    async def close(self) -> None:
        async with self._request_lock:
            if self._client is not None and self._client.isConnected():
                self._client.disconnect()
            self._client = None
            self._contracts.clear()

    @classmethod
    def _to_price_bar(cls, bar: Any, price_type: PriceType) -> PriceBar:
        return PriceBar(
            price_type=price_type,
            timestamp=cls._timestamp_utc(bar.date),
            open=Decimal(str(bar.open)),
            high=Decimal(str(bar.high)),
            low=Decimal(str(bar.low)),
            close=Decimal(str(bar.close)),
            volume=cls._optional_decimal(getattr(bar, "volume", None)),
            weighted_average_price=cls._optional_decimal(
                getattr(bar, "average", None), positive=True
            ),
            trade_count=cls._optional_count(getattr(bar, "barCount", None)),
        )

    @staticmethod
    def _timestamp_utc(value: datetime | date | str) -> datetime:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                return value.replace(tzinfo=UTC)
            return value.astimezone(UTC)
        if isinstance(value, date):
            return datetime.combine(value, time.min, tzinfo=UTC)

        raw = str(value).strip()
        for pattern in ("%Y%m%d", "%Y%m%d %H:%M:%S", "%Y%m%d-%H:%M:%S"):
            try:
                return datetime.strptime(raw, pattern).replace(tzinfo=UTC)
            except ValueError:
                continue
        parsed = datetime.fromisoformat(raw)
        return parsed.replace(tzinfo=parsed.tzinfo or UTC).astimezone(UTC)

    @staticmethod
    def _optional_decimal(value: Any, *, positive: bool = False) -> Decimal | None:
        if value is None:
            return None
        try:
            number = Decimal(str(value))
        except Exception:
            return None
        minimum_is_valid = number > 0 if positive else number >= 0
        return number if number.is_finite() and minimum_is_valid else None

    @staticmethod
    def _optional_count(value: Any) -> int | None:
        try:
            count = int(value)
        except (TypeError, ValueError, OverflowError):
            return None
        return count if count >= 0 else None
