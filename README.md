# Liquidation Lab 0.4.8

Paper-only laboratory; no real exchange orders are sent.

Application source remains in `Liquidation_Lab_v0.4.7/` for compatibility with existing Railway root-directory settings. The package version is 0.4.8.

## Railway

Build from the repository root using the root `Dockerfile` and `railway.toml`. Existing services using `Liquidation_Lab_v0.4.7` as their root can continue to use the Dockerfile in that directory.

Mount persistent storage at `/data`; the default SQLite path is `/data/paper_v040.db`. Preserve the existing database and make a backup before updating. Configure `DASHBOARD_TOKEN` before exposing the dashboard. `DATA_MODE=live` uses public exchange data, but execution remains paper-only.

`/health` reports engine health, not the availability of a trading opportunity. `/api/status` includes account halt reasons and feed status; `/api/decisions` includes persisted entry decisions. Both API routes use the configured dashboard token.

## Local checks

```sh
cd Liquidation_Lab_v0.4.7
python -m pip install -r requirements.txt -r requirements-dev.txt
python -m pytest tests scenario_model -q -p no:cacheprovider
```

For an offline run set `DATA_MODE=synthetic` and a writable `DB_PATH`, then run `python -m app`.

See [release notes](Liquidation_Lab_v0.4.7/CHANGELOG_048_RU.md) and [configuration](Liquidation_Lab_v0.4.7/README_RU.md).
