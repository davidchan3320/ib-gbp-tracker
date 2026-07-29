import logging
import sqlite3
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.domain import PriceType
from app.main import create_app
from app.providers.base import PriceBar


def test_startup_logs_database_connection_without_password(caplog, monkeypatch) -> None:
    password = "do-not-log-this"
    settings = Settings(
        database_url=f"postgresql+asyncpg://fx_tape:{password}@db:5432/fx_tape",
        scheduler_enabled=False,
    )
    app = create_app(settings)

    async def create_schema() -> None:
        pass

    monkeypatch.setattr(app.state.database, "create_schema", create_schema)

    with caplog.at_level(logging.INFO, logger="uvicorn.error"):
        with TestClient(app):
            pass

    assert "Database connected: backend=postgresql" in caplog.text
    assert "url=postgresql+asyncpg://fx_tape:***@db:5432/fx_tape" in caplog.text
    assert password not in caplog.text


def test_demo_sync_persists_and_exposes_bars(tmp_path: Path) -> None:
    database_path = tmp_path / "test.db"
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        sync_on_startup=False,
    )

    with TestClient(create_app(settings)) as client:
        first_sync = client.post("/api/v1/sync")
        assert first_sync.status_code == 200
        assert first_sync.json()["incremental"] is False

        bars_response = client.get("/api/v1/bars", params={"limit": 25})
        assert bars_response.status_code == 200
        payload = bars_response.json()
        assert payload["pair"] == "GBP/USD"
        assert payload["bar_size"] == "1 min"
        assert payload["price_type"] == "midpoint"
        assert payload["count"] == 25
        assert payload["start"] is None
        assert payload["end"] is None
        assert payload["has_more"] is True
        assert payload["next_cursor"] == payload["bars"][0]["timestamp"]
        assert payload["bars"] == sorted(payload["bars"], key=lambda bar: bar["timestamp"])
        assert set(payload["bars"][0]) == {
            "price_type",
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "weighted_average_price",
            "trade_count",
        }
        assert payload["bars"][0]["volume"] > 0
        assert payload["bars"][0]["weighted_average_price"] > 0
        assert payload["bars"][0]["trade_count"] > 0
        assert all(bar["price_type"] == "midpoint" for bar in payload["bars"])
        assert all(bar["close"] > 1 for bar in payload["bars"])

        older_response = client.get(
            "/api/v1/bars",
            params={"limit": 25, "cursor": payload["next_cursor"]},
        )
        assert older_response.status_code == 200
        older_payload = older_response.json()
        assert older_payload["count"] == 25
        assert older_payload["bars"] == sorted(
            older_payload["bars"], key=lambda bar: bar["timestamp"]
        )
        assert older_payload["bars"][-1]["timestamp"] < payload["bars"][0]["timestamp"]
        assert {bar["timestamp"] for bar in older_payload["bars"]}.isdisjoint(
            bar["timestamp"] for bar in payload["bars"]
        )

        timestamps = [bar["timestamp"] for bar in payload["bars"]]
        range_response = client.get(
            "/api/v1/bars",
            params={"start": timestamps[5], "end": timestamps[15], "limit": 100},
        )
        assert range_response.status_code == 200
        range_payload = range_response.json()
        assert range_payload["start"] == timestamps[5]
        assert range_payload["end"] == timestamps[15]
        assert range_payload["count"] == 10
        assert range_payload["has_more"] is False
        assert range_payload["next_cursor"] is None
        assert [bar["timestamp"] for bar in range_payload["bars"]] == timestamps[5:15]

        invalid_range = client.get(
            "/api/v1/bars",
            params={"start": timestamps[15], "end": timestamps[5]},
        )
        assert invalid_range.status_code == 422

        bid_payload = client.get("/api/v1/bars", params={"price_type": "bid", "limit": 25}).json()
        ask_payload = client.get("/api/v1/bars", params={"price_type": "ask", "limit": 25}).json()
        assert bid_payload["price_type"] == "bid"
        assert ask_payload["price_type"] == "ask"
        assert [bar["timestamp"] for bar in bid_payload["bars"]] == [
            bar["timestamp"] for bar in ask_payload["bars"]
        ]
        assert all(
            bid["close"] < midpoint["close"] < ask["close"]
            for bid, midpoint, ask in zip(
                bid_payload["bars"], payload["bars"], ask_payload["bars"], strict=True
            )
        )

        metrics_response = client.get("/api/v1/metrics")
        assert metrics_response.status_code == 200
        assert metrics_response.json()["latest_close"] > 0

        metric_day = payload["bars"][-1]["timestamp"][:10]
        daily_metrics_response = client.get(
            "/api/v1/metrics/daily",
            params={"day": metric_day},
        )
        assert daily_metrics_response.status_code == 200
        daily_metrics = daily_metrics_response.json()
        assert daily_metrics["pair"] == "GBP/USD"
        assert daily_metrics["price_type"] == "midpoint"
        assert daily_metrics["day"] == metric_day
        assert daily_metrics["timezone"] == "UTC"
        assert daily_metrics["bar_count"] > 0
        assert (
            daily_metrics["average_open_close"]
            == (daily_metrics["open"] + daily_metrics["close"]) / 2
        )
        assert (
            daily_metrics["average_high_low"] == (daily_metrics["high"] + daily_metrics["low"]) / 2
        )

        bid_daily_metrics = client.get(
            "/api/v1/metrics/daily",
            params={"day": metric_day, "price_type": "bid"},
        )
        assert bid_daily_metrics.status_code == 200
        assert bid_daily_metrics.json()["price_type"] == "bid"

        assert client.get("/api/v1/metrics/daily", params={"day": "1900-01-01"}).status_code == 404
        assert client.get("/api/v1/metrics/daily", params={"day": "not-a-date"}).status_code == 422

        status_response = client.get("/api/v1/status")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["stored_bars"] >= 25
        assert status_payload["price_types"] == ["bid", "ask", "midpoint"]
        assert status_payload["last_sync"]["status"] == "succeeded"
        assert "backfill" not in status_payload
        assert client.get("/api/v1/backfill").status_code == 404

        second_sync = client.post("/api/v1/sync")
        assert second_sync.status_code == 200
        assert second_sync.json()["incremental"] is True

    with sqlite3.connect(database_path) as connection:
        tables = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
        ).fetchall()
        columns = connection.execute("PRAGMA table_info(ohlc_bars)").fetchall()
        price_type_counts = connection.execute(
            "SELECT price_type, COUNT(*) FROM ohlc_bars GROUP BY price_type ORDER BY price_type"
        ).fetchall()

    assert tables == [
        ("backfill_checkpoints",),
        ("ib_daily_bars",),
        ("metric_cache",),
        ("metric_cache_state",),
        ("ohlc_bars",),
    ]
    assert [column[1] for column in columns] == [
        "price_type",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "weighted_average_price",
        "trade_count",
    ]
    assert {column[1]: column[5] for column in columns} == {
        "price_type": 1,
        "timestamp": 2,
        "open": 0,
        "high": 0,
        "low": 0,
        "close": 0,
        "volume": 0,
        "weighted_average_price": 0,
        "trade_count": 0,
    }
    assert [price_type for price_type, _count in price_type_counts] == [
        "ask",
        "bid",
        "midpoint",
    ]
    assert len({count for _price_type, count in price_type_counts}) == 1


