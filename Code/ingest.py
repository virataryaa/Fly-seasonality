"""
Seasonal Dashboard — LSEG ingest.

Fetches daily settlement prices for 11 soft-commodity futures markets
(8 direct LSEG pulls + GBPUSD FX + 3 derived arb markets), builds calendar
spreads and flies, and saves everything to parquet files in ../Database.

Self-contained: does not depend on the separate `futures` project or its
per-market DuckDBs — this can run standalone and its output is what gets
committed and pushed to GitHub for the Streamlit Cloud dashboard to read.

Run:
    python ingest.py            # incremental (only fetches what's missing)
    python ingest.py --force    # wipe and re-fetch full history for everything
"""

import sys
import time
import logging
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import lseg.data as ld

from market_configs import (
    DIRECT_MARKETS, ALL_MARKETS,
    GBP_RIC, GBP_START,
    WP, WP_TICKER, WP_FACTOR, WP_PAIR_MAP,
    CFARB_TICKER, CFARB_FACTOR, CFARB_PAIR_MAP,
    COCARB_TICKER, COCARB_PAIR_MAP,
)

HERE   = Path(__file__).resolve().parent
DB_DIR = HERE.parent / "Database"

PRICES_PARQUET  = DB_DIR / "prices.parquet"
SPREADS_PARQUET = DB_DIR / "spreads.parquet"
FLIES_PARQUET   = DB_DIR / "flies.parquet"
GBP_PARQUET     = DB_DIR / "gbp_rates.parquet"
SKIPPED_PARQUET = DB_DIR / "skipped_contracts.parquet"

REQUEST_SLEEP      = 0.5   # seconds between LSEG Data API calls
STALE_CUTOFF_DAYS   = 30   # stop chasing expired contracts with permanent gaps

PRICES_COLS  = ['ticker', 'ric', 'contract_name', 'date', 'close', 'expiry', 'dte', 'fnd', 'dtf']
SPREADS_COLS = ['ticker', 'spread_type', 'vintage', 'date', 'front_name', 'back_name', 'spread', 'dte', 'name']
FLIES_COLS   = ['ticker', 'fly_type', 'vintage', 'date', 'front_spread', 'back_spread', 'fly', 'dte', 'name']
SKIPPED_COLS = ['ticker', 'contract_name', 'expiry', 'last_attempt']

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger(__name__)


# ── parquet I/O ──────────────────────────────────────────────────────────────
def _load(path: Path, columns: list[str]) -> pd.DataFrame:
    if path.exists():
        return pd.read_parquet(path)
    return pd.DataFrame(columns=columns)


def _clean_name(ticker: str, month_code: str, year: int) -> str:
    return f"{ticker}{month_code}{year % 100:02d}"


# ── LSEG fetch helpers ───────────────────────────────────────────────────────
def _fetch_history(ric: str, start: date, end: date, field: str = 'SETTLE') -> pd.DataFrame:
    """Returns DataFrame indexed by date with column 'close', or empty on failure."""
    try:
        raw = ld.get_history(
            universe=ric, fields=[field], interval='daily',
            start=start.strftime('%Y-%m-%d'), end=end.strftime('%Y-%m-%d'),
        )
    except Exception as e:
        msg = str(e).lower()
        if 'invalid ric' in msg or 'no data' in msg:
            log.warning("%s — %s", ric, e)
            return pd.DataFrame()
        raise
    time.sleep(REQUEST_SLEEP)

    if raw is None or raw.empty:
        return pd.DataFrame()

    raw.index.name = 'date'
    raw.columns = raw.columns.str.lower()
    field_l = field.lower()
    if field_l not in raw.columns:
        return pd.DataFrame()

    out = raw[[field_l]].rename(columns={field_l: 'close'}).dropna(subset=['close'])
    out.index = pd.to_datetime(out.index)
    return out


def _fetch_metadata(rics: list[str]) -> dict:
    """Returns {ric: {'expiry': date|None, 'fnd': date|None}}."""
    if not rics:
        return {}
    result = {r: {'expiry': None, 'fnd': None} for r in rics}
    try:
        df = ld.get_data(universe=rics, fields=['EXPIR_DATE', 'TR.FOFirstNoticeDay'])
        time.sleep(REQUEST_SLEEP)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ric = row.get('Instrument')
            if ric not in result:
                continue
            result[ric]['expiry'] = _parse_date(row.get('EXPIR_DATE'))
            result[ric]['fnd']    = _parse_date(row.get('First Notice Day'))
    except Exception:
        log.exception("fetch_metadata failed — actual expiry/FND will be missing")
    return result


