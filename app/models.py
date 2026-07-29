from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Index, Integer, Numeric, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain import PriceType


class Base(DeclarativeBase):
    pass


class OHLCBar(Base):
    __tablename__ = "ohlc_bars"
    __table_args__ = (
        CheckConstraint(
            "price_type IN ('bid', 'ask', 'midpoint')",
            name="ck_ohlc_bars_price_type",
        ),
        Index("ix_ohlc_bars_timestamp", "timestamp"),
    )

    price_type: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        default=PriceType.MIDPOINT.value,
    )
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(24, 4), nullable=True)
    weighted_average_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), nullable=True)
    trade_count: Mapped[int | None] = mapped_column(Integer, nullable=True)


class IBDailyBar(Base):
    __tablename__ = "ib_daily_bars"
    __table_args__ = (
        CheckConstraint(
            "price_type IN ('bid', 'ask', 'midpoint')",
            name="ck_ib_daily_bars_price_type",
        ),
    )

    price_type: Mapped[str] = mapped_column(
        Text,
        primary_key=True,
        default=PriceType.MIDPOINT.value,
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 8), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BackfillCheckpoint(Base):
    __tablename__ = "backfill_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "status IN ('idle', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_backfill_checkpoints_status",
        ),
    )

    checkpoint_key: Mapped[str] = mapped_column(Text, primary_key=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cursor: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    total_chunks: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunks_completed: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bars_received: Mapped[int] = mapped_column(BigInteger, nullable=False)
    bars_written: Mapped[int] = mapped_column(BigInteger, nullable=False)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)


class MetricCacheState(Base):
    __tablename__ = "metric_cache_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)


class MetricCache(Base):
    __tablename__ = "metric_cache"
    __table_args__ = (Index("ix_metric_cache_expires_at", "expires_at"),)

    cache_key: Mapped[str] = mapped_column(Text, primary_key=True)
    generation: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
