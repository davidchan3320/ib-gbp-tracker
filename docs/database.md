# Database design, migration, and first setup

FX Tape supports PostgreSQL and SQLite. PostgreSQL is the recommended target for the full
2017-to-present one-minute backfill; SQLite is intended for local evaluation and smaller datasets.

The database is intentionally scoped to one configured pair and one configured minute-bar size.
The default pair is `GBPUSD`, meaning USD per 1 GBP. Pair and minute-bar size are not repeated on
every market-data row, so a database must not mix configurations. The `ib_daily_bars` table is a
separate fixed-`1 day` series for that same pair; `ib_weekly_bars` and `ib_monthly_bars` hold the
corresponding native IB periods.

## Data flow

```text
IB Gateway / demo provider
          │
   ┌──────┼──────────────┐
   ▼      ▼              ▼
collector backfill CLI  direct calendar APIs (IB only)
   │      ├───────────> backfill_checkpoints
   └──┬───┘              │
      ▼                  ▼
 ohlc_bars          native IB period tables
      │
      ├────────────> REST API / metrics
      └────────────> metric cache (SQLite / PostgreSQL / Redis)
```

`backfill_checkpoints` is job metadata and the metric cache contains disposable derived results.
Neither is a parent of the bar rows, so there are no foreign keys between these stores.

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

The daily, weekly, monthly, and yearly metrics endpoints expose this OHLC calculation for half-open
UTC calendar periods. Weeks use the ISO week calendar and begin Monday at 00:00 UTC:

```text
GET /api/v1/metrics/daily?day=2026-07-27&price_type=midpoint
GET /api/v1/metrics/weekly?week=2026-W31&price_type=midpoint
GET /api/v1/metrics/monthly?month=2026-07&price_type=midpoint
GET /api/v1/metrics/yearly?year=2026&price_type=midpoint
```

Each endpoint also accepts a half-open range for batch aggregation:

```text
GET /api/v1/metrics/daily?start=2026-07-01&end=2026-08-01
GET /api/v1/metrics/weekly?start=2026-W01&end=2027-W01
GET /api/v1/metrics/monthly?start=2026-01&end=2027-01
GET /api/v1/metrics/yearly?start=2020&end=2027
```

Batch ranges use keyset pagination. The aggregate query orders populated UTC buckets from newest to
oldest and reads `limit + 1` buckets to determine `has_more`. The returned page is reversed into
chronological order. When another page exists, its earliest bucket becomes `next_cursor`; the next
query applies that cursor as an exclusive upper time bound, avoiding offset scans and duplicate
boundary buckets.

Each query reads only its requested period through the composite primary-key index. The open is the
first bar's `open`, the close is the last bar's `close`, and the extremes are `MAX(high)` and
`MIN(low)`. The result also includes `(open + close) / 2` as `average_open_close` and
`(high + low) / 2` as `average_high_low`. These values are computed on demand and require no schema
change or metrics table; the aggregate query does not load every minute bar into application
memory. Batch queries group the bars into UTC calendar buckets in one query, rank the first and
last bar within each bucket, and omit buckets without data.

## Native IB calendar-bar tables

The direct IB endpoints fetch one native historical bar and store it before returning:

| Endpoint | IB bar size | Table | Composite primary key |
| --- | --- | --- | --- |
| `/api/v1/ib/daily` | `1 day` | `ib_daily_bars` | `(price_type, day)` |
| `/api/v1/ib/weekly` | `1 week` | `ib_weekly_bars` | `(price_type, week_start)` |
| `/api/v1/ib/monthly` | `1 month` | `ib_monthly_bars` | `(price_type, month_start)` |

The normalized weekly key is the requested ISO week's Monday, and the monthly key is the first day
of the requested month. Repeated requests are idempotent: a later fetch refreshes the OHLC values
and `updated_at` for the existing price-type/period row. These tables stay separate from
`ohlc_bars`, which contains the configured minute series and has no bar-size key.

All three tables share these columns, with only the period-key name changing:

| Column | PostgreSQL type | Null | Purpose |
| --- | --- | --- | --- |
| `price_type` | `TEXT` | No | `bid`, `ask`, or `midpoint` |
| `day`, `week_start`, or `month_start` | `DATE` | No | Normalized period key |
| `open` | `NUMERIC(18,8)` | No | Native period opening price |
| `high` | `NUMERIC(18,8)` | No | Native period high |
| `low` | `NUMERIC(18,8)` | No | Native period low |
| `close` | `NUMERIC(18,8)` | No | Native period closing price |
| `updated_at` | `TIMESTAMPTZ` | No | UTC time of the latest successful upsert |

Equivalent PostgreSQL DDL for the weekly table (the daily and monthly tables substitute their
period-key column and constraint names):

```sql
CREATE TABLE ib_weekly_bars (
    price_type TEXT NOT NULL,
    week_start DATE NOT NULL,
    open NUMERIC(18, 8) NOT NULL,
    high NUMERIC(18, 8) NOT NULL,
    low NUMERIC(18, 8) NOT NULL,
    close NUMERIC(18, 8) NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (price_type, week_start),
    CONSTRAINT ck_ib_weekly_bars_price_type
        CHECK (price_type IN ('bid', 'ask', 'midpoint'))
);
```