def _parse_date(val):
    # pd.NaT is (surprisingly) a subclass of datetime.date AND truthy, so it must be
    # caught by pd.isna() before any isinstance(val, date) check, or a missing
    # expiry/FND silently passes through as a "valid" date full of NaNs.
    try:
        if val is None or pd.isna(val):
            return None
    except (TypeError, ValueError):
        pass  # pd.isna() can't evaluate some types (e.g. arrays) — fall through
    if isinstance(val, date):
        return val
    try:
        return pd.to_datetime(val).date()
    except Exception:
        return None


def _all_contracts(cfg, today: date) -> list[dict]:
    out = []
    for year in range(cfg.start_year, today.year + 3):
        for mc in cfg.month_codes:
            exp = cfg.expiry_fn(mc, year)
            if exp < date(cfg.start_year, 1, 1):
                continue
            out.append(dict(
                month_code=mc, year=year,
                ric=cfg.build_ric_fn(mc, year, today),
                expiry=exp,
                clean_name=_clean_name(cfg.ticker, mc, year),
            ))
    return sorted(out, key=lambda c: c['expiry'])


# ── shared upsert helper ────────────────────────────────────────────────────
def _apply_updates(prices_df: pd.DataFrame, ticker: str, existing: pd.DataFrame,
                    new_rows: list[pd.DataFrame], delete_specs: list[tuple]) -> pd.DataFrame:
    if delete_specs:
        keep = pd.Series(True, index=existing.index)
        for contract_name, replace_from in delete_specs:
            if replace_from is None:
                keep &= ~(existing['contract_name'] == contract_name)
            else:
                keep &= ~((existing['contract_name'] == contract_name) &
                          (existing['date'] >= pd.Timestamp(replace_from)))
        existing = existing[keep]

    parts = [df for df in ([existing] + new_rows) if not df.empty]
    combined = pd.concat(parts, ignore_index=True) if parts else existing

    other = prices_df[prices_df['ticker'] != ticker]
    parts2 = [df for df in (other, combined) if not df.empty]
    return pd.concat(parts2, ignore_index=True) if parts2 else prices_df.iloc[0:0]


