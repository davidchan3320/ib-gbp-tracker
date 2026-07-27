from app.config import Settings
from app.providers.base import HistoricalDataProvider
from app.providers.demo import DemoHistoricalDataProvider
from app.providers.ib import IBHistoricalDataProvider


def build_provider(settings: Settings) -> HistoricalDataProvider:
    if settings.data_provider == "ib":
        return IBHistoricalDataProvider(settings)
    return DemoHistoricalDataProvider()


__all__ = ["HistoricalDataProvider", "build_provider"]
