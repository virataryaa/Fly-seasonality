"""
Futures Seasonal Dashboard — Streamlit.

Seasonality overlay chart (DTE/DTF x-axis, year-over-year comparison) for
11 soft-commodity futures markets, reading from the parquet files that
Code/ingest.py produces in ../Database.
"""

import sys
import pathlib
from datetime import date, timedelta

import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
import streamlit as st

_HERE = pathlib.Path(__file__).resolve().parent
_CODE = _HERE.parent / "Code"
if str(_CODE) not in sys.path:
    sys.path.insert(0, str(_CODE))

from market_configs import ALL_MARKETS  # noqa: E402

MARKET_CONFIGS = {m.ticker: m for m in ALL_MARKETS}
TICKERS        = [m.ticker for m in ALL_MARKETS]
COLORS         = px.colors.qualitative.Dark24

DB_DIR           = _HERE.parent / "Database"
PRICES_PARQUET   = DB_DIR / "prices.parquet"
SPREADS_PARQUET  = DB_DIR / "spreads.parquet"
FLIES_PARQUET    = DB_DIR / "flies.parquet"

DEFAULTS = dict(
    market    = TICKERS[0],
    table     = 'spreads',
    xaxis     = 'dte',
    max_days  = 500,
    year_mode = 'last_n',
    year_n    = 5,
    overlay_n = 30,
)

st.set_page_config(page_title="Futures Seasonal Dashboard", layout="wide")
st.title("Futures Seasonal Dashboard")


# ── data loading ──────────────────────────────────────────────────────────────
@st.cache_data(ttl=3600)
def load_data():
    if not PRICES_PARQUET.exists():
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    prices  = pd.read_parquet(PRICES_PARQUET)
    spreads = pd.read_parquet(SPREADS_PARQUET)
    flies   = pd.read_parquet(FLIES_PARQUET)
    for df in (prices, spreads, flies):
        if not df.empty:
            df['date'] = pd.to_datetime(df['date'])
    if not prices.empty:
        prices['expiry'] = pd.to_datetime(prices['expiry'])
        prices['fnd']    = pd.to_datetime(prices['fnd'])
    return prices, spreads, flies


def get_contract_types(ticker: str, table: str) -> list[str]:
    cfg = MARKET_CONFIGS[ticker]
    if table == 'prices':
        return cfg.month_codes
    elif table == 'spreads':
        return [p.spread_type for p in cfg.spread_pairs]
    else:
        return [f.fly_type for f in cfg.fly_defs]


def get_last_date(prices: pd.DataFrame, ticker: str) -> str:
    sub = prices[prices['ticker'] == ticker]
    if sub.empty:
        return 'N/A'
    return sub['date'].max().strftime('%d %b %Y')


def has_fnd(prices: pd.DataFrame, ticker: str) -> bool:
    sub = prices[prices['ticker'] == ticker]
    return bool(sub['fnd'].notna().any()) if not sub.empty else False


def get_available_years(prices, spreads, flies, ticker: str, table: str, contract_type: str) -> list[int]:
    if table == 'prices':
        df = prices[(prices['ticker'] == ticker) &
                    (prices['contract_name'].str.startswith(f"{ticker}{contract_type}"))]
        years = df['expiry'].dropna().dt.year.unique()
    else:
        src, type_col = (spreads, 'spread_type') if table == 'spreads' else (flies, 'fly_type')
        df = src[(src['ticker'] == ticker) & (src[type_col] == contract_type)]
        years = df['vintage'].apply(lambda v: 2000 + int(v[-2:])).unique()
    return sorted(int(y) for y in years)


def get_live_years(prices, spreads, flies, ticker: str, table: str, contract_type: str) -> set[int]:
    cutoff = pd.Timestamp(date.today() - timedelta(days=7))
    if table == 'prices':
        df = prices[(prices['ticker'] == ticker) &
                    (prices['contract_name'].str.startswith(f"{ticker}{contract_type}")) &
                    (prices['date'] >= cutoff)]
        years = df['expiry'].dropna().dt.year.unique()
    else:
        src, type_col = (spreads, 'spread_type') if table == 'spreads' else (flies, 'fly_type')
        df = src[(src['ticker'] == ticker) & (src[type_col] == contract_type) & (src['date'] >= cutoff)]
        years = df['vintage'].apply(lambda v: 2000 + int(v[-2:])).unique()
    return {int(y) for y in years}


