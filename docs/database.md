# Database design, migration, and first setup

FX Tape supports PostgreSQL and SQLite. PostgreSQL is the recommended target for the full
2017-to-present one-minute backfill; SQLite is intended for local evaluation and smaller datasets.

The database is intentionally scoped to one configured pair and bar size. The default pair is
`GBPUSD`, meaning USD per 1 GBP. Pair and bar size are not repeated on every market-data row, so a
database must not mix configurations.

## Data flow

```text
IB Gateway / demo provider
          │
          ├── collector ───────────────┐
          │                            ▼
          └── backfill CLI ──────> ohlc_bars <──── REST API / metrics
                    │
                    └────────────> backfill_checkpoints
```

`backfill_checkpoints` is job metadata, not a parent of the bar rows, so there is no foreign key
between the two tables.

## `ohlc_bars`

One timestamp has separate bid, ask, and midpoint rows. All timestamps are normalized to UTC.

| Column | PostgreSQL type | Null | Purpose |
| --- | --- | --- | --- |
| `price_type` | `TEXT` | No | `bid`, `ask`, or `midpoint` |
| `timestamp` | `TIMESTAMPTZ` | No | Opening time of the one-minute bar |
| `open` | `NUMERIC(18,8)` | No | Opening price |
| `high` | `NUMERIC(18,8)` | No | Highest price |
| `low` | `NUMERIC(18,8)` | No | Lowest price |
| `close` | `NUMERIC(18,8)` | No | Closing price |
| `volume` | `NUMERIC(24,4)` | Yes | Provider volume, when meaningful |
| `weighted_average_price` | `NUMERIC(18,8)` | Yes | Provider WAP, when available |
| `trade_count` | `INTEGER` | Yes | Provider trade count, when available |

The primary key is `(price_type, timestamp)`. It prevents duplicate series observations and is the
conflict target used by the collector and backfill atomic upserts. The additional timestamp index
supports time-range queries that cross all three price types.

Equivalent PostgreSQL DDL:

```sql
CREATE TABLE ohlc_bars (
    price_type TEXT NOT NULL,
    timestamp TIMESTAMPTZ NOT NULL,
    open NUMERIC(18, 8) NOT NULL,
    high NUMERIC(18, 8) NOT NULL,
    low NUMERIC(18, 8) NOT NULL,
    close NUMERIC(18, 8) NOT NULL,
    volume NUMERIC(24, 4),
    weighted_average_price NUMERIC(18, 8),
    trade_count INTEGER,
    PRIMARY KEY (price_type, timestamp),
    CONSTRAINT ck_ohlc_bars_price_type
        CHECK (price_type IN ('bid', 'ask', 'midpoint'))
);

CREATE INDEX ix_ohlc_bars_timestamp ON ohlc_bars (timestamp);
```

Recent midpoint bars use the primary-key index:

```sql
SELECT *
FROM ohlc_bars
WHERE price_type = 'midpoint'
ORDER BY timestamp DESC
LIMIT 1440;
```

A UTC daily high and low can be calculated with a half-open time window:

```sql
SELECT MAX(high) AS day_high, MIN(low) AS day_low
FROM ohlc_bars
WHERE price_type = 'midpoint'
  AND timestamp >= TIMESTAMPTZ '2026-07-27 00:00:00+00'
  AND timestamp <  TIMESTAMPTZ '2026-07-28 00:00:00+00';
```

## `backfill_checkpoints`

This table contains the durable cursor and progress for each provider/pair/start-date job.

| Column | PostgreSQL type | Null | Purpose |
| --- | --- | --- | --- |
| `checkpoint_key` | `TEXT` | No | Primary key identifying the job |
| `status` | `TEXT` | No | Job lifecycle status |
| `start_at`, `end_at`, `cursor` | `TIMESTAMPTZ` | No | Backfill range and current cursor |
| `started_at`, `updated_at` | `TIMESTAMPTZ` | No | Lifecycle timestamps |
| `completed_at` | `TIMESTAMPTZ` | Yes | Completion, failure, or cancellation time |
| `total_chunks`, `chunks_completed` | `BIGINT` | No | Day-chunk progress |
| `bars_received`, `bars_written` | `BIGINT` | No | Import counters |
| `message` | `TEXT` | Yes | Retry, error, or cancellation detail |

