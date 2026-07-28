from collections.abc import AsyncIterator

from app.services.cache import RedisMetricCache


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.pinged = False
        self.closed = False

    async def ping(self) -> bool:
        self.pinged = True
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int) -> None:
        assert ex > 0
        self.values[key] = value

    async def scan_iter(self, *, match: str) -> AsyncIterator[str]:
        prefix = match.removesuffix("*")
        for key in list(self.values):
            if key.startswith(prefix):
                yield key

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)

    async def aclose(self) -> None:
        self.closed = True


async def test_redis_metric_cache_round_trip_and_clear() -> None:
    cache = RedisMetricCache("redis://localhost:6379/0")
    fake_redis = FakeRedis()
    cache.client = fake_redis  # type: ignore[assignment]

    await cache.initialize()
    await cache.set(
        "metric:daily",
        generation=7,
        payload={"summary": {"open": 1.25}},
        ttl_seconds=300,
    )
    cached = await cache.get("metric:daily")

    assert fake_redis.pinged is True
    assert cached is not None
    assert cached.generation == 7
    assert cached.payload == {"summary": {"open": 1.25}}

    await cache.clear()
    assert await cache.get("metric:daily") is None

    await cache.close()
    assert fake_redis.closed is True