def _normalise_to_grid(df: pd.DataFrame, max_days: int) -> pd.DataFrame:
    """
    For each year, reindex to a complete integer DTE/DTF grid [0 … max_days]
    and backward-fill gaps (weekends, holidays) from the next higher x value
    (= the previous trading day in time). Ensures a stable denominator when
    computing averages or any other cross-year metric.
    """
    if df.empty:
        return df
    full_index = pd.RangeIndex(0, max_days + 1)
    parts = []
    for year, grp in df.groupby('year'):
        min_x = grp['x'].min()
        s = grp.set_index('x')['value'].reindex(full_index).bfill()
        s = s[s.index >= min_x].dropna()
        parts.append(pd.DataFrame({'year': year, 'x': s.index, 'value': s.values}))
    return pd.concat(parts, ignore_index=True) if parts else df


def get_seasonal_data(prices, spreads, flies, ticker: str, table: str, contract_type: str,
                       years: list[int], x_col: str, max_days: int) -> pd.DataFrame:
    if not years:
        return pd.DataFrame(columns=['year', 'x', 'value'])

    if table == 'prices':
        df = prices[(prices['ticker'] == ticker) &
                    (prices['contract_name'].str.startswith(f"{ticker}{contract_type}"))].copy()
        df['year']  = df['expiry'].dt.year
        df          = df[df['year'].isin(years)]
        df['x']     = df[x_col]
        df['value'] = df['close']
    else:
        src, type_col, value_col = (
            (spreads, 'spread_type', 'spread') if table == 'spreads' else (flies, 'fly_type', 'fly')
        )
        df = src[(src['ticker'] == ticker) & (src[type_col] == contract_type)].copy()
        df['year'] = df['vintage'].apply(lambda v: 2000 + int(v[-2:]))
        df = df[df['year'].isin(years)]

        if x_col == 'dte':
            df['x'] = df['dte']
        else:
            # dtf only lives on the prices table — join on vintage (= front contract) + date
            p = prices[prices['ticker'] == ticker][['contract_name', 'date', 'dtf']]
            df = df.merge(p, left_on=['vintage', 'date'], right_on=['contract_name', 'date'], how='inner')
            df['x'] = df['dtf']
        df['value'] = df[value_col]

    df = df.dropna(subset=['x', 'value'])
    df = df[(df['x'] >= 0) & (df['x'] <= max_days)]
    df['x'] = df['x'].astype(int)
    df = df[['year', 'x', 'value']]
    return _normalise_to_grid(df, max_days)


