# FX Tape

FX Tape collects one-minute GBP/USD bid, ask, and midpoint bars from Interactive Brokers Gateway.
It stores each OHLC series plus any available volume, weighted-average price, and trade count in
SQLite or PostgreSQL, calculates live and calendar-period metrics, and serves a responsive dashboard
plus a documented REST API. A separate CLI owns the resumable historical backfill, while SQLite,
PostgreSQL, or Redis can cache calendar aggregates.

The app starts in **demo mode**, so the entire collector, database, chart, and metrics path can be
evaluated without an IB account. Switching one environment variable activates the real IB
Gateway provider.

## Agent skill set

For agent-assisted follow-up work, load the project-local [`ib-gbp-tracker`](SKILL.md) skill first.
It records the current architecture, data semantics, IB pacing assumptions, persistence rules, and
required verification workflow. The following supporting skills are useful when available in the
agent environment:

| Skill | Project use |
| --- | --- |
| `ib-gbp-tracker` | Start here for every repository change, investigation, or handoff. |
| `postgresql-table-design` | Review table keys, data types, indexes, constraints, and migrations. |
| `postgresql-optimization` | Analyze query plans, aggregation performance, and database storage. |
| `multi-stage-dockerfile` | Change or review the production Docker build. |
| `frontend-design` | Improve the dependency-free dashboard layout and visual design. |
| `browser:control-in-app-browser` | Exercise and visually verify the local dashboard and API flows. |
| `skill-creator` | Maintain or extend the project-local `SKILL.md`. |

Supporting skills depend on the agent installation. If one is unavailable, follow `SKILL.md`, the
repository documentation, and the existing test suite directly.

## Docker quick start

Requirements: Docker Engine with the Compose plugin.

```bash
cp .env.example .env
docker compose up --build -d app
```