# ── direct (LSEG) market ingest ─────────────────────────────────────────────
def _ingest_direct_market(cfg, prices_df: pd.DataFrame, skipped_df: pd.DataFrame,
                           today: date, force: bool = False):
    ticker   = cfg.ticker
    existing = prices_df[prices_df['ticker'] == ticker]
    if force:
        existing = existing.iloc[0:0]

    contract_last_dates = existing.groupby('contract_name')['date'].max().to_dict()
    skipped_names = set(skipped_df.loc[skipped_df['ticker'] == ticker, 'contract_name'])

    all_ctrs = _all_contracts(cfg, today)
    cutoff   = today - timedelta(days=STALE_CUTOFF_DAYS)

    to_process, pre_skipped = [], 0
    for ctr in all_ctrs:
        if not force and ctr['clean_name'] in skipped_names:
            pre_skipped += 1
            continue
        if not force:
            ctr_last = contract_last_dates.get(ctr['clean_name'])
            if ctr_last is not None:
                ctr_last_d = ctr_last.date() if hasattr(ctr_last, 'date') else ctr_last
                if ctr['expiry'] < cutoff:
                    pre_skipped += 1
                    continue
                if ctr_last_d >= min(ctr['expiry'], today):
                    pre_skipped += 1
                    continue
        to_process.append(ctr)

    log.info("%s: %d contracts total, %d to fetch, %d already up to date",
             ticker, len(all_ctrs), len(to_process), pre_skipped)

    if not to_process:
        return prices_df, skipped_df

    metadata = _fetch_metadata([c['ric'] for c in to_process if c['ric']])

    new_rows, delete_specs, new_skip_rows = [], [], []

    for i, ctr in enumerate(to_process, 1):
      try:
        ric = ctr['ric']
        if not ric:
            continue
        meta          = metadata.get(ric, {})
        actual_expiry = meta.get('expiry') or ctr['expiry']
        actual_fnd    = meta.get('fnd')
        if actual_fnd is None and cfg.fnd_fn:
            actual_fnd = cfg.fnd_fn(ctr['month_code'], ctr['year'])

        # Metadata dates can come back as pandas/numpy scalar types (e.g. numpy.float64
        # if LSEG returns a raw serial number) that datetime.date() rejects outright —
        # normalize defensively rather than trust the upstream type.
        actual_expiry = date(int(actual_expiry.year), int(actual_expiry.month), int(actual_expiry.day))

        clean_name = ctr['clean_name']
        ctr_last   = contract_last_dates.get(clean_name)

        if force or ctr_last is None:
            fetch_start  = max(date(cfg.start_year, 1, 1),
                                date(actual_expiry.year - 2, actual_expiry.month, 1))
            replace_from = None
        else:
            ctr_last_d   = ctr_last.date() if hasattr(ctr_last, 'date') else ctr_last
            fetch_start  = ctr_last_d - timedelta(days=5)
            replace_from = fetch_start

        fetch_end = min(actual_expiry, today)
        if fetch_start >= fetch_end:
            continue

        log.info("[%d/%d] %s / %s — fetching %s -> %s",
                 i, len(to_process), clean_name, ric, fetch_start, fetch_end)
        df = _fetch_history(ric, fetch_start, fetch_end + timedelta(days=1))

        if df.empty:
            log.warning("%s — no data returned", clean_name)
            if actual_expiry < today:
                new_skip_rows.append(dict(ticker=ticker, contract_name=clean_name,
                                           expiry=actual_expiry, last_attempt=today))
            continue

        out = df.reset_index()
        out['ticker']        = ticker
        out['ric']           = ric
        out['contract_name'] = clean_name
        out['date']          = pd.to_datetime(out['date'])
        out['expiry']        = pd.Timestamp(actual_expiry)
        out['dte']           = (out['expiry'] - out['date']).dt.days
        if actual_fnd:
            out['fnd'] = pd.Timestamp(actual_fnd)
            out['dtf'] = (out['fnd'] - out['date']).dt.days
        else:
            out['fnd'] = pd.NaT
            out['dtf'] = np.nan
        out = out[PRICES_COLS].drop_duplicates(subset=['ric', 'date'])

        new_rows.append(out)
        delete_specs.append((clean_name, replace_from))
        log.info("%s — stored %d rows", clean_name, len(out))
      except Exception:
        log.exception("%s — unexpected error, skipping this contract", ctr.get('clean_name'))
        continue

    prices_df = _apply_updates(prices_df, ticker, existing, new_rows, delete_specs)

    if new_skip_rows:
        add = pd.DataFrame(new_skip_rows)
        skipped_df = pd.concat([
            skipped_df[~((skipped_df['ticker'] == ticker) &
                         (skipped_df['contract_name'].isin(add['contract_name'])))],
            add,
        ], ignore_index=True)

    return prices_df, skipped_df


# ── GBPUSD FX ────────────────────────────────────────────────────────────────
def _ingest_gbp(gbp_df: pd.DataFrame, today: date, force: bool = False) -> pd.DataFrame:
    if force:
        gbp_df = gbp_df.iloc[0:0]

    last  = gbp_df['date'].max() if not gbp_df.empty else None
    start = (last.date() if hasattr(last, 'date') else last) if last is not None else GBP_START
    end   = today

    if start >= end:
        log.info("GBP: up to date (last=%s)", last)
        return gbp_df

    chunks = []
    for year in range(start.year, end.year + 1):
        chunk_start = date(year, 1, 1) if year != start.year else start
        chunk_end   = date(year, 12, 31) if year != end.year else end
        log.info("GBP: fetching %s -> %s", chunk_start, chunk_end)
        chunk = _fetch_history(GBP_RIC, chunk_start, chunk_end, field='MID_PRICE')
        if not chunk.empty:
            chunks.append(chunk)

    if not chunks:
        log.warning("GBP: no data returned")
        return gbp_df

    new = pd.concat(chunks)
    new = new[~new.index.duplicated(keep='last')].reset_index()
    new.columns = ['date', 'gbpusd']

    gbp_df = gbp_df[gbp_df['date'] < pd.Timestamp(start)] if not gbp_df.empty else gbp_df
    parts  = [df for df in (gbp_df, new) if not df.empty]
    gbp_df = pd.concat(parts, ignore_index=True).sort_values('date').reset_index(drop=True)
    log.info("GBP: stored, last=%s", gbp_df['date'].max().date())
    return gbp_df