# ── figure builder ────────────────────────────────────────────────────────────
def build_figure(ticker, table, contract_type, x_col, max_days,
                  years, overlays, overlay_n, prices, spreads, flies) -> go.Figure:

    def empty_fig(msg=None):
        fig = go.Figure()
        fig.update_layout(
            xaxis=dict(visible=False), yaxis=dict(visible=False),
            plot_bgcolor='white', paper_bgcolor='white',
            annotations=[dict(
                text=msg or "No data",
                x=0.5, y=0.5, xref='paper', yref='paper',
                showarrow=False, font=dict(size=14, color='#888'),
            )] if msg else [],
        )
        return fig

    if not contract_type or not max_days:
        return empty_fig()

    if x_col == 'dtf' and not has_fnd(prices, ticker):
        return empty_fig(f"{ticker} does not have a First Notice Day — switch to DTE")

    if not years:
        return empty_fig("Select at least one year")

    df = get_seasonal_data(prices, spreads, flies, ticker, table, contract_type, years, x_col, max_days)
    if df.empty:
        return empty_fig("No data for the selected combination")

    ov_n          = max(1, int(overlay_n or DEFAULTS['overlay_n']))
    all_years     = get_available_years(prices, spreads, flies, ticker, table, contract_type)
    overlay_years = all_years[-ov_n:]
    overlay_df    = get_seasonal_data(prices, spreads, flies, ticker, table, contract_type,
                                       overlay_years, x_col, max_days)

    fig          = go.Figure()
    overlays     = overlays or []
    sorted_years = sorted(years)
    n_years      = len(sorted_years)
    live_years   = get_live_years(prices, spreads, flies, ticker, table, contract_type)

    for i, year in enumerate(sorted_years):
        sub = df[df['year'] == year].sort_values('x', ascending=False)
        if sub.empty:
            continue
        is_live = year in live_years
        if 'hide_series' in overlays and not is_live:
            continue
        rank = n_years - 1 - i
        if rank == 0:
            color, width = '#e63946', 2.5
        elif rank == 1:
            color, width = '#1d3557', 2.5
        else:
            color, width = COLORS[i % len(COLORS)], 1.0
        fig.add_trace(go.Scatter(
            x=sub['x'], y=sub['value'],
            mode='lines', name=str(year),
            line=dict(color=color, width=width),
            hovertemplate=(
                f"<b>{year}</b><br>{x_col.upper()}: %{{x}}<br>Value: %{{y:.2f}}<extra></extra>"
            ),
        ))

    ht  = x_col.upper() + ': %{x}<br>Value: %{y:.2f}<extra></extra>'
    lbl = f' ({ov_n}y)'

    if 'band' in overlays and not overlay_df.empty:
        p75 = overlay_df.groupby('x')['value'].quantile(0.75).sort_index()
        p25 = overlay_df.groupby('x')['value'].quantile(0.25).sort_index()
        fig.add_trace(go.Scatter(x=p75.index, y=p75.values, mode='lines', name=f'P75{lbl}',
                                  line=dict(color='rgba(99,110,250,0.5)', width=1),
                                  showlegend=False, hovertemplate=f'<b>P75{lbl}</b><br>' + ht))
        fig.add_trace(go.Scatter(x=p25.index, y=p25.values, mode='lines', name=f'P25–P75{lbl}',
                                  line=dict(color='rgba(99,110,250,0.5)', width=1),
                                  fill='tonexty', fillcolor='rgba(99,110,250,0.12)',
                                  hovertemplate=f'<b>P25{lbl}</b><br>' + ht))

    if 'band_p10' in overlays and not overlay_df.empty:
        p90 = overlay_df.groupby('x')['value'].quantile(0.90).sort_index()
        p10 = overlay_df.groupby('x')['value'].quantile(0.10).sort_index()
        fig.add_trace(go.Scatter(x=p90.index, y=p90.values, mode='lines', name=f'P90{lbl}',
                                  line=dict(color='rgba(255,127,14,0.5)', width=1),
                                  showlegend=False, hovertemplate=f'<b>P90{lbl}</b><br>' + ht))
        fig.add_trace(go.Scatter(x=p10.index, y=p10.values, mode='lines', name=f'P10–P90{lbl}',
                                  line=dict(color='rgba(255,127,14,0.5)', width=1),
                                  fill='tonexty', fillcolor='rgba(255,127,14,0.08)',
                                  hovertemplate=f'<b>P10{lbl}</b><br>' + ht))

    if 'avg' in overlays and not overlay_df.empty:
        avg = overlay_df.groupby('x')['value'].mean().sort_index(ascending=False)
        fig.add_trace(go.Scatter(x=avg.index, y=avg.values, mode='lines', name=f'Avg{lbl}',
                                  line=dict(color='black', width=2, dash='dash'),
                                  hovertemplate=f'<b>Avg{lbl}</b><br>' + ht))

    if 'max' in overlays and not overlay_df.empty:
        mx = overlay_df.groupby('x')['value'].max().sort_index(ascending=False)
        fig.add_trace(go.Scatter(x=mx.index, y=mx.values, mode='lines', name=f'Max{lbl}',
                                  line=dict(color='#2ca02c', width=1.5, dash='dot'),
                                  hovertemplate=f'<b>Max{lbl}</b><br>' + ht))

    if 'min' in overlays and not overlay_df.empty:
        mn = overlay_df.groupby('x')['value'].min().sort_index(ascending=False)
        fig.add_trace(go.Scatter(x=mn.index, y=mn.values, mode='lines', name=f'Min{lbl}',
                                  line=dict(color='#ff7f0e', width=1.5, dash='dot'),
                                  hovertemplate=f'<b>Min{lbl}</b><br>' + ht))

    value_label = 'Price' if table == 'prices' else ('Spread' if table == 'spreads' else 'Fly')
    x_label     = 'Days to Expiry' if x_col == 'dte' else 'Days to FND'

    fig.update_layout(
        title=dict(text=f"<b>{ticker} {contract_type} {value_label}</b>", font=dict(size=16)),
        xaxis_title=x_label,
        yaxis_title=value_label,
        xaxis_range=[int(max_days), 0],
        hovermode=False if 'no_hover' in overlays else 'x unified',
        legend=dict(orientation='v', x=1.01, y=1, title=dict(text='Year')),
        margin=dict(l=60, r=120, t=60, b=60),
        plot_bgcolor='white',
        paper_bgcolor='white',
        height=620,
    )
    fig.update_xaxes(showgrid=True, gridcolor='#e8e8e8', zeroline=True, zerolinecolor='#bbb')
    fig.update_yaxes(showgrid=True, gridcolor='#e8e8e8', zeroline=True, zerolinecolor='#bbb')
    return fig


