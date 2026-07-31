---
name: ib-gbp-tracker
description: Maintain and extend the FX Tape (ib-gbp-tracker) repository. Use for any follow-up involving IB Gateway GBP/USD data collection, API pacing, one-minute OHLC persistence and storage estimates, historical backfill, FastAPI routes, the dashboard, SQLite or PostgreSQL, Redis metric caching, Docker Compose, tests, or project documentation.
---

# Maintain IB GBP Tracker

## Establish context

1. Inspect `git status` before editing and preserve unrelated or unfinished changes.
2. Read the relevant sections of `README.md` and `docs/database.md` before changing behavior.
3. Use `.env.example` for configuration context. Do not read, print, commit, or expose `.env`.
4. Treat checked-in code and tests as the source of truth when documentation and implementation differ.

## Preserve system invariants

- Keep the application read-only toward IB Gateway. Do not add order placement without an explicit
  user request and a separate safety design.
- Interpret `GBPUSD` as USD per GBP. Interpret `USDGBP` as GBP per USD.
- Normalize market-data timestamps to UTC before persistence and use half-open time ranges.
- Store one configured currency pair and minute-bar size per database. The current rows do not
  contain pair or bar-size dimensions; add both to the relevant keys before supporting multiple
  configured series in one database. Native IB calendar rows are the fixed-period exceptions below.
- Preserve `(price_type, timestamp)` as the `ohlc_bars` identity unless performing an explicit
  schema migration. Valid price types are `bid`, `ask`, and `midpoint`.
- Keep native IB calendar bars in dedicated tables: `ib_daily_bars` uses `(price_type, day)`,
  `ib_weekly_bars` uses `(price_type, week_start)`, and `ib_monthly_bars` uses
  `(price_type, month_start)`. Do not mix them with the configured minute series in `ohlc_bars`,
  which has no bar-size dimension.
- Keep bar writes idempotent. Overlapping collection windows must update existing rows instead of
  creating duplicates.
- Advance the metric-cache generation in the same transaction as bar writes, then clear disposable
  cache entries. Keep `ohlc_bars` as the source of truth.
- Keep spot-FX `volume`, `weighted_average_price`, and `trade_count` nullable because IB commonly
  reports no meaningful values for these historical series.

## Reason about collection and IB pacing

Describe the current implementation accurately:

- The app does **not** currently maintain a streaming `reqMktData` subscription.
- `IBHistoricalDataProvider.fetch_bars()` sends three serialized
  `reqHistoricalDataAsync()` requests per sync: `BID`, `ASK`, and `MIDPOINT`.
- The IB connection is read-only, `useRTH` is false, timestamps use `formatDate=2`, and
  `keepUpToDate` is false.
- The default scheduler runs every 60 seconds. Demo mode seeds `DEMO_HISTORY_DURATION` (default
  five days) from bundled `fx-chart-nuxt` daily GBP/USD OHLC, extends short coverage with only the
  missing older segment, tolerates bounded market gaps at window edges, and catches up recent gaps.
  It chunks windows longer than 50,000 time slots without truncation. IB mode's first sync
  requests `HISTORY_DURATION` (default one day); later one-minute syncs request a one-hour overlap.
- At the default interval, routine collection averages `3 / 60 = 0.05` historical requests per
  second. Connection and contract qualification add occasional messages. Each direct daily,
  weekly, or monthly endpoint call sends one historical request. Each backfill day sends three
  historical requests.
- IB's usual socket pacing allowance is 50 outgoing requests per second with the default 100 market
  data lines, but historical-data pacing and soft throttling are additional constraints. Preserve
  request serialization, retries, and a non-zero real-Gateway backfill delay.

If explicitly changing the app to true streaming:

1. Treat streaming as an architectural change, not a small provider substitution.
2. Open long-lived subscriptions once; do not poll or cancel and resubscribe every minute.
3. Coalesce incoming bid and ask ticks in memory into UTC minute OHLC buckets.
4. Derive midpoint values consistently and flush only closed or deliberately revised buckets.
5. Upsert minute bars rather than inserting every tick.
6. Handle reconnect and resubscription without duplicate rows.
7. Update the provider interface, lifecycle shutdown, collector, tests, README, and storage estimates.

Incoming streaming callbacks do not consume the outgoing request-per-second allowance. The initial
subscription and cancellation do.

## Calculate row counts and storage

Count all three stored series. A complete market minute produces three rows, not one:

```text
24 hours x 60 minutes x 5 days x 52 weeks = 374,400 minute buckets/year
374,400 x 3 price types = 1,123,200 OHLC rows/year
```

Writing stale values continuously through weekends would instead produce:

```text
365 days x 24 hours x 60 minutes x 3 price types = 1,576,800 rows/year
```

Do not count overlapping sync results as new storage: the composite-key upsert rewrites them.
Account for holidays, maintenance windows, and missing IB history when estimating actual rows.

Avoid presenting raw numeric payload size as total database storage. The current schema uses
`NUMERIC`, nullable statistics, a composite primary-key index, and a timestamp index. For PostgreSQL,
measure the deployed table and extrapolate from observed bytes per row:

```sql
SELECT count(*) AS rows,
       pg_size_pretty(pg_total_relation_size('ohlc_bars')) AS total_size,
       pg_total_relation_size('ohlc_bars') /
         NULLIF((SELECT count(*) FROM ohlc_bars), 0) AS bytes_per_row
FROM ohlc_bars;
```

Multiply measured bytes per row by `1,123,200` for a typical complete FX year. State separately
whether an estimate includes indexes, cache tables, PostgreSQL WAL, backups, or replication.

## Make changes by subsystem

- Change runtime settings in `app/config.py` and mirror them in `.env.example`, Compose, tests, and
  README when applicable.
- Change IB behavior in `app/providers/ib.py`; retain lazy `ib_async` imports so demo mode remains
  independent of a live Gateway.
- Change orchestration in `app/services/collector.py`, `scheduler.py`, or `backfill.py`.
- Change persistence in `app/models.py`, `app/db.py`, and `app/services/repository.py`. Keep SQLite
  and PostgreSQL behavior aligned and document migrations in `docs/database.md`.
- Change API contracts in `app/main.py` and `app/schemas.py`; add API tests and regenerate
  `openapi.json` with `make openapi`.
- Change the dependency-free dashboard in `app/static/` and verify its API assumptions.
- Mock IB with fake clients in tests. Do not require credentials, network access, or a running
  Gateway for the normal test suite.

## Verify before handoff

Run the narrowest relevant tests while iterating, then run:

```bash
uv run ruff check .
uv run pytest
```

After route or schema changes, also run:

```bash
make openapi
uv run pytest tests/test_openapi.py
```

Update `README.md` and `docs/database.md` whenever setup, data semantics, persistence, pacing, or
operational behavior changes. Report which checks ran and disclose any check that could not run.
