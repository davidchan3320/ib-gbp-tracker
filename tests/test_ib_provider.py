from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import ib_async

from app.config import Settings
from app.domain import PriceType
from app.providers.ib import IBHistoricalDataProvider


async def test_ib_provider_requests_midpoint_bars_in_readonly_mode(monkeypatch) -> None:
    calls = {}

    class FakeIB:
        connected = False

        async def connectAsync(self, host, port, **kwargs):
            calls["connect"] = (host, port, kwargs)
            self.connected = True

        async def qualifyContractsAsync(self, contract):
            calls["contract"] = contract
            return [contract]

        async def reqHistoricalDataAsync(self, contract, **kwargs):
            calls.setdefault("history", []).append((contract, kwargs))
            return [
                SimpleNamespace(
                    date=datetime(2026, 7, 27, 4, tzinfo=UTC),
                    open=0.77,
                    high=0.78,
                    low=0.76,
                    close=0.775,
                    volume=12_500_000,
                    average=0.7745,
                    barCount=87,
                )
            ]

        def isConnected(self):
            return self.connected

        def disconnect(self):
            self.connected = False

    monkeypatch.setattr(ib_async, "IB", FakeIB)
    provider = IBHistoricalDataProvider(Settings(data_provider="ib"))

    end_at = datetime(2026, 7, 27, tzinfo=UTC)
    bars = await provider.fetch_bars(
        pair="USDGBP",
        bar_size="1 min",
        duration="1 D",
        end_at=end_at,
    )

    assert calls["connect"][2]["readonly"] is True
    requests = [request[1] for request in calls["history"]]
    assert [request["whatToShow"] for request in requests] == ["BID", "ASK", "MIDPOINT"]
    assert all(request["barSizeSetting"] == "1 min" for request in requests)
    assert all(request["endDateTime"] == end_at for request in requests)
    assert all(request["useRTH"] is False for request in requests)
    assert all(request["formatDate"] == 2 for request in requests)
    assert {bar.price_type for bar in bars} == set(PriceType)
    assert all(bar.timestamp.tzinfo == UTC for bar in bars)
    assert all(bar.volume == Decimal("12500000") for bar in bars)
    assert all(bar.weighted_average_price == Decimal("0.7745") for bar in bars)
    assert all(bar.trade_count == 87 for bar in bars)
    await provider.close()
    assert provider._client is None


def test_ib_unavailable_bar_statistics_become_null() -> None:
    bar = SimpleNamespace(
        date=datetime(2026, 7, 27, 4, tzinfo=UTC),
        open=0.77,
        high=0.78,
        low=0.76,
        close=0.775,
        volume=-1,
        average=-1,
        barCount=-1,
    )

    result = IBHistoricalDataProvider._to_price_bar(bar, PriceType.MIDPOINT)

    assert result.price_type is PriceType.MIDPOINT
    assert result.volume is None
    assert result.weighted_average_price is None
    assert result.trade_count is None