def test_dashboard_and_health_are_served(tmp_path: Path) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'web.db'}",
        scheduler_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        page = client.get("/")
        assert page.status_code == 200
        assert "FX Tape" in page.text


def test_direct_ib_daily_bar_returns_gateway_ohlc(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_provider="ib",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ib-daily.db'}",
        scheduler_enabled=False,
    )
    app = create_app(settings)
    requested = {}
    close_price = {"value": Decimal("1.3300")}

    async def fetch_daily_bar(*, pair, day, price_type):
        requested.update(pair=pair, day=day, price_type=price_type)
        return PriceBar(
            price_type=price_type,
            timestamp=datetime(2026, 7, 27, tzinfo=UTC),
            open=Decimal("1.3100"),
            close=close_price["value"],
            high=Decimal("1.3400"),
            low=Decimal("1.3000"),
        )

    monkeypatch.setattr(app.state.provider, "fetch_daily_bar", fetch_daily_bar)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/ib/daily",
            params={"day": "2026-07-27", "price_type": "bid"},
        )
        close_price["value"] = Decimal("1.3350")
        refreshed_response = client.get(
            "/api/v1/ib/daily",
            params={"day": "2026-07-27", "price_type": "bid"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "ib",
        "pair": "GBP/USD",
        "bar_size": "1 day",
        "price_type": "bid",
        "day": "2026-07-27",
        "open": 1.31,
        "close": 1.33,
        "high": 1.34,
        "low": 1.3,
        "stored": True,
    }
    assert refreshed_response.status_code == 200
    assert refreshed_response.json()["close"] == 1.335
    assert refreshed_response.json()["stored"] is True
    assert requested == {
        "pair": "GBPUSD",
        "day": date(2026, 7, 27),
        "price_type": PriceType.BID,
    }

    with sqlite3.connect(tmp_path / "ib-daily.db") as connection:
        stored_rows = connection.execute(
            """
            SELECT price_type, day, open, high, low, close, updated_at
            FROM ib_daily_bars
            """
        ).fetchall()

    assert len(stored_rows) == 1
    assert stored_rows[0][:6] == ("bid", "2026-07-27", 1.31, 1.34, 1.3, 1.335)
    assert stored_rows[0][6] is not None


def test_direct_ib_daily_bar_requires_ib_provider(tmp_path: Path) -> None:
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'demo-daily.db'}",
        scheduler_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/ib/daily", params={"day": "2026-07-27"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Direct IB data requires DATA_PROVIDER=ib."}