```sql
CREATE TABLE backfill_checkpoints (
    checkpoint_key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    start_at TIMESTAMPTZ NOT NULL,
    end_at TIMESTAMPTZ NOT NULL,
    cursor TIMESTAMPTZ NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL,
    total_chunks BIGINT NOT NULL,
    chunks_completed BIGINT NOT NULL,
    bars_received BIGINT NOT NULL,
    bars_written BIGINT NOT NULL,
    message TEXT,
    CONSTRAINT ck_backfill_checkpoints_status
        CHECK (status IN ('idle', 'running', 'succeeded', 'failed', 'cancelled'))
);
```

## Current migration mechanism

The project does not currently use Alembic or a schema-version table. `Database.create_schema()` is
called at API startup and before CLI backfill commands. It performs these transactional steps:

1. Create both tables when `ohlc_bars` does not exist.
2. If a legacy table has no `price_type`, create `ohlc_bars_v2`, copy old observations as
   `midpoint`, replace the old table, and establish the composite primary key.
3. Add `volume`, `weighted_average_price`, or `trade_count` when missing.
4. Add the timestamp index when missing.
5. Create any missing model tables, including `backfill_checkpoints`.

This startup migration is sufficient for the schema changes currently implemented, but
`create_all()` does not modify arbitrary existing columns or constraints. Before production schema
changes become more frequent, introduce versioned Alembic migrations and run them as a dedicated
deployment step before starting API or backfill processes.

Changing `FX_PAIR` is not a schema migration. Inverting existing USD/GBP rows is also not a simple
rename: prices must be inverted, high and low exchange roles, and bid/ask series must be swapped.
The safe approach is a fresh database followed by a GBP/USD backfill.

## First setup with Docker Compose

Copy the example environment:

```bash
cp .env.example .env
```

For a first demo environment, the relevant settings are:

```dotenv
DATA_PROVIDER=demo
FX_PAIR=GBPUSD

APP_PORT=8000
POSTGRES_PORT=5432
POSTGRES_DB=fx_tape
POSTGRES_USER=fx_tape
POSTGRES_PASSWORD=fx_tape_local
```

The example password is for local development only. Use managed secrets and an application-specific
database role outside local development.

Start PostgreSQL and the API:

```bash
docker compose up --build -d app
docker compose ps
```

On an empty PostgreSQL volume, the official image creates the configured role and database. The API
waits for PostgreSQL to become healthy and then creates or migrates the application schema. Compose
constructs the container's internal `DATABASE_URL` automatically using the `POSTGRES_*` values and
the hostname `db`; the SQLite `DATABASE_URL` in `.env.example` only applies to non-Compose runs.

Inspect the created schema:

```bash
docker compose exec db psql -U fx_tape -d fx_tape -c '\dt'
docker compose exec db psql -U fx_tape -d fx_tape -c '\d+ ohlc_bars'
docker compose exec db psql -U fx_tape -d fx_tape -c '\d+ backfill_checkpoints'
```

Start or inspect the historical backfill:

```bash
docker compose run --rm backfill
docker compose run --rm backfill fx-tape backfill status
```

PostgreSQL applies `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD` only when its data
directory is empty. Changing those values after first startup does not rename the database or reset
the stored credentials.

### Existing USD/GBP environment

Do not point the GBP/USD configuration at a database containing observations from the previous
USD/GBP default. To retain the old data and create a separate Compose volume, add a new project name
to `.env` before startup:

```dotenv
COMPOSE_PROJECT_NAME=fx-tape-gbpusd
```

Deleting the existing volume with `docker compose down -v` is another option only when all existing
market data can be discarded; that command is irreversible.

## First setup with external PostgreSQL

Create a dedicated owner and database while connected as an administrator:

```sql
CREATE ROLE fx_tape_app LOGIN PASSWORD 'replace-with-a-secret';
CREATE DATABASE fx_tape OWNER fx_tape_app ENCODING 'UTF8';
```

Configure the application connection. Percent-encode reserved URL characters in credentials:

```dotenv
DATABASE_URL=postgresql+asyncpg://fx_tape_app:replace-with-a-secret@127.0.0.1:5432/fx_tape
FX_PAIR=GBPUSD
```

Starting the API creates the initial schema:

```bash
uv sync
uv run uvicorn app.main:app
```

The application role must be able to create and alter tables and indexes in its own database because
the current startup migration runs under that role.

## First setup with SQLite

No server setup is required:

```dotenv
DATABASE_URL=sqlite+aiosqlite:///./fx_tape.db
FX_PAIR=GBPUSD
```

Start the API or run a CLI status command; either path creates the file and schema automatically:

```bash
uv run uvicorn app.main:app
# or
uv run fx-tape backfill status
```

Use a different SQLite file for every pair/bar-size configuration.
