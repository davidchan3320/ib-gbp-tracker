import asyncio
from datetime import UTC, date, datetime, time
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