# ── derived markets ──────────────────────────────────────────────────────────
def _ingest_wp(prices_df: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    ticker   = WP_TICKER
    lsu_all  = prices_df[prices_df['ticker'] == 'LSU']
    sb_all   = prices_df[prices_df['ticker'] == 'SB']
    existing = prices_df[prices_df['ticker'] == ticker]
    if force:
        existing = existing.iloc[0:0]

    global_last    = existing['date'].max() if not existing.empty else None
    existing_names = set(existing['contract_name'].unique())

    if lsu_all.empty:
        return prices_df
    years = sorted(lsu_all['expiry'].dropna().apply(lambda d: pd.Timestamp(d).year).unique())

    new_rows, delete_specs = [], []
    for year in years:
        for wp_m, (sb_m, sb_off) in WP_PAIR_MAP.items():
            lsu_name = _clean_name('LSU', wp_m, year)
            sb_name  = _clean_name('SB', sb_m, year + sb_off)
            wp_name  = _clean_name(ticker, wp_m, year)

            is_new     = wp_name not in existing_names
            fetch_from = None if (force or is_new) else global_last

            lsu_df = lsu_all[lsu_all['contract_name'] == lsu_name]
            sb_df  = sb_all[sb_all['contract_name'] == sb_name]
            if fetch_from is not None:
                lsu_df = lsu_df[lsu_df['date'] >= fetch_from]
                sb_df  = sb_df[sb_df['date'] >= fetch_from]
            if lsu_df.empty or sb_df.empty:
                continue

            m = lsu_df.merge(sb_df, on='date', suffixes=('_lsu', '_sb'))
            if m.empty:
                continue

            m['close']  = m['close_lsu'] - m['close_sb'] * WP_FACTOR
            m['expiry'] = m[['expiry_lsu', 'expiry_sb']].min(axis=1)
            m['dte']    = (m['expiry'] - m['date']).dt.days
            m['ticker'] = ticker
            m['ric']    = wp_name
            m['contract_name'] = wp_name
            m['fnd'] = pd.NaT
            m['dtf'] = np.nan

            new_rows.append(m[PRICES_COLS])
            delete_specs.append((wp_name, fetch_from))

    prices_df = _apply_updates(prices_df, ticker, existing, new_rows, delete_specs)
    log.info("WP: %d contract-vintages updated", len(new_rows))
    return prices_df


def _ingest_cfarb(prices_df: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    ticker   = CFARB_TICKER
    kc_all   = prices_df[prices_df['ticker'] == 'KC']
    lrc_all  = prices_df[prices_df['ticker'] == 'LRC']
    existing = prices_df[prices_df['ticker'] == ticker]
    if force:
        existing = existing.iloc[0:0]

    global_last    = existing['date'].max() if not existing.empty else None
    existing_names = set(existing['contract_name'].unique())

    if kc_all.empty:
        return prices_df
    years = sorted(kc_all['expiry'].dropna().apply(lambda d: pd.Timestamp(d).year).unique())

    new_rows, delete_specs = [], []
    for year in years:
        for cfarb_m, (kc_m, lrc_m, lrc_off) in CFARB_PAIR_MAP.items():
            kc_name     = _clean_name('KC', kc_m, year)
            lrc_name    = _clean_name('LRC', lrc_m, year + lrc_off)
            cfarb_name  = _clean_name(ticker, cfarb_m, year)

            is_new     = cfarb_name not in existing_names
            fetch_from = None if (force or is_new) else global_last

            kc_df  = kc_all[kc_all['contract_name'] == kc_name]
            lrc_df = lrc_all[lrc_all['contract_name'] == lrc_name]
            if fetch_from is not None:
                kc_df  = kc_df[kc_df['date'] >= fetch_from]
                lrc_df = lrc_df[lrc_df['date'] >= fetch_from]
            if kc_df.empty or lrc_df.empty:
                continue

            m = kc_df.merge(lrc_df, on='date', suffixes=('_kc', '_lrc'))
            if m.empty:
                continue

            m['close']  = m['close_kc'] - m['close_lrc'] / CFARB_FACTOR
            m['expiry'] = m[['expiry_kc', 'expiry_lrc']].min(axis=1)
            m['dte']    = (m['expiry'] - m['date']).dt.days
            m['fnd']    = m[['fnd_kc', 'fnd_lrc']].min(axis=1)
            m['dtf']    = (m['fnd'] - m['date']).dt.days
            m['ticker'] = ticker
            m['ric']    = cfarb_name
            m['contract_name'] = cfarb_name

            new_rows.append(m[PRICES_COLS])
            delete_specs.append((cfarb_name, fetch_from))

    prices_df = _apply_updates(prices_df, ticker, existing, new_rows, delete_specs)
    log.info("CFARB: %d contract-vintages updated", len(new_rows))
    return prices_df


def _ingest_cocarb(prices_df: pd.DataFrame, gbp_df: pd.DataFrame, force: bool = False) -> pd.DataFrame:
    ticker   = COCARB_TICKER
    cc_all   = prices_df[prices_df['ticker'] == 'CC']
    lcc_all  = prices_df[prices_df['ticker'] == 'LCC']
    existing = prices_df[prices_df['ticker'] == ticker]
    if force:
        existing = existing.iloc[0:0]

    if gbp_df.empty or cc_all.empty:
        log.warning("COCARB: missing GBP rates or CC prices — skipping")
        return prices_df

    global_last    = existing['date'].max() if not existing.empty else None
    existing_names = set(existing['contract_name'].unique())

    # Forward-fill GBPUSD across the full calendar range so it can be inner-joined
    # onto any futures trading date without gaps.
    gbp_s     = gbp_df.set_index('date')['gbpusd'].sort_index()
    full_idx  = pd.date_range(gbp_s.index.min(), gbp_s.index.max(), freq='D')
    gbp_full  = gbp_s.reindex(full_idx).ffill().reset_index()
    gbp_full.columns = ['date', 'gbpusd']

    years = sorted(cc_all['expiry'].dropna().apply(lambda d: pd.Timestamp(d).year).unique())

    new_rows, delete_specs = [], []
    for year in years:
        for cocarb_m, (cc_m, lcc_m, lcc_off) in COCARB_PAIR_MAP.items():
            cc_name     = _clean_name('CC', cc_m, year)
            lcc_name    = _clean_name('LCC', lcc_m, year + lcc_off)
            cocarb_name = _clean_name(ticker, cocarb_m, year)

            is_new     = cocarb_name not in existing_names
            fetch_from = None if (force or is_new) else global_last

            cc_df  = cc_all[cc_all['contract_name'] == cc_name]
            lcc_df = lcc_all[lcc_all['contract_name'] == lcc_name]
            if fetch_from is not None:
                cc_df  = cc_df[cc_df['date'] >= fetch_from]
                lcc_df = lcc_df[lcc_df['date'] >= fetch_from]
            if cc_df.empty or lcc_df.empty:
                continue

            m = cc_df.merge(lcc_df, on='date', suffixes=('_cc', '_lcc'))
            if m.empty:
                continue
            m = m.merge(gbp_full, on='date', how='inner')
            if m.empty:
                continue

            m['close']  = m['close_cc'] - m['close_lcc'] * m['gbpusd']
            m['expiry'] = m[['expiry_cc', 'expiry_lcc']].min(axis=1)
            m['dte']    = (m['expiry'] - m['date']).dt.days
            m['fnd']    = m['fnd_cc']   # LCC has no FND — CC leg only
            m['dtf']    = (m['fnd'] - m['date']).dt.days
            m['ticker'] = ticker
            m['ric']    = cocarb_name
            m['contract_name'] = cocarb_name

            new_rows.append(m[PRICES_COLS])
            delete_specs.append((cocarb_name, fetch_from))

    prices_df = _apply_updates(prices_df, ticker, existing, new_rows, delete_specs)
    log.info("COCARB: %d contract-vintages updated", len(new_rows))
    return prices_df


# ── spreads / flies (generic across all 11 markets) ─────────────────────────
def _compute_spreads(prices_df: pd.DataFrame, cfg) -> pd.DataFrame:
    ticker = cfg.ticker
    p = prices_df[prices_df['ticker'] == ticker]
    if p.empty:
        return pd.DataFrame(columns=SPREADS_COLS)

    years = sorted(p['expiry'].dropna().apply(lambda d: pd.Timestamp(d).year).unique())
    rows = []
    for year in years:
        for pair in cfg.spread_pairs:
            front_name = _clean_name(ticker, pair.front_month, year)
            back_name  = _clean_name(ticker, pair.back_month, year + pair.back_year_offset)
            name       = f"{ticker}{pair.spread_type}{year % 100:02d}"

            f = p[p['contract_name'] == front_name][['date', 'close', 'dte']]
            b = p[p['contract_name'] == back_name][['date', 'close']]
            if f.empty or b.empty:
                continue
            m = f.merge(b, on='date', suffixes=('_f', '_b'))
            if m.empty:
                continue

            m['spread']      = m['close_f'] - m['close_b']
            m['spread_type'] = pair.spread_type
            m['vintage']     = front_name
            m['front_name']  = front_name
            m['back_name']   = back_name
            m['name']        = name
            m['ticker']      = ticker
            rows.append(m[SPREADS_COLS])

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=SPREADS_COLS)


def _compute_flies(spreads_df: pd.DataFrame, cfg) -> pd.DataFrame:
    ticker = cfg.ticker
    s = spreads_df[spreads_df['ticker'] == ticker]
    if s.empty:
        return pd.DataFrame(columns=FLIES_COLS)

    years = sorted(s['vintage'].apply(lambda v: 2000 + int(v[-2:])).unique())
    rows = []
    for year in years:
        for fly in cfg.fly_defs:
            front_vintage = _clean_name(ticker, fly.front_spread.front_month, year)
            back_vintage  = _clean_name(ticker, fly.back_spread.front_month, year + fly.back_year_offset)
            name          = f"{ticker}{fly.fly_type}{year % 100:02d}"

            f = s[(s['spread_type'] == fly.front_spread.spread_type) &
                  (s['vintage'] == front_vintage)][['date', 'spread', 'dte']]
            b = s[(s['spread_type'] == fly.back_spread.spread_type) &
                  (s['vintage'] == back_vintage)][['date', 'spread']]
            if f.empty or b.empty:
                continue
            m = f.merge(b, on='date', suffixes=('_f', '_b'))
            if m.empty:
                continue

            m['fly']          = m['spread_f'] - m['spread_b']
            m['fly_type']     = fly.fly_type
            m['vintage']      = front_vintage
            m['front_spread'] = fly.front_spread.spread_type
            m['back_spread']  = fly.back_spread.spread_type
            m['name']         = name
            m['ticker']       = ticker
            rows.append(m[FLIES_COLS])

    return pd.concat(rows, ignore_index=True) if rows else pd.DataFrame(columns=FLIES_COLS)


# ── main ──────────────────────────────────────────────────────────────────────
def main(force: bool = False, tickers: list[str] | None = None):
    """
    tickers: optional subset of direct-market tickers to fetch (e.g. ['KC', 'CT']).
             When set, GBP + derived markets (WP/CFARB/COCARB) are skipped — they
             need their underlying legs fetched too, so run without a filter once
             all the direct markets they depend on have data.
    """
    today = date.today()
    log.info("=" * 60)
    log.info("Seasonal Dashboard ingest — %s (force=%s, tickers=%s)",
             today, force, tickers or 'ALL')
    log.info("=" * 60)

    DB_DIR.mkdir(parents=True, exist_ok=True)

    prices_df  = _load(PRICES_PARQUET, PRICES_COLS)
    skipped_df = _load(SKIPPED_PARQUET, SKIPPED_COLS)
    gbp_df     = _load(GBP_PARQUET, ['date', 'gbpusd'])

    if not prices_df.empty:
        prices_df['date']   = pd.to_datetime(prices_df['date'])
        prices_df['expiry'] = pd.to_datetime(prices_df['expiry'])
        prices_df['fnd']    = pd.to_datetime(prices_df['fnd'])
    if not gbp_df.empty:
        gbp_df['date'] = pd.to_datetime(gbp_df['date'])

    markets_to_fetch = DIRECT_MARKETS if tickers is None else \
        [m for m in DIRECT_MARKETS if m.ticker in tickers]
    unknown = set(tickers or []) - {m.ticker for m in DIRECT_MARKETS}
    if unknown:
        log.warning("Unknown/non-direct tickers ignored (derived markets aren't fetched "
                     "directly, run without --tickers to include them): %s", unknown)

    ld.open_session()
    try:
        for cfg in markets_to_fetch:
            prices_df, skipped_df = _ingest_direct_market(cfg, prices_df, skipped_df, today, force=force)

        if tickers is None:
            gbp_df = _ingest_gbp(gbp_df, today, force=force)
            prices_df = _ingest_wp(prices_df, force=force)
            prices_df = _ingest_cfarb(prices_df, force=force)
            prices_df = _ingest_cocarb(prices_df, gbp_df, force=force)
    finally:
        ld.close_session()

    markets_for_spreads = ALL_MARKETS if tickers is None else markets_to_fetch
    log.info("Computing spreads and flies for %d market(s)...", len(markets_for_spreads))
    spread_frames, fly_frames = [], []
    for cfg in markets_for_spreads:
        sp = _compute_spreads(prices_df, cfg)
        spread_frames.append(sp)
        fly_frames.append(_compute_flies(sp, cfg))

    new_spreads_df = pd.concat(spread_frames, ignore_index=True) if spread_frames else pd.DataFrame(columns=SPREADS_COLS)
    new_flies_df   = pd.concat(fly_frames, ignore_index=True) if fly_frames else pd.DataFrame(columns=FLIES_COLS)

    # Partial runs (tickers filter) must not clobber other markets' already-saved
    # spreads/flies — carry forward whatever's on disk for tickers not touched this run.
    touched = {cfg.ticker for cfg in markets_for_spreads}
    old_spreads_df = _load(SPREADS_PARQUET, SPREADS_COLS)
    old_flies_df   = _load(FLIES_PARQUET, FLIES_COLS)
    kept_spreads   = old_spreads_df[~old_spreads_df['ticker'].isin(touched)] if not old_spreads_df.empty else old_spreads_df
    kept_flies     = old_flies_df[~old_flies_df['ticker'].isin(touched)] if not old_flies_df.empty else old_flies_df

    spreads_parts = [d for d in (kept_spreads, new_spreads_df) if not d.empty]
    flies_parts   = [d for d in (kept_flies, new_flies_df) if not d.empty]
    spreads_df = pd.concat(spreads_parts, ignore_index=True) if spreads_parts else pd.DataFrame(columns=SPREADS_COLS)
    flies_df   = pd.concat(flies_parts, ignore_index=True) if flies_parts else pd.DataFrame(columns=FLIES_COLS)

    prices_df  = prices_df.sort_values(['ticker', 'contract_name', 'date']).reset_index(drop=True)
    spreads_df = spreads_df.sort_values(['ticker', 'vintage', 'date']).reset_index(drop=True)
    flies_df   = flies_df.sort_values(['ticker', 'vintage', 'date']).reset_index(drop=True)

    prices_df.to_parquet(PRICES_PARQUET, index=False)
    spreads_df.to_parquet(SPREADS_PARQUET, index=False)
    flies_df.to_parquet(FLIES_PARQUET, index=False)
    gbp_df.to_parquet(GBP_PARQUET, index=False)
    skipped_df.to_parquet(SKIPPED_PARQUET, index=False)

    log.info("Saved: prices=%d rows, spreads=%d rows, flies=%d rows",
             len(prices_df), len(spreads_df), len(flies_df))
    if not prices_df.empty:
        log.info("Last date per ticker:")
        for t, d in prices_df.groupby('ticker')['date'].max().items():
            log.info("  %-8s %s", t, d.date())


def _parse_tickers_arg() -> list[str] | None:
    for arg in sys.argv[1:]:
        if arg.startswith('--tickers='):
            return [t.strip().upper() for t in arg.split('=', 1)[1].split(',') if t.strip()]
    return None


if __name__ == '__main__':
    main(force='--force' in sys.argv, tickers=_parse_tickers_arg())
