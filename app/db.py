from collections.abc import AsyncIterator

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Integer,
    MetaData,
    Numeric,
    PrimaryKeyConstraint,
    Table,
    Text,
    inspect,
    literal,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.models import Base

OPTIONAL_BAR_COLUMNS = {
    "volume": "NUMERIC(24, 4)",
    "weighted_average_price": "NUMERIC(18, 8)",
    "trade_count": "INTEGER",
}

BAR_COLUMN_NAMES = (
    "price_type",
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "weighted_average_price",
    "trade_count",
)


class Database:
    def __init__(self, url: str) -> None:
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: AsyncEngine = create_async_engine(
            url,
            pool_pre_ping=True,
            connect_args=connect_args,
        )
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def create_schema(self) -> None:
        async with self.engine.begin() as connection:
            tables = await connection.run_sync(
                lambda sync_connection: set(inspect(sync_connection).get_table_names())
            )
            if "ohlc_bars" not in tables:
                await connection.run_sync(Base.metadata.create_all)
                return

            columns = await self._bar_columns(connection)
            if "price_type" not in columns:
                await connection.run_sync(self._migrate_legacy_bars)
                columns = await self._bar_columns(connection)

            for name, sql_type in OPTIONAL_BAR_COLUMNS.items():
                if name not in columns:
                    await connection.execute(
                        text(f"ALTER TABLE ohlc_bars ADD COLUMN {name} {sql_type}")
                    )

            indexes = await connection.run_sync(
                lambda sync_connection: inspect(sync_connection).get_indexes("ohlc_bars")
            )
            if not any(index.get("column_names") == ["timestamp"] for index in indexes):
                await connection.execute(
                    text("CREATE INDEX ix_ohlc_bars_timestamp ON ohlc_bars (timestamp)")
                )
            await connection.run_sync(Base.metadata.create_all)

    @staticmethod
    async def _bar_columns(connection) -> set[str]:
        return await connection.run_sync(
            lambda sync_connection: {
                column["name"] for column in inspect(sync_connection).get_columns("ohlc_bars")
            }
        )

    @staticmethod
    def _migrate_legacy_bars(sync_connection) -> None:
        source_metadata = MetaData()
        source = Table("ohlc_bars", source_metadata, autoload_with=sync_connection)
        target_metadata = MetaData()
        target = Table(
            "ohlc_bars_v2",
            target_metadata,
            Column("price_type", Text, nullable=False),
            Column("timestamp", DateTime(timezone=True), nullable=False),
            Column("open", Numeric(18, 8), nullable=False),
            Column("high", Numeric(18, 8), nullable=False),
            Column("low", Numeric(18, 8), nullable=False),
            Column("close", Numeric(18, 8), nullable=False),
            Column("volume", Numeric(24, 4), nullable=True),
            Column("weighted_average_price", Numeric(18, 8), nullable=True),
            Column("trade_count", Integer, nullable=True),
            PrimaryKeyConstraint("price_type", "timestamp"),
            CheckConstraint(
                "price_type IN ('bid', 'ask', 'midpoint')",
                name="ck_ohlc_bars_v2_price_type",
            ),
        )
        target.create(sync_connection)

        values = [literal("midpoint"), source.c.timestamp]
        for name in BAR_COLUMN_NAMES[2:]:
            values.append(source.c[name] if name in source.c else literal(None))
        sync_connection.execute(target.insert().from_select(BAR_COLUMN_NAMES, select(*values)))
        source.drop(sync_connection)
        sync_connection.execute(text("ALTER TABLE ohlc_bars_v2 RENAME TO ohlc_bars"))

    async def dispose(self) -> None:
        await self.engine.dispose()

    async def session(self) -> AsyncIterator[AsyncSession]:
        async with self.session_factory() as session:
            yield session