def test_direct_ib_weekly_bar_returns_gateway_ohlc(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        data_provider="ib",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'ib-weekly.db'}",
        scheduler_enabled=False,
    )
    app = create_app(settings)
    requested = {}

    async def fetch_weekly_bar(*, pair, week_start, price_type):
        requested.update(pair=pair, week_start=week_start, price_type=price_type)
        return PriceBar(
            price_type=price_type,
            timestamp=datetime(2026, 7, 27, tzinfo=UTC),
            open=Decimal("1.3100"),
            close=Decimal("1.3400"),
            high=Decimal("1.3500"),
            low=Decimal("1.3000"),
        )

    monkeypatch.setattr(app.state.provider, "fetch_weekly_bar", fetch_weekly_bar)

    with TestClient(app) as client:
        response = client.get(
            "/api/v1/ib/weekly",
            params={"week": "2026-W31", "price_type": "ask"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "provider": "ib",
        "pair": "GBP/USD",
        "bar_size": "1 week",
        "price_type": "ask",
        "week": "2026-W31",
        "open": 1.31,
        "close": 1.34,
        "high": 1.35,
        "low": 1.3,
    }
    assert requested == {
        "pair": "GBPUSD",
        "week_start": date(2026, 7, 27),
        "price_type": PriceType.ASK,
    }


def test_direct_ib_weekly_bar_requires_ib_provider(tmp_path: Path) -> None:
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'demo-weekly.db'}",
        scheduler_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.get("/api/v1/ib/weekly", params={"week": "2026-W31"})

    assert response.status_code == 503
    assert response.json() == {"detail": "Direct IB data requires DATA_PROVIDER=ib."}


