# Bundled demo market data

`daily.csv` is copied verbatim from the sibling `fx-chart-nuxt/content/daily.csv` collection. It is
the canonical demo OHLC input for FX Tape. The source weekly and monthly CSV files are presentation
summaries, so FX Tape continues to calculate those periods from the daily-backed intraday series in
its own database.

The provider ignores rows without OHLC unless a forward/open value is present for a trailing partial
day. At load time it expands any malformed high/low bounds to include open and close; the vendored
CSV itself remains unchanged.

To refresh the snapshot from sibling checkouts:

```bash
cp ../fx-chart-nuxt/content/daily.csv app/demo_data/daily.csv
```
