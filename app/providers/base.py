from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.domain import PriceType


@dataclass(frozen=True, slots=True)
class PriceBar:
    price_type: PriceType
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    weighted_average_price: Decimal | None = None
    trade_count: int | None = None


class HistoricalDataProvider(ABC):
    name: str

    @abstractmethod
    async def fetch_bars(
        self,
        *,
        pair: str,
        bar_size: str,
        duration: str,
        end_at: datetime | None = None,
        allow_empty: bool = False,
    ) -> list[PriceBar]:
        """Fetch bid, ask, and midpoint historical bars in chronological order."""

    async def close(self) -> None:
        """Release any provider connection held across requests."""
        return None