def test_metric_api_database_cache_hits_and_invalidates_on_sync(tmp_path: Path) -> None:
    database_path = tmp_path / "metric-cache.db"
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        metrics_cache_ttl_seconds=300,
    )

    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/sync").status_code == 200
        latest_bar = client.get("/api/v1/bars", params={"limit": 1}).json()["bars"][0]
        metric_day = latest_bar["timestamp"][:10]
        next_day = (datetime.fromisoformat(metric_day) + timedelta(days=1)).date().isoformat()
        single_params = {"day": metric_day}
        batch_params = {"start": metric_day, "end": next_day, "limit": 10}

        assert client.get("/api/v1/metrics/daily", params=single_params).status_code == 200
        assert client.get("/api/v1/metrics/daily", params=batch_params).status_code == 200
        with sqlite3.connect(database_path) as connection:
            first_entries = connection.execute(
                """
                SELECT cache_key, generation, payload, created_at, expires_at
                FROM metric_cache
                ORDER BY cache_key
                """
            ).fetchall()
            first_generation = connection.execute(
                "SELECT generation FROM metric_cache_state WHERE id = 1"
            ).fetchone()[0]

        assert len(first_entries) == 2
        assert {entry[0].split(":")[1] for entry in first_entries} == {"batch", "single"}
        assert all(entry[1] == first_generation for entry in first_entries)

        assert client.get("/api/v1/metrics/daily", params=single_params).status_code == 200
        assert client.get("/api/v1/metrics/daily", params=batch_params).status_code == 200
        with sqlite3.connect(database_path) as connection:
            second_entries = connection.execute(
                """
                SELECT cache_key, generation, payload, created_at, expires_at
                FROM metric_cache
                ORDER BY cache_key
                """
            ).fetchall()

        assert second_entries == first_entries

        assert client.post("/api/v1/sync").status_code == 200
        with sqlite3.connect(database_path) as connection:
            cache_count = connection.execute("SELECT COUNT(*) FROM metric_cache").fetchone()[0]
            invalidated_generation = connection.execute(
                "SELECT generation FROM metric_cache_state WHERE id = 1"
            ).fetchone()[0]

        assert cache_count == 0
        assert invalidated_generation == first_generation + 1

        assert client.get("/api/v1/metrics/daily", params=single_params).status_code == 200
        with sqlite3.connect(database_path) as connection:
            refreshed_generation = connection.execute(
                "SELECT generation FROM metric_cache"
            ).fetchone()[0]
        assert refreshed_generation == invalidated_generation


def test_metric_api_cache_can_be_disabled(tmp_path: Path) -> None:
    database_path = tmp_path / "disabled-metric-cache.db"
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        metrics_cache_ttl_seconds=0,
    )

    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/sync").status_code == 200
        metric_day = client.get("/api/v1/bars", params={"limit": 1}).json()["bars"][0]["timestamp"][
            :10
        ]
        assert (
            client.get(
                "/api/v1/metrics/daily",
                params={"day": metric_day},
            ).status_code
            == 200
        )

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM metric_cache").fetchone()[0] == 0


def test_metric_api_can_use_separate_sqlite_cache_database(tmp_path: Path) -> None:
    database_path = tmp_path / "source.db"
    cache_path = tmp_path / "cache.db"
    settings = Settings(
        data_provider="demo",
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
        metrics_cache_backend="sqlite",
        metrics_cache_url=f"sqlite+aiosqlite:///{cache_path}",
    )

    with TestClient(create_app(settings)) as client:
        assert client.post("/api/v1/sync").status_code == 200
        status_payload = client.get("/api/v1/status").json()
        assert status_payload["metrics_cache_backend"] == "sqlite"
        assert status_payload["metrics_cache_ttl_seconds"] == 300

        metric_day = client.get("/api/v1/bars", params={"limit": 1}).json()["bars"][0]["timestamp"][
            :10
        ]
        assert (
            client.get(
                "/api/v1/metrics/daily",
                params={"day": metric_day},
            ).status_code
            == 200
        )

        with sqlite3.connect(database_path) as source_connection:
            assert source_connection.execute("SELECT COUNT(*) FROM metric_cache").fetchone()[0] == 0
        with sqlite3.connect(cache_path) as cache_connection:
            assert cache_connection.execute("SELECT COUNT(*) FROM metric_cache").fetchone()[0] == 1

        assert client.post("/api/v1/sync").status_code == 200
        with sqlite3.connect(cache_path) as cache_connection:
            assert cache_connection.execute("SELECT COUNT(*) FROM metric_cache").fetchone()[0] == 0


