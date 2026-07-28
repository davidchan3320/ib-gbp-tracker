import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from redis.asyncio import Redis
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings
from app.models import MetricCache


@dataclass(frozen=True, slots=True)
class CachedMetric:
    generation: int
    payload: dict[str, object]


class MetricCacheBackend(Protocol):
    name: str

    async def initialize(self) -> None: ...

    async def get(self, cache_key: str) -> CachedMetric | None: ...

    async def set(
        self,
        cache_key: str,
        *,
        generation: int,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> None: ...

    async def clear(self) -> None: ...

    async def close(self) -> None: ...


class NullMetricCache:
    name = "disabled"

    async def initialize(self) -> None:
        return None

    async def get(self, cache_key: str) -> CachedMetric | None:
        return None

    async def set(
        self,
        cache_key: str,
        *,
        generation: int,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> None:
        return None

    async def clear(self) -> None:
        return None

    async def close(self) -> None:
        return None


class SQLMetricCache:
    def __init__(
        self,
        *,
        name: str,
        session_factory: async_sessionmaker[AsyncSession],
        owned_engine: AsyncEngine | None = None,
    ) -> None:
        self.name = name
        self.session_factory = session_factory
        self.owned_engine = owned_engine

    @classmethod
    def from_url(cls, *, name: str, url: str) -> "SQLMetricCache":
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        engine = create_async_engine(url, pool_pre_ping=True, connect_args=connect_args)
        return cls(
            name=name,
            session_factory=async_sessionmaker(
                engine,
                class_=AsyncSession,
                expire_on_commit=False,
            ),
            owned_engine=engine,
        )

    async def initialize(self) -> None:
        if self.owned_engine is None:
            return
        async with self.owned_engine.begin() as connection:
            await connection.run_sync(MetricCache.__table__.create, checkfirst=True)

    async def get(self, cache_key: str) -> CachedMetric | None:
        async with self.session_factory() as session:
            entry = await session.get(MetricCache, cache_key)
            if entry is None or _as_utc(entry.expires_at) <= datetime.now(UTC):
                return None
            try:
                payload = json.loads(entry.payload)
            except json.JSONDecodeError:
                return None
            if not isinstance(payload, dict):
                return None
            return CachedMetric(generation=entry.generation, payload=payload)

    async def set(
        self,
        cache_key: str,
        *,
        generation: int,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> None:
        now = datetime.now(UTC)
        values = {
            "cache_key": cache_key,
            "generation": generation,
            "payload": json.dumps(payload, separators=(",", ":"), sort_keys=True),
            "created_at": now,
            "expires_at": now + timedelta(seconds=ttl_seconds),
        }
        async with self.session_factory() as session:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            if dialect == "postgresql":
                insert = postgresql_insert
            elif dialect == "sqlite":
                insert = sqlite_insert
            else:
                raise RuntimeError(f"Unsupported SQL metric cache dialect: {dialect}")
            statement = insert(MetricCache).values(values)
            statement = statement.on_conflict_do_update(
                index_elements=[MetricCache.cache_key],
                set_={
                    "generation": statement.excluded.generation,
                    "payload": statement.excluded.payload,
                    "created_at": statement.excluded.created_at,
                    "expires_at": statement.excluded.expires_at,
                },
            )
            await session.execute(delete(MetricCache).where(MetricCache.expires_at <= now))
            await session.execute(statement)
            await session.commit()

    async def clear(self) -> None:
        async with self.session_factory() as session:
            await session.execute(delete(MetricCache))
            await session.commit()

    async def close(self) -> None:
        if self.owned_engine is not None:
            await self.owned_engine.dispose()


class RedisMetricCache:
    name = "redis"
    KEY_PREFIX = "fx_tape:metrics:"

    def __init__(self, url: str) -> None:
        self.client: Redis = Redis.from_url(url, decode_responses=True)

    async def initialize(self) -> None:
        await self.client.ping()

    async def get(self, cache_key: str) -> CachedMetric | None:
        value = await self.client.get(self._key(cache_key))
        if value is None:
            return None
        try:
            envelope = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(envelope, dict) or not isinstance(envelope.get("payload"), dict):
            return None
        return CachedMetric(
            generation=int(envelope["generation"]),
            payload=envelope["payload"],
        )

    async def set(
        self,
        cache_key: str,
        *,
        generation: int,
        payload: dict[str, object],
        ttl_seconds: int,
    ) -> None:
        envelope = json.dumps(
            {"generation": generation, "payload": payload},
            separators=(",", ":"),
            sort_keys=True,
        )
        await self.client.set(self._key(cache_key), envelope, ex=ttl_seconds)

    async def clear(self) -> None:
        keys: list[str] = []
        async for key in self.client.scan_iter(match=f"{self.KEY_PREFIX}*"):
            keys.append(key)
            if len(keys) == 500:
                await self.client.delete(*keys)
                keys.clear()
        if keys:
            await self.client.delete(*keys)

    async def close(self) -> None:
        await self.client.aclose()

    @classmethod
    def _key(cls, cache_key: str) -> str:
        return f"{cls.KEY_PREFIX}{cache_key}"


def build_metric_cache(
    settings: Settings,
    source_session_factory: async_sessionmaker[AsyncSession],
) -> MetricCacheBackend:
    if settings.metrics_cache_ttl_seconds == 0:
        return NullMetricCache()

    backend = settings.resolved_metrics_cache_backend
    url = settings.resolved_metrics_cache_url
    if backend == "redis":
        return RedisMetricCache(url)
    if url == settings.database_url:
        return SQLMetricCache(name=backend, session_factory=source_session_factory)
    return SQLMetricCache.from_url(name=backend, url=url)


def _as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=value.tzinfo or UTC).astimezone(UTC)
