import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


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
        assert payload["pair"] == "USD/GBP"
        assert payload["bar_size"] == "1 min"
        assert payload["price_type"] == "midpoint"
        assert payload["count"] == 25
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

    assert tables == [("backfill_checkpoints",), ("ohlc_bars",)]
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
    assert migrated == ("midpoint", "2026-07-27 12:00:00", 0.77, 0.78, 0.76, 0.775)