# ── app ───────────────────────────────────────────────────────────────────────
prices, spreads, flies = load_data()

if prices.empty:
    st.error("No data found in Database/. Run Code/ingest.py first, then commit and push the parquet files.")
    st.stop()

with st.sidebar:
    st.markdown("## Controls")

    if st.button("Reset to defaults", use_container_width=True):
        for k in ("market_dd", "table_radio", "contract_radio", "xaxis_radio", "max_days_input",
                  "year_mode_radio", "year_n_input", "year_specific_dd", "overlays_checklist",
                  "overlay_n_input"):
            st.session_state.pop(k, None)
        st.rerun()

    market = st.selectbox("Market", TICKERS, key="market_dd",
                          index=TICKERS.index(DEFAULTS['market']))
    st.caption(f"Last data: **{get_last_date(prices, market)}**")

    table = st.radio("Instrument", ['prices', 'spreads', 'flies'], key="table_radio",
                     format_func=lambda v: {'prices': 'Outright', 'spreads': 'Spread', 'flies': 'Fly'}[v],
                     index=['prices', 'spreads', 'flies'].index(DEFAULTS['table']), horizontal=True)

    contract_types = get_contract_types(market, table)
    contract_type = st.radio("Contract", contract_types, key="contract_radio", horizontal=True) \
        if contract_types else None

    x_col = st.radio("X-axis", ['dte', 'dtf'], key="xaxis_radio",
                     format_func=lambda v: 'Days to Expiry (DTE)' if v == 'dte' else 'Days to FND (DTF)',
                     index=['dte', 'dtf'].index(DEFAULTS['xaxis']), horizontal=True)

    max_days = st.number_input("Max days to include", key="max_days_input",
                               min_value=10, max_value=2000, step=10, value=DEFAULTS['max_days'])

    st.markdown("---")
    overlays = st.multiselect(
        "Overlays", ['avg', 'band', 'band_p10', 'max', 'min', 'hide_series', 'no_hover'],
        default=['no_hover'], key="overlays_checklist",
        format_func=lambda v: {
            'avg': 'Avg', 'band': 'P25–P75', 'band_p10': 'P10–P90',
            'max': 'Max', 'min': 'Min', 'hide_series': 'Hide series', 'no_hover': 'No hover',
        }[v],
    )
    overlay_n = st.number_input("Overlay history: last N years", key="overlay_n_input",
                                min_value=1, max_value=99, step=1, value=DEFAULTS['overlay_n'])

    st.markdown("---")
    year_mode = st.radio("Display years", ['last_n', 'specific'], key="year_mode_radio",
                         format_func=lambda v: 'Last N years' if v == 'last_n' else 'Specific years',
                         index=['last_n', 'specific'].index(DEFAULTS['year_mode']))

    if contract_type:
        all_years = get_available_years(prices, spreads, flies, market, table, contract_type)
    else:
        all_years = []

    if year_mode == 'last_n':
        year_n = st.number_input("N", key="year_n_input", min_value=1, max_value=30, step=1,
                                 value=DEFAULTS['year_n'])
        years = all_years[-int(year_n):] if all_years else []
    else:
        default_years = all_years[-5:] if len(all_years) >= 5 else all_years
        years = st.multiselect("Select years", options=all_years, default=default_years,
                               key="year_specific_dd")

fig = build_figure(market, table, contract_type, x_col, max_days, years,
                   overlays, overlay_n, prices, spreads, flies)
st.plotly_chart(fig, use_container_width=True)

st.caption(f"Data updated: {get_last_date(prices, market)} | {market} {table} {contract_type or ''}")
