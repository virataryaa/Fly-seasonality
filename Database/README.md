# Database

Parquet output from `Code/ingest.py` lives here:

- `prices.parquet` — daily settlement prices per contract, all 11 markets
- `spreads.parquet` — calendar spreads derived from prices
- `flies.parquet` — flies derived from spreads
- `gbp_rates.parquet` — GBPUSD spot (FX input to COCARB only)
- `skipped_contracts.parquet` — expired contracts with permanent data gaps, skipped on future runs

Run `python Code/ingest.py` to populate this folder, then commit and push —
`Dashboard/app.py` (deployed on Streamlit Cloud) reads directly from these files.
