from enum import StrEnum


class PriceType(StrEnum):
    BID = "bid"
    ASK = "ask"
    MIDPOINT = "midpoint"


PRICE_TYPES = tuple(PriceType)
