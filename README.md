# Fly-seasonality

Futures seasonal dashboard for 11 soft-commodity markets (SB, LSU, KC, LRC, CC, LCC, CT, OJ,
plus derived WP / CFARB / COCARB arb markets) — year-over-year overlay of outrights, spreads,
and flies against days-to-expiry / days-to-FND.

Hardmine architecture: `Code/ingest.py` pulls daily settlement prices from LSEG and computes
spreads/flies, saving parquet to `Database/`. That gets committed and pushed here, and
`Dashboard/app.py` (deployed on Streamlit Cloud) reads it straight from the repo.

- **Code/** — `ingest.py` (LSEG fetch + spread/fly computation) and `market_configs.py`
  (per-market RIC/expiry/FND conventions)
- **Database/** — parquet output, git-tracked
- **Dashboard/** — `app.py`, the Streamlit Cloud entry point
- **Automator/** — `run_pipeline.bat` (Task Scheduler entry: ingest → git push → email) and
  `notify.py`

## Setup

```
pip install -r requirements.txt
python Code/ingest.py          # first run pulls full history from 2000 — takes a while
streamlit run Dashboard/app.py
```