This starts the API, PostgreSQL, and Redis metric cache in demo mode. Adminer is available as an
optional database UI; start it with `docker compose up -d adminer`. Open
[http://127.0.0.1:8000](http://127.0.0.1:8000) for the dashboard or
[http://127.0.0.1:8080](http://127.0.0.1:8080) for Adminer. Inspect the containers with
`docker compose ps`, or follow API logs with:

```bash
docker compose logs -f app
```

The expected core services are `app`, `db`, and `redis`. Check Redis directly from inside its
container with:

```bash
docker compose exec redis redis-cli ping
```

It should print `PONG`.

Stop the stack with `docker compose down`. The named PostgreSQL volume is retained; running
`docker compose down -v` also deletes all stored market data. Redis is a disposable cache capped at
64 MiB with `allkeys-lru`, so it is intentionally empty after the Redis container is recreated.

The image runs as an unprivileged user, has a built-in API health check, and uses a read-only root
filesystem under Compose. Build it without starting services using `docker compose build app`.
Redis is published only on the host loopback address, using `REDIS_PORT` (default `6379`).

See [Database design, migration, and first setup](docs/database.md) for the complete PostgreSQL and
SQLite schema, startup migration behavior, environment configuration, and inspection commands.

## Local quick start

Run the Python app directly while Docker Compose provides PostgreSQL and Redis. This workflow does
not build the application image.

Requirements: Python 3.11+, [uv](https://docs.astral.sh/uv/), and Docker Engine with the Compose
plugin.

```bash
cp .env.example .env
docker compose up -d db redis
docker compose ps db redis
uv sync
```

Set these values in `.env` so the host-run app connects to the containers through their loopback
ports:

```dotenv
DATABASE_URL=postgresql+asyncpg://fx_tape:fx_tape_local@127.0.0.1:5432/fx_tape
METRICS_CACHE_BACKEND=redis
METRICS_CACHE_URL=redis://127.0.0.1:6379/0
```

Then start the app without Docker:

```bash
uv run uvicorn app.main:app --reload
```

Open [http://127.0.0.1:8000](http://127.0.0.1:8000), or verify it with
`curl http://127.0.0.1:8000/healthz`. The scheduler creates a synthetic initial history in the
background, and API documentation is available at
[http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

Stop the supporting containers with `docker compose stop db redis`, or use `docker compose down`.
PostgreSQL data is retained in the `fx_tape_postgres` volume; use `docker compose down -v` only
when you also want to remove that local data.

Run the checks with:

```bash
uv run ruff check .
uv run pytest
```

## Connect IB Gateway

IB Gateway must be running and authenticated; the socket API does not replace the Gateway login.
The official setup guide explains the relationship between Gateway/TWS and socket clients:
[Interactive Brokers TWS API initial setup](https://interactivebrokers.github.io/tws-api/initial_setup.html).

1. In IB Gateway, open **Configure → Settings → API → Settings**.
2. Enable **ActiveX and Socket Clients** and add the app host to **Trusted IPs** if needed.
3. Keep **Read-Only API** enabled. This application contains no order endpoints.
4. Confirm the socket port. The common Gateway defaults are `4002` for paper and `4001` for live;
   always use the value displayed by your Gateway session.
5. Set a client ID not used by another API process.
6. Update `.env`:

```dotenv
DATA_PROVIDER=ib
IB_HOST=127.0.0.1
IB_PORT=4002
IB_CLIENT_ID=21
IB_BACKFILL_CLIENT_ID=22
```

When the app runs in Docker, leave `IB_DOCKER_HOST=host.docker.internal`. Compose maps that name to
the Docker host on Linux and Docker Desktop. `IB_HOST` remains available for running the app
directly outside a container.

Restart the app, then use **Sync now**. The latest sync state is kept in memory and shown in the
Data pulse panel; it is not written to the database. The IB adapter makes `BID`, `ASK`, and
`MIDPOINT` historical requests with `useRTH=false` and `formatDate=2`; all stored timestamps are
normalized to UTC. Forex supports all three historical bar types according to the
[IB historical-bar reference](https://interactivebrokers.github.io/tws-api/historical_bars.html).

The web collector and backfill CLI use separate read-only Gateway connections and client IDs. This
allows them to run at the same time without an IB client-ID collision.

IB says API historical data generally carries the same market-data subscription requirements as
streaming top-of-book data. If the connection works but no history arrives, check the account's
market-data permissions and the same instrument in a Gateway/TWS chart. See
[IB historical market data](https://interactivebrokers.github.io/tws-api/historical_data.html).

The default `FX_PAIR=GBPUSD` means **USD per 1 GBP**. For the inverse convention—GBP per 1 USD—set
`FX_PAIR=USDGBP` and use a separate database because the configured pair is not stored on each row.

## PostgreSQL for local development

SQLite is the zero-setup default. For PostgreSQL:

```bash
docker compose up -d db
```

Then set:

```dotenv
DATABASE_URL=postgresql+asyncpg://fx_tape:fx_tape_local@127.0.0.1:5432/fx_tape
```

The full Compose stack configures its internal database connection automatically. The Compose
credentials are development-only; use secret-managed credentials and a managed database in
production.

### Adminer database UI

Adminer is an optional local-only database browser. Start it with PostgreSQL:

```bash
docker compose up -d adminer
```

Open [http://127.0.0.1:8080](http://127.0.0.1:8080) and use:

| Field | Default value |
| --- | --- |
| System | `PostgreSQL` |
| Server | `db` |
| Username | `fx_tape` |
| Password | `fx_tape_local` |
| Database | `fx_tape` |

The username, password, and database follow `POSTGRES_USER`, `POSTGRES_PASSWORD`, and `POSTGRES_DB`
from `.env`. The server remains `db`, the PostgreSQL service name inside Compose. Set
`ADMINER_PORT` to change the host port from `8080`; Adminer remains bound to `127.0.0.1` and is not
exposed on external network interfaces. Stop it with `docker compose stop adminer`, or stop the
whole stack with `docker compose down`.

## How collection works

```text
IB Gateway socket / demo generator
                │
                ▼
      HistoricalDataProvider
                │
       ┌────────┴────────┐
       ▼                 ▼
 web collector      backfill CLI
       └────────┬────────┘
                ▼
  idempotent typed-OHLC database upsert
                │
        ┌───────┴──────────────┐
        ▼                      ▼
    REST API             metric calculators
        │                      │
        │                      ▼
        │              generation-safe cache
        └──────────┬───────────┘
                   ▼
                dashboard
```

The database contains one normalized `ohlc_bars` table with `price_type`, `timestamp`, `open`,
`high`, `low`, `close`, `volume`, `weighted_average_price`, and `trade_count`. The composite primary
key is `(price_type, timestamp)`, so one minute can contain independent `bid`, `ask`, and `midpoint`
rows without duplicates. A separate timestamp index supports cross-series time-range queries. The
final three fields are nullable because IB does not provide meaningful centralized volume, WAP, or
trade count for spot FX bid/ask/midpoint history. Pair, bar size, provider, generated IDs, audit
timestamps, and sync history are not persisted. Calendar metric results may be stored temporarily
in the configured SQL or Redis cache described below; bars remain their source of truth.

The separate `backfill_checkpoints` table stores only resumable job state. It advances after a day's
bars commit, so an interruption can repeat one idempotent upsert but cannot skip a completed chunk.

The first successful run requests `HISTORY_DURATION` (one day by default). Later one-minute runs
request a one-hour overlap for each price type. Rewriting that overlap is deliberate: IB can revise
the latest incomplete bar, while the composite primary key prevents duplicates. It also avoids
repeatedly pulling the entire backfill and helps stay within IB's
[historical-data request limits](https://interactivebrokers.github.io/tws-api/historical_limitations.html).

## Backfill from 2017

The backfill walks backward from the current minute to `2017-01-01T00:00:00Z` in one-day requests.
Each day requests `BID`, `ASK`, and `MIDPOINT`, writes only timestamps inside that half-open daily
window, persists its checkpoint, then waits before the next chunk. Closed-market days may produce
no rows and still advance safely.

Configure it in `.env`:

```dotenv
IB_BACKFILL_CLIENT_ID=22
BACKFILL_START=2017-01-01
BACKFILL_REQUEST_DELAY_SECONDS=10
BACKFILL_MAX_RETRIES=3
BACKFILL_RETRY_DELAY_SECONDS=30
```

Run it from the command line:

```bash
uv run fx-tape backfill run
uv run fx-tape backfill status
```

With Docker Compose, the equivalent commands use the opt-in `backfill` service:

```bash
docker compose run --rm backfill
docker compose run --rm backfill fx-tape backfill status
```

The first command starts or resumes the backfill. For a detached worker, use:

```bash
docker compose --profile backfill up -d backfill
docker compose logs -f backfill
```

`docker compose stop backfill` sends a graceful interrupt so the current checkpoint is preserved.
The normal `docker compose up` command does not start this profile.

`run` resumes the durable cursor by default. Use `uv run fx-tape backfill run --restart` only when
intentionally discarding progress and resetting the cursor to the current minute. Pressing
`Ctrl+C` cancels cleanly after preserving the current checkpoint. Add `--json` to either command
for machine-readable final output.

The CLI holds a PostgreSQL advisory lock or a local SQLite file lock, so a second worker for the
same checkpoint exits instead of competing. It can run alongside the API because database writes
use atomic upserts and the IB client IDs are distinct. To detach it without a process supervisor:

```bash
nohup uv run fx-tape backfill run > backfill.log 2>&1 &
```

The default 10-second pause is configurable, and failed requests use exponential retry delays. IB
notes that hard pacing limits for bars of one minute or greater have been lifted, but large or
frequent requests can still be soft-throttled.

A 2017-to-present run is roughly 3,500 calendar chunks and more than 10,000 IB requests. Expect it
to take hours and store roughly ten million price rows, depending on market closures and IB's data
availability. PostgreSQL is the better target for this full backfill; do not set the request delay
to zero against a real Gateway.

## API

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/healthz` | Process liveness |
| `GET` | `/api/v1/status` | Collector, database, cache backend, and latest sync state |
| `GET` | `/api/v1/bars` | Time-range and cursor-paginated `bid`, `ask`, or `midpoint` bars |
| `GET` | `/api/v1/ib/daily` | Fetch one daily OHLC bar directly from IB Gateway |
| `GET` | `/api/v1/metrics` | Midpoint price, 24h range/change, SMA 20, ATR 14, realized vol |
| `GET` | `/api/v1/metrics/daily` | OHLC and two averages for one day or a day range |
| `GET` | `/api/v1/metrics/monthly` | OHLC and two averages for one month or a month range |
| `GET` | `/api/v1/metrics/yearly` | OHLC and two averages for one year or a year range |
| `POST` | `/api/v1/sync` | Run one collection immediately |

Bar ranges are half-open: `start` is inclusive and `end` is exclusive. The first response contains
the newest bars in the range, ordered chronologically. When `has_more` is true, pass
`next_cursor` back as `cursor` to retrieve the next older page:

```bash
curl --get http://127.0.0.1:8000/api/v1/bars \
  --data-urlencode 'price_type=midpoint' \
  --data-urlencode 'start=2026-07-01T00:00:00Z' \
  --data-urlencode 'end=2026-07-28T00:00:00Z' \
  --data-urlencode 'limit=500'

curl --get http://127.0.0.1:8000/api/v1/bars \
  --data-urlencode 'price_type=midpoint' \
  --data-urlencode 'start=2026-07-01T00:00:00Z' \
  --data-urlencode 'end=2026-07-28T00:00:00Z' \
  --data-urlencode 'cursor=2026-07-27T15:41:00Z' \
  --data-urlencode 'limit=500'
```

`next_cursor` is `null` when no older page remains. Omitting `start` and `end` preserves the default
behavior of returning the most recent bars. Timestamps without an explicit offset are interpreted
as UTC.

Fetch one daily bar directly from IB Gateway without reading or writing the application database:

```bash
curl --get http://127.0.0.1:8000/api/v1/ib/daily \
  --data-urlencode 'day=2026-07-27' \
  --data-urlencode 'price_type=midpoint'
```

This endpoint requires `DATA_PROVIDER=ib`. It makes one IB historical-data request with a `1 day`
bar size and returns `open`, `close`, `high`, and `low` for the matching IB session date. Choose
`bid`, `ask`, or `midpoint` with `price_type`; the default is `midpoint`. A market holiday or other
date for which IB returns no matching session bar produces `404`.

Request calendar-period metrics with a required day, month, or year and an optional price type
(default: `midpoint`):

```bash
curl --get http://127.0.0.1:8000/api/v1/metrics/daily \
  --data-urlencode 'day=2026-07-27' \
  --data-urlencode 'price_type=midpoint'

curl --get http://127.0.0.1:8000/api/v1/metrics/monthly \
  --data-urlencode 'month=2026-07' \
  --data-urlencode 'price_type=midpoint'

curl --get http://127.0.0.1:8000/api/v1/metrics/yearly \
  --data-urlencode 'year=2026' \
  --data-urlencode 'price_type=midpoint'
```

For a batch response, replace the single-period parameter with inclusive `start` and exclusive
`end` parameters in the same format:

```bash
curl --get http://127.0.0.1:8000/api/v1/metrics/daily \
  --data-urlencode 'start=2026-07-01' \
  --data-urlencode 'end=2026-08-01' \
  --data-urlencode 'limit=10'

curl --get http://127.0.0.1:8000/api/v1/metrics/monthly \
  --data-urlencode 'start=2026-01' \
  --data-urlencode 'end=2027-01'

curl --get http://127.0.0.1:8000/api/v1/metrics/yearly \
  --data-urlencode 'start=2020' \
  --data-urlencode 'end=2027'
```

The first batch page contains the newest populated periods, ordered chronologically within the page.
Responses include `has_more` and `next_cursor`; pass `next_cursor` unchanged as the next request's
exclusive `cursor` to fetch the next older page:

```bash
curl --get http://127.0.0.1:8000/api/v1/metrics/daily \
  --data-urlencode 'start=2026-07-01' \
  --data-urlencode 'end=2026-08-01' \
  --data-urlencode 'cursor=2026-07-20' \
  --data-urlencode 'limit=10'
```

Batch responses also contain the requested `start` and `end`, the current page's `metrics`, and its
item count in `count`. `limit` defaults to 100 and cannot exceed 1,000. Periods without stored bars
are omitted; a range with no data returns `200` with an empty `metrics` list. A request must provide
either the single-period parameter or both range parameters, and cannot mix the two forms. Ranges
are limited to 10,000 calendar periods per request.

Example batch page:

```json
{
  "pair": "GBP/USD",
  "bar_size": "1 min",
  "price_type": "midpoint",
  "timezone": "UTC",
  "start": "2026-01",
  "end": "2027-01",
  "count": 1,
  "has_more": true,
  "next_cursor": "2026-09",
  "metrics": [
    {
      "pair": "GBP/USD",
      "bar_size": "1 min",
      "price_type": "midpoint",
      "timezone": "UTC",
      "bar_count": 43200,
      "open": 1.318,
      "close": 1.337,
      "high": 1.351,
      "low": 1.301,
      "average_open_close": 1.3275,
      "average_high_low": 1.326,
      "month": "2026-09"
    }
  ]
}
```

Each request covers the corresponding half-open UTC calendar interval. `open` comes from the first
stored bar, `close` from the last stored bar, `high` and `low` are the period's extremes, and
`bar_count` shows how many stored bars contributed. The derived fields are:

```text
average_open_close = (open + close) / 2
average_high_low   = (high + low) / 2
```

Single-period requests return `404` when the selected series has no stored bars for that period.
Both single and batch requests aggregate in the database on demand.

Calendar metric aggregates and cursor pages support SQLite, PostgreSQL, or Redis cache backends.
When no backend is configured, the cache uses the application database's SQLite/PostgreSQL type and
URL. The default TTL is five minutes and can be changed or disabled in `.env`:

| Backend value | URL example | Typical use |
| --- | --- | --- |
| `sqlite` | `sqlite+aiosqlite:///./fx_tape_cache.db` | Local or single-process deployment |
| `postgresql` / `pgsql` | `postgresql+asyncpg://user:pass@host/cache` | Shared SQL deployment |
| `redis` | `redis://host:6379/0` | Lowest-latency shared cache |

```dotenv
METRICS_CACHE_BACKEND=sqlite
METRICS_CACHE_URL=sqlite+aiosqlite:///./fx_tape.db
METRICS_CACHE_TTL_SECONDS=300
```

PostgreSQL (the `pgsql` alias is also accepted):

```dotenv
METRICS_CACHE_BACKEND=postgresql
METRICS_CACHE_URL=postgresql+asyncpg://user:password@localhost/cache_database
```

Redis:

```dotenv
METRICS_CACHE_BACKEND=redis
METRICS_CACHE_URL=redis://localhost:6379/0
```

Docker Compose includes Redis and selects it by default with
`METRICS_CACHE_URL=redis://redis:6379/0`. The API and backfill services wait for the Redis health
check before starting. Override `METRICS_CACHE_BACKEND` and `METRICS_CACHE_URL` to use SQLite or
PostgreSQL instead; the Redis container remains harmless and disposable when another backend is
selected. The bundled Redis has no password because it is not published outside the private Compose
network; use authenticated `redis://` or TLS `rediss://` endpoints outside local development.

`METRICS_CACHE_URL` may be omitted when the selected SQL backend matches `DATABASE_URL`; the same
database and connection settings are then reused. A separate SQLite URL creates only the cache
table in that file. Set `METRICS_CACHE_TTL_SECONDS=0` to disable cache reads and writes; the maximum
TTL is 86,400 seconds.

Repeated requests with the same price type, period range, cursor, and limit reuse the cached result.
Every collector or backfill bar upsert atomically advances the source-data generation and then
clears existing entries, so a successful data write cannot leave active stale metrics. Generation
checks remain race-safe even when the cache is in a separate database or Redis. Expired SQL entries
are cleaned up during writes; Redis entries use native key TTLs.

The checked-in [`openapi.json`](openapi.json) can be imported into API clients and documentation
tools. Regenerate it after changing routes or schemas:

```bash
make openapi
```

Only one configured pair and bar size are stored per database. The three price types are identified
inside the composite primary key. Supporting multiple pairs or bar sizes in one database would
require adding those series dimensions to the key.

## Extend metrics

Metric calculation is isolated in [`app/services/metrics.py`](app/services/metrics.py). Add a field
to `MetricSnapshot`, calculate it in `calculate_metrics`, expose it through `MetricsResponse`, and
render it in the dashboard. Calculators receive chronological OHLC bars and are evaluated on
demand; only disposable TTL cache entries are stored, never canonical metric records.

## Project layout

```text
app/
  config.py                validated application and cache settings
  cli.py                   standalone backfill commands and worker lock
  main.py                  FastAPI routes and lifecycle
  models.py                OHLC, checkpoint, cache, and generation tables
  providers/               IB and deterministic demo adapters
  services/backfill.py     resumable 2017-to-present backfill engine
  services/cache.py        SQLite, PostgreSQL, and Redis cache backends
  services/collector.py    incremental collection orchestration
  services/metrics.py      extensible metric calculations
  services/repository.py   bar persistence, aggregation, and cache integration
  static/                  dependency-free dashboard
Dockerfile                 non-root multi-stage API/CLI image
compose.yaml               API, PostgreSQL, Redis, Adminer, and optional backfill worker
.dockerignore              minimal Docker build context
docs/database.md           schema, migration, and first-environment setup
scripts/export_openapi.py  deterministic OpenAPI export
openapi.json               generated API contract
tests/                     API, collector, and metric tests
```