def test_calendar_metrics_aggregate_daily_monthly_and_yearly_periods(tmp_path: Path) -> None:
    database_path = tmp_path / "calendar-metrics.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        with sqlite3.connect(database_path) as connection:
            connection.executemany(
                """
                INSERT INTO ohlc_bars (price_type, timestamp, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("midpoint", "2025-12-31 23:59:00.000000", 0.9, 1.0, 0.8, 0.95),
                    ("midpoint", "2026-01-01 00:00:00.000000", 1.2, 1.4, 1.0, 1.3),
                    ("midpoint", "2026-01-01 23:59:00.000000", 1.3, 1.45, 1.2, 1.4),
                    ("midpoint", "2026-01-31 23:59:00.000000", 1.4, 1.5, 1.1, 1.45),
                    ("midpoint", "2026-02-01 00:00:00.000000", 2.0, 2.2, 1.9, 2.1),
                    ("midpoint", "2026-12-31 23:59:00.000000", 3.0, 3.2, 2.8, 3.1),
                    ("midpoint", "2027-01-01 00:00:00.000000", 4.0, 4.2, 3.8, 4.1),
                ],
            )

        daily = client.get("/api/v1/metrics/daily", params={"day": "2026-01-01"})
        assert daily.status_code == 200
        assert daily.json() == {
            "pair": "GBP/USD",
            "bar_size": "1 min",
            "price_type": "midpoint",
            "timezone": "UTC",
            "bar_count": 2,
            "open": 1.2,
            "close": 1.4,
            "high": 1.45,
            "low": 1.0,
            "average_open_close": pytest.approx(1.3),
            "average_high_low": pytest.approx(1.225),
            "day": "2026-01-01",
        }

        monthly = client.get("/api/v1/metrics/monthly", params={"month": "2026-01"})
        assert monthly.status_code == 200
        assert monthly.json() == {
            "pair": "GBP/USD",
            "bar_size": "1 min",
            "price_type": "midpoint",
            "timezone": "UTC",
            "bar_count": 3,
            "open": 1.2,
            "close": 1.45,
            "high": 1.5,
            "low": 1.0,
            "average_open_close": pytest.approx(1.325),
            "average_high_low": pytest.approx(1.25),
            "month": "2026-01",
        }

        yearly = client.get("/api/v1/metrics/yearly", params={"year": 2026})
        assert yearly.status_code == 200
        assert yearly.json() == {
            "pair": "GBP/USD",
            "bar_size": "1 min",
            "price_type": "midpoint",
            "timezone": "UTC",
            "bar_count": 5,
            "open": 1.2,
            "close": 3.1,
            "high": 3.2,
            "low": 1.0,
            "average_open_close": pytest.approx(2.15),
            "average_high_low": pytest.approx(2.1),
            "year": 2026,
        }

        daily_batch = client.get(
            "/api/v1/metrics/daily",
            params={"start": "2025-12-31", "end": "2026-02-02"},
        )
        assert daily_batch.status_code == 200
        assert daily_batch.json()["start"] == "2025-12-31"
        assert daily_batch.json()["end"] == "2026-02-02"
        assert daily_batch.json()["count"] == 4
        assert daily_batch.json()["has_more"] is False
        assert daily_batch.json()["next_cursor"] is None
        assert [metric["day"] for metric in daily_batch.json()["metrics"]] == [
            "2025-12-31",
            "2026-01-01",
            "2026-01-31",
            "2026-02-01",
        ]
        assert daily_batch.json()["metrics"][1]["open"] == 1.2
        assert daily_batch.json()["metrics"][1]["close"] == 1.4

        monthly_batch = client.get(
            "/api/v1/metrics/monthly",
            params={"start": "2025-12", "end": "2027-01"},
        )
        assert monthly_batch.status_code == 200
        assert monthly_batch.json()["count"] == 4
        assert monthly_batch.json()["has_more"] is False
        assert monthly_batch.json()["next_cursor"] is None
        assert [metric["month"] for metric in monthly_batch.json()["metrics"]] == [
            "2025-12",
            "2026-01",
            "2026-02",
            "2026-12",
        ]
        assert monthly_batch.json()["metrics"][1]["bar_count"] == 3
        assert monthly_batch.json()["metrics"][1]["average_high_low"] == pytest.approx(1.25)

        yearly_batch = client.get(
            "/api/v1/metrics/yearly",
            params={"start": 2025, "end": 2027},
        )
        assert yearly_batch.status_code == 200
        assert yearly_batch.json()["count"] == 2
        assert yearly_batch.json()["has_more"] is False
        assert yearly_batch.json()["next_cursor"] is None
        assert [metric["year"] for metric in yearly_batch.json()["metrics"]] == [2025, 2026]
        assert yearly_batch.json()["metrics"][1]["bar_count"] == 5

        empty_batch = client.get(
            "/api/v1/metrics/daily",
            params={"start": "1900-01-01", "end": "1900-01-02"},
        )
        assert empty_batch.status_code == 200
        assert empty_batch.json()["count"] == 0
        assert empty_batch.json()["has_more"] is False
        assert empty_batch.json()["next_cursor"] is None
        assert empty_batch.json()["metrics"] == []

        first_daily_page = client.get(
            "/api/v1/metrics/daily",
            params={"start": "2025-12-31", "end": "2026-02-02", "limit": 2},
        )
        assert first_daily_page.status_code == 200
        assert first_daily_page.json()["has_more"] is True
        assert first_daily_page.json()["next_cursor"] == "2026-01-31"
        assert [metric["day"] for metric in first_daily_page.json()["metrics"]] == [
            "2026-01-31",
            "2026-02-01",
        ]
        second_daily_page = client.get(
            "/api/v1/metrics/daily",
            params={
                "start": "2025-12-31",
                "end": "2026-02-02",
                "cursor": first_daily_page.json()["next_cursor"],
                "limit": 2,
            },
        )
        assert second_daily_page.status_code == 200
        assert second_daily_page.json()["has_more"] is False
        assert second_daily_page.json()["next_cursor"] is None
        assert [metric["day"] for metric in second_daily_page.json()["metrics"]] == [
            "2025-12-31",
            "2026-01-01",
        ]

        first_monthly_page = client.get(
            "/api/v1/metrics/monthly",
            params={"start": "2025-12", "end": "2027-01", "limit": 2},
        )
        assert first_monthly_page.status_code == 200
        assert first_monthly_page.json()["has_more"] is True
        assert first_monthly_page.json()["next_cursor"] == "2026-02"
        assert [metric["month"] for metric in first_monthly_page.json()["metrics"]] == [
            "2026-02",
            "2026-12",
        ]
        second_monthly_page = client.get(
            "/api/v1/metrics/monthly",
            params={
                "start": "2025-12",
                "end": "2027-01",
                "cursor": first_monthly_page.json()["next_cursor"],
                "limit": 2,
            },
        )
        assert second_monthly_page.status_code == 200
        assert second_monthly_page.json()["has_more"] is False
        assert [metric["month"] for metric in second_monthly_page.json()["metrics"]] == [
            "2025-12",
            "2026-01",
        ]

        first_yearly_page = client.get(
            "/api/v1/metrics/yearly",
            params={"start": 2025, "end": 2027, "limit": 1},
        )
        assert first_yearly_page.status_code == 200
        assert first_yearly_page.json()["has_more"] is True
        assert first_yearly_page.json()["next_cursor"] == 2026
        assert [metric["year"] for metric in first_yearly_page.json()["metrics"]] == [2026]
        second_yearly_page = client.get(
            "/api/v1/metrics/yearly",
            params={
                "start": 2025,
                "end": 2027,
                "cursor": first_yearly_page.json()["next_cursor"],
                "limit": 1,
            },
        )
        assert second_yearly_page.status_code == 200
        assert second_yearly_page.json()["has_more"] is False
        assert [metric["year"] for metric in second_yearly_page.json()["metrics"]] == [2025]

        assert client.get("/api/v1/metrics/monthly", params={"month": "2026-13"}).status_code == 422
        assert client.get("/api/v1/metrics/monthly", params={"month": "1900-01"}).status_code == 404
        assert client.get("/api/v1/metrics/yearly", params={"year": 1900}).status_code == 404
        assert client.get("/api/v1/metrics/daily").status_code == 422
        assert (
            client.get(
                "/api/v1/metrics/monthly",
                params={"month": "2026-01", "start": "2026-01", "end": "2026-02"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/metrics/yearly",
                params={"start": 2026},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/metrics/yearly",
                params={"start": 2026, "end": 2026},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/metrics/daily",
                params={
                    "start": "2026-01-01",
                    "end": "2026-02-01",
                    "cursor": "2025-12-31",
                },
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/metrics/monthly",
                params={"month": "2026-01", "cursor": "2026-01"},
            ).status_code
            == 422
        )
        assert (
            client.get(
                "/api/v1/metrics/yearly",
                params={"start": 2025, "end": 2027, "limit": 0},
            ).status_code
            == 422
        )


def test_weekly_metrics_use_monday_based_iso_weeks(tmp_path: Path) -> None:
    database_path = tmp_path / "weekly-metrics.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
    )

    with TestClient(create_app(settings)) as client:
        with sqlite3.connect(database_path) as connection:
            connection.executemany(
                """
                INSERT INTO ohlc_bars (price_type, timestamp, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [
                    ("midpoint", "2025-12-28 23:59:00.000000", 0.9, 1.0, 0.8, 0.95),
                    ("midpoint", "2025-12-29 00:00:00.000000", 1.2, 1.4, 1.0, 1.3),
                    ("midpoint", "2026-01-04 23:59:00.000000", 1.3, 1.5, 1.1, 1.4),
                    ("midpoint", "2026-01-05 00:00:00.000000", 2.0, 2.2, 1.9, 2.1),
                    ("midpoint", "2026-01-11 23:59:00.000000", 2.1, 2.3, 2.0, 2.2),
                ],
            )

        weekly = client.get("/api/v1/metrics/weekly", params={"week": "2026-W01"})
        assert weekly.status_code == 200
        assert weekly.json() == {
            "pair": "GBP/USD",
            "bar_size": "1 min",
            "price_type": "midpoint",
            "timezone": "UTC",
            "bar_count": 2,
            "open": 1.2,
            "close": 1.4,
            "high": 1.5,
            "low": 1.0,
            "average_open_close": pytest.approx(1.3),
            "average_high_low": pytest.approx(1.25),
            "week": "2026-W01",
        }

        batch = client.get(
            "/api/v1/metrics/weekly",
            params={"start": "2025-W52", "end": "2026-W03"},
        )
        assert batch.status_code == 200
        assert batch.json()["start"] == "2025-W52"
        assert batch.json()["end"] == "2026-W03"
        assert batch.json()["count"] == 3
        assert [metric["week"] for metric in batch.json()["metrics"]] == [
            "2025-W52",
            "2026-W01",
            "2026-W02",
        ]

        first_page = client.get(
            "/api/v1/metrics/weekly",
            params={"start": "2025-W52", "end": "2026-W03", "limit": 2},
        )
        assert first_page.status_code == 200
        assert first_page.json()["has_more"] is True
        assert first_page.json()["next_cursor"] == "2026-W01"
        assert [metric["week"] for metric in first_page.json()["metrics"]] == [
            "2026-W01",
            "2026-W02",
        ]

        second_page = client.get(
            "/api/v1/metrics/weekly",
            params={
                "start": "2025-W52",
                "end": "2026-W03",
                "cursor": first_page.json()["next_cursor"],
                "limit": 2,
            },
        )
        assert second_page.status_code == 200
        assert second_page.json()["has_more"] is False
        assert [metric["week"] for metric in second_page.json()["metrics"]] == ["2025-W52"]

        assert client.get("/api/v1/metrics/weekly", params={"week": "2025-W53"}).status_code == 422
        assert client.get("/api/v1/metrics/weekly", params={"week": "1900-W01"}).status_code == 404
        assert client.get("/api/v1/metrics/weekly").status_code == 422
        assert (
            client.get(
                "/api/v1/metrics/weekly",
                params={"week": "2026-W01", "start": "2026-W01", "end": "2026-W02"},
            ).status_code
            == 422
        )


def test_monthly_metrics_include_more_than_one_api_page_of_bars(tmp_path: Path) -> None:
    database_path = tmp_path / "large-month.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
    )
    month_start = datetime(2026, 3, 1)

    with TestClient(create_app(settings)) as client:
        with sqlite3.connect(database_path) as connection:
            connection.executemany(
                """
                INSERT INTO ohlc_bars (price_type, timestamp, open, high, low, close)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    (
                        "midpoint",
                        (month_start + timedelta(minutes=index)).strftime("%Y-%m-%d %H:%M:%S.%f"),
                        1.0,
                        3.0 if index == 10_000 else 2.0,
                        0.25 if index == 10_000 else 0.5,
                        1.75 if index == 10_000 else 1.5,
                    )
                    for index in range(10_001)
                ),
            )

        response = client.get("/api/v1/metrics/monthly", params={"month": "2026-03"})

        assert response.status_code == 200
        assert response.json()["bar_count"] == 10_001
        assert response.json()["open"] == 1.0
        assert response.json()["close"] == 1.75
        assert response.json()["high"] == 3.0
        assert response.json()["low"] == 0.25

        batch_response = client.get(
            "/api/v1/metrics/monthly",
            params={"start": "2026-03", "end": "2026-04"},
        )
        assert batch_response.status_code == 200
        assert batch_response.json()["count"] == 1
        assert batch_response.json()["has_more"] is False
        assert batch_response.json()["next_cursor"] is None
        assert batch_response.json()["metrics"][0]["bar_count"] == 10_001
        assert batch_response.json()["metrics"][0]["close"] == 1.75


def test_schema_migrates_existing_midpoint_table_to_composite_key(tmp_path: Path) -> None:
    database_path = tmp_path / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE ohlc_bars (
                timestamp DATETIME PRIMARY KEY,
                open NUMERIC(18, 8) NOT NULL,
                high NUMERIC(18, 8) NOT NULL,
                low NUMERIC(18, 8) NOT NULL,
                close NUMERIC(18, 8) NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ohlc_bars (timestamp, open, high, low, close)
            VALUES ('2026-07-27 12:00:00', 0.77, 0.78, 0.76, 0.775)
            """
        )

    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path}",
        scheduler_enabled=False,
    )
    with TestClient(create_app(settings)) as client:
        assert client.get("/healthz").status_code == 200

    with sqlite3.connect(database_path) as connection:
        columns = connection.execute("PRAGMA table_info(ohlc_bars)").fetchall()
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        migrated = connection.execute(
            "SELECT price_type, timestamp, open, high, low, close FROM ohlc_bars"
        ).fetchone()

    assert [column[1] for column in columns] == [
        "price_type",
        "timestamp",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "weighted_average_price",
        "trade_count",
    ]
    assert {column[1]: column[5] for column in columns}["price_type"] == 1
    assert {column[1]: column[5] for column in columns}["timestamp"] == 2
    assert "ib_daily_bars" in tables
    assert migrated == ("midpoint", "2026-07-27 12:00:00", 0.77, 0.78, 0.76, 0.775)