The configured currency pair applies to every native-period row and is not duplicated in these
tables. They are not used by the minute-bar calendar aggregation endpoints, and writing them does
not invalidate the minute-derived metric cache.

## API time-range pagination

`GET /api/v1/bars` exposes the indexed time-range access path without using offset pagination:

```text
GET /api/v1/bars
    ?price_type=midpoint
    &start=2026-07-01T00:00:00Z
    &end=2026-07-28T00:00:00Z
    &limit=500
```

The range is `[start, end)`. The query reads newest rows first through the composite primary-key
index, requests `limit + 1` rows to determine whether more exist, and reverses the selected page so
the returned `bars` array is chronological. A response includes:

```json
{
  "start": "2026-07-01T00:00:00Z",
  "end": "2026-07-28T00:00:00Z",
  "count": 500,
  "has_more": true,
  "next_cursor": "2026-07-27T15:41:00Z",
  "bars": []
}
```

Pass `next_cursor` unchanged as the next request's `cursor`. The cursor is an exclusive upper bound,
so adjacent pages cannot repeat the boundary row:

```text
GET /api/v1/bars
    ?price_type=midpoint
    &start=2026-07-01T00:00:00Z
    &end=2026-07-28T00:00:00Z
    &cursor=2026-07-27T15:41:00Z
    &limit=500
```

This remains stable when new bars arrive because later pages always move toward older timestamps.
Offset pagination is intentionally not used because large offsets become increasingly expensive and
can shift when concurrent inserts occur.

## Metric cache

The cache stores JSON summaries for single calendar periods and cursor-paginated batch pages. The
cache key covers price type, UTC query bounds, period level, and page limit. SQL backends use the
`metric_cache` table; Redis uses `fx_tape:metrics:*` keys with native TTLs. `metric_cache_state`
always remains in the source bar database and contains the current source-data generation:

| Table / column | PostgreSQL type | Purpose |
| --- | --- | --- |
| `metric_cache.cache_key` | `TEXT` | Primary key for the normalized aggregate query |
| `metric_cache.generation` | `BIGINT` | Bar generation used to calculate the payload |
| `metric_cache.payload` | `TEXT` | Compact JSON summary or page |
| `metric_cache.created_at` | `TIMESTAMPTZ` | Time the result was calculated |
| `metric_cache.expires_at` | `TIMESTAMPTZ` | TTL boundary; indexed for cleanup |
| `metric_cache_state.id` | `INTEGER` | Singleton key (`1`) |
| `metric_cache_state.generation` | `BIGINT` | Current bar-data generation |

Cache lookup requires both a matching source generation and an unexpired backend entry. Bar upserts
advance the singleton generation in the same transaction as the OHLC writes, then clear the chosen
backend. The generation check protects against a concurrent request inserting an old calculation
after that clear: the old entry can exist, but no later request will use it.

Configuration accepts `sqlite`, `postgresql` (`pgsql` alias), or `redis`:

```dotenv
METRICS_CACHE_BACKEND=sqlite
METRICS_CACHE_URL=sqlite+aiosqlite:///./fx_tape_cache.db
METRICS_CACHE_TTL_SECONDS=300
```

If the backend is omitted, it follows `DATABASE_URL`. `METRICS_CACHE_URL` can be omitted when a SQL
cache uses the same backend as the source database; otherwise it selects a separate SQLite or
PostgreSQL database. Redis URLs use `redis://` or `rediss://`. A zero TTL bypasses all cache reads
and writes. SQL cache writes opportunistically remove expired rows, while Redis enforces expiration
natively.

Docker Compose provisions a `redis:8-alpine` service, waits for `redis-cli ping`, and defaults the
containers to `METRICS_CACHE_BACKEND=redis` with `redis://redis:6379/0`. Redis is capped at 64 MiB
with `allkeys-lru`, persistence is disabled, and `/data` is a disposable `tmpfs`. Evicted or
restart-cleared results are recalculated from `ohlc_bars`.

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

1. Create all model tables when `ohlc_bars` does not exist.
2. If a legacy table has no `price_type`, create `ohlc_bars_v2`, copy old observations as
   `midpoint`, replace the old table, and establish the composite primary key.
3. Add `volume`, `weighted_average_price`, or `trade_count` when missing.
4. Add the timestamp index when missing.
5. Create any missing model tables, including all three native IB calendar-bar tables, the cache,
   and `backfill_checkpoints`.
6. Ensure the singleton `metric_cache_state` generation row exists.

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
docker compose exec db psql -U fx_tape -d fx_tape -c '\d+ ib_daily_bars'
docker compose exec db psql -U fx_tape -d fx_tape -c '\d+ ib_weekly_bars'
docker compose exec db psql -U fx_tape -d fx_tape -c '\d+ ib_monthly_bars'
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
