"""
Market configuration for the Seasonal Dashboard ingest pipeline.

Self-contained (no dependency on the separate `futures` project) — mirrors
its RIC/expiry/FND conventions per market so the two stay in agreement, but
is a standalone module that ships with this repo.

11 tickers total:
  8 direct LSEG markets : SB, LSU, KC, LRC, CC, LCC, CT, OJ
  1 FX feed             : GBP (GBPUSD spot, only used to build COCARB)
  3 derived markets     : WP (LSU-SB), CFARB (KC-LRC), COCARB (CC-LCC*GBPUSD)
"""

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Callable, Optional


@dataclass
class SpreadDef:
    front_month:      str   # e.g. 'H'
    back_month:       str   # e.g. 'K'
    back_year_offset: int   # 0 = same year, 1 = back contract is following year
    spread_type:      str   # e.g. 'HK'


@dataclass
class FlyDef:
    fly_type:         str
    front_spread:      SpreadDef
    back_spread:       SpreadDef
    back_year_offset:  int   # year-delta between fly anchor year and back spread vintage


@dataclass
class MarketConfig:
    ticker:       str
    month_codes:  list[str]
    start_year:   int
    spread_pairs: list[SpreadDef]
    fly_defs:     list[FlyDef]
    # Direct (LSEG-fetched) markets only:
    expiry_fn:    Optional[Callable[[str, int], date]]              = None
    build_ric_fn: Optional[Callable[[str, int, date], Optional[str]]] = None
    fnd_fn:       Optional[Callable[[str, int], Optional[date]]]    = None
    is_derived:   bool = False


# ── shared business-day helpers ─────────────────────────────────────────────
def _last_biz_day(year: int, month: int) -> date:
    last = calendar.monthrange(year, month)[1]
    d = date(year, month, last)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def _first_biz_day(year: int, month: int) -> date:
    d = date(year, month, 1)
    while d.weekday() >= 5:
        d += timedelta(days=1)
    return d


def _nth_biz_day(year: int, month: int, n: int) -> date:
    d = date(year, month, 1)
    last = calendar.monthrange(year, month)[1]
    count = 0
    while d <= date(year, month, last):
        if d.weekday() < 5:
            count += 1
            if count == n:
                return d
        d += timedelta(days=1)
    raise ValueError(f"Fewer than {n} business days in {year}-{month:02d}")


def _n_biz_days_before(d: date, n: int) -> date:
    count = 0
    while count < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            count += 1
    return d


def _ice_ric(prefix: str, month_code: str, year: int, expiry: date, today: date) -> str:
    """ICE Futures convention: {PREFIX}{M}{Y} live, {PREFIX}{M}{Y}^N expired."""
    base = f"{prefix}{month_code}{year % 10}"
    if expiry >= today:
        return base
    decade = (year - 2000) // 10
    return f"{base}^{decade}"


# ── SB — Sugar No. 11 (ICE US) ──────────────────────────────────────────────
_SB_EXPIRY_MON = {'H': 2, 'K': 4, 'N': 6, 'V': 9}


def _sb_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _SB_EXPIRY_MON[mc])


def _sb_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('SB', mc, year, _sb_expiry_fn(mc, year), today)


_SB_HK = SpreadDef('H', 'K', 0, 'HK')
_SB_KN = SpreadDef('K', 'N', 0, 'KN')
_SB_NV = SpreadDef('N', 'V', 0, 'NV')
_SB_VH = SpreadDef('V', 'H', 1, 'VH')

SB = MarketConfig(
    ticker       = 'SB',
    month_codes  = ['H', 'K', 'N', 'V'],
    start_year   = 2000,
    spread_pairs = [_SB_HK, _SB_KN, _SB_NV, _SB_VH],
    fly_defs = [
        FlyDef('HKN', _SB_HK, _SB_KN, 0),
        FlyDef('KNV', _SB_KN, _SB_NV, 0),
        FlyDef('NVH', _SB_NV, _SB_VH, 0),
        FlyDef('VHK', _SB_VH, _SB_HK, 1),
    ],
    expiry_fn    = _sb_expiry_fn,
    build_ric_fn = _sb_ric_fn,
    fnd_fn       = None,
)


# ── LSU — White Sugar No. 5 (ICE Europe) ────────────────────────────────────
_LSU_EXPIRY_MON = {'H': 2, 'K': 4, 'Q': 7, 'V': 9, 'Z': 11}


def _lsu_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _LSU_EXPIRY_MON[mc])


def _lsu_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('LSU', mc, year, _lsu_expiry_fn(mc, year), today)


_LSU_HK = SpreadDef('H', 'K', 0, 'HK')
_LSU_KQ = SpreadDef('K', 'Q', 0, 'KQ')
_LSU_QV = SpreadDef('Q', 'V', 0, 'QV')
_LSU_VZ = SpreadDef('V', 'Z', 0, 'VZ')
_LSU_ZH = SpreadDef('Z', 'H', 1, 'ZH')

LSU = MarketConfig(
    ticker       = 'LSU',
    month_codes  = ['H', 'K', 'Q', 'V', 'Z'],
    start_year   = 2000,
    spread_pairs = [_LSU_HK, _LSU_KQ, _LSU_QV, _LSU_VZ, _LSU_ZH],
    fly_defs = [
        FlyDef('HKQ', _LSU_HK, _LSU_KQ, 0),
        FlyDef('KQV', _LSU_KQ, _LSU_QV, 0),
        FlyDef('QVZ', _LSU_QV, _LSU_VZ, 0),
        FlyDef('ZHK', _LSU_ZH, _LSU_HK, 1),
    ],
    expiry_fn    = _lsu_expiry_fn,
    build_ric_fn = _lsu_ric_fn,
    fnd_fn       = None,
)


# ── KC — Coffee C (ICE US) ───────────────────────────────────────────────────
_KC_DELIVERY_MON = {'H': 3, 'K': 5, 'N': 7, 'U': 9, 'Z': 12}


def _kc_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _KC_DELIVERY_MON[mc])


def _kc_fnd_fn(mc: str, year: int) -> date:
    first_biz = _first_biz_day(year, _KC_DELIVERY_MON[mc])
    return _n_biz_days_before(first_biz, 7)


def _kc_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('KC', mc, year, _kc_expiry_fn(mc, year), today)


_KC_HK = SpreadDef('H', 'K', 0, 'HK')
_KC_KN = SpreadDef('K', 'N', 0, 'KN')
_KC_NU = SpreadDef('N', 'U', 0, 'NU')
_KC_UZ = SpreadDef('U', 'Z', 0, 'UZ')
_KC_ZH = SpreadDef('Z', 'H', 1, 'ZH')

KC = MarketConfig(
    ticker       = 'KC',
    month_codes  = ['H', 'K', 'N', 'U', 'Z'],
    start_year   = 2000,
    spread_pairs = [_KC_HK, _KC_KN, _KC_NU, _KC_UZ, _KC_ZH],
    fly_defs = [
        FlyDef('HKN', _KC_HK, _KC_KN, 0),
        FlyDef('KNU', _KC_KN, _KC_NU, 0),
        FlyDef('NUZ', _KC_NU, _KC_UZ, 0),
        FlyDef('UZH', _KC_UZ, _KC_ZH, 0),
        FlyDef('ZHK', _KC_ZH, _KC_HK, 1),
    ],
    expiry_fn    = _kc_expiry_fn,
    build_ric_fn = _kc_ric_fn,
    fnd_fn       = _kc_fnd_fn,
)


# ── LRC — Robusta Coffee (ICE Europe) ───────────────────────────────────────
_LRC_DELIVERY_MON = {'F': 1, 'H': 3, 'K': 5, 'N': 7, 'U': 9, 'X': 11}


def _lrc_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _LRC_DELIVERY_MON[mc])


def _lrc_fnd_fn(mc: str, year: int) -> date:
    first_biz = _first_biz_day(year, _LRC_DELIVERY_MON[mc])
    return _n_biz_days_before(first_biz, 4)


def _lrc_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('LRC', mc, year, _lrc_expiry_fn(mc, year), today)


_LRC_FH = SpreadDef('F', 'H', 0, 'FH')
_LRC_HK = SpreadDef('H', 'K', 0, 'HK')
_LRC_KN = SpreadDef('K', 'N', 0, 'KN')
_LRC_NU = SpreadDef('N', 'U', 0, 'NU')
_LRC_UX = SpreadDef('U', 'X', 0, 'UX')
_LRC_XF = SpreadDef('X', 'F', 1, 'XF')

LRC = MarketConfig(
    ticker       = 'LRC',
    month_codes  = ['F', 'H', 'K', 'N', 'U', 'X'],
    start_year   = 2000,
    spread_pairs = [_LRC_FH, _LRC_HK, _LRC_KN, _LRC_NU, _LRC_UX, _LRC_XF],
    fly_defs = [
        FlyDef('FHK', _LRC_FH, _LRC_HK, 0),
        FlyDef('HKN', _LRC_HK, _LRC_KN, 0),
        FlyDef('KNU', _LRC_KN, _LRC_NU, 0),
        FlyDef('UXF', _LRC_UX, _LRC_XF, 0),
        FlyDef('XFH', _LRC_XF, _LRC_FH, 1),
    ],
    expiry_fn    = _lrc_expiry_fn,
    build_ric_fn = _lrc_ric_fn,
    fnd_fn       = _lrc_fnd_fn,
)


# ── CC — Cocoa (ICE US) ──────────────────────────────────────────────────────
_CC_DELIVERY_MON = {'H': 3, 'K': 5, 'N': 7, 'U': 9, 'Z': 12}


def _cc_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _CC_DELIVERY_MON[mc])


def _cc_fnd_fn(mc: str, year: int) -> date:
    sixth_biz = _nth_biz_day(year, _CC_DELIVERY_MON[mc], 6)
    return _n_biz_days_before(sixth_biz, 10)


def _cc_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('CC', mc, year, _cc_expiry_fn(mc, year), today)


_CC_HK = SpreadDef('H', 'K', 0, 'HK')
_CC_KN = SpreadDef('K', 'N', 0, 'KN')
_CC_NU = SpreadDef('N', 'U', 0, 'NU')
_CC_UZ = SpreadDef('U', 'Z', 0, 'UZ')
_CC_ZH = SpreadDef('Z', 'H', 1, 'ZH')

CC = MarketConfig(
    ticker       = 'CC',
    month_codes  = ['H', 'K', 'N', 'U', 'Z'],
    start_year   = 2000,
    spread_pairs = [_CC_HK, _CC_KN, _CC_NU, _CC_UZ, _CC_ZH],
    fly_defs = [
        FlyDef('HKN', _CC_HK, _CC_KN, 0),
        FlyDef('KNU', _CC_KN, _CC_NU, 0),
        FlyDef('NUZ', _CC_NU, _CC_UZ, 0),
        FlyDef('UZH', _CC_UZ, _CC_ZH, 0),
        FlyDef('ZHK', _CC_ZH, _CC_HK, 1),
    ],
    expiry_fn    = _cc_expiry_fn,
    build_ric_fn = _cc_ric_fn,
    fnd_fn       = _cc_fnd_fn,
)


# ── LCC — London Cocoa (ICE Europe) ─────────────────────────────────────────
_LCC_DELIVERY_MON = {'H': 3, 'K': 5, 'N': 7, 'U': 9, 'Z': 12}


def _lcc_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _LCC_DELIVERY_MON[mc])


def _lcc_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('LCC', mc, year, _lcc_expiry_fn(mc, year), today)


_LCC_HK = SpreadDef('H', 'K', 0, 'HK')
_LCC_KN = SpreadDef('K', 'N', 0, 'KN')
_LCC_NU = SpreadDef('N', 'U', 0, 'NU')
_LCC_UZ = SpreadDef('U', 'Z', 0, 'UZ')
_LCC_ZH = SpreadDef('Z', 'H', 1, 'ZH')

LCC = MarketConfig(
    ticker       = 'LCC',
    month_codes  = ['H', 'K', 'N', 'U', 'Z'],
    start_year   = 2000,
    spread_pairs = [_LCC_HK, _LCC_KN, _LCC_NU, _LCC_UZ, _LCC_ZH],
    fly_defs = [
        FlyDef('HKN', _LCC_HK, _LCC_KN, 0),
        FlyDef('KNU', _LCC_KN, _LCC_NU, 0),
        FlyDef('NUZ', _LCC_NU, _LCC_UZ, 0),
        FlyDef('UZH', _LCC_UZ, _LCC_ZH, 0),
        FlyDef('ZHK', _LCC_ZH, _LCC_HK, 1),
    ],
    expiry_fn    = _lcc_expiry_fn,
    build_ric_fn = _lcc_ric_fn,
    fnd_fn       = None,
)


# ── CT — Cotton No. 2 (ICE US) ──────────────────────────────────────────────
_CT_DELIVERY_MON = {'H': 3, 'K': 5, 'N': 7, 'Z': 12}


def _ct_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _CT_DELIVERY_MON[mc])


def _ct_fnd_fn(mc: str, year: int) -> date:
    first_biz = _first_biz_day(year, _CT_DELIVERY_MON[mc])
    return _n_biz_days_before(first_biz, 5)


def _ct_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('CT', mc, year, _ct_expiry_fn(mc, year), today)


_CT_HK = SpreadDef('H', 'K', 0, 'HK')
_CT_KN = SpreadDef('K', 'N', 0, 'KN')
_CT_NZ = SpreadDef('N', 'Z', 0, 'NZ')
_CT_ZH = SpreadDef('Z', 'H', 1, 'ZH')

CT = MarketConfig(
    ticker       = 'CT',
    month_codes  = ['H', 'K', 'N', 'Z'],
    start_year   = 2000,
    spread_pairs = [_CT_HK, _CT_KN, _CT_NZ, _CT_ZH],
    fly_defs = [
        FlyDef('HKN', _CT_HK, _CT_KN, 0),
        FlyDef('KNZ', _CT_KN, _CT_NZ, 0),
        FlyDef('NZH', _CT_NZ, _CT_ZH, 0),
        FlyDef('ZHK', _CT_ZH, _CT_HK, 1),
    ],
    expiry_fn    = _ct_expiry_fn,
    build_ric_fn = _ct_ric_fn,
    fnd_fn       = _ct_fnd_fn,
)


# ── OJ — Orange Juice (ICE US) ──────────────────────────────────────────────
_OJ_DELIVERY_MON = {'F': 1, 'H': 3, 'K': 5, 'N': 7, 'U': 9, 'X': 11}


def _oj_expiry_fn(mc: str, year: int) -> date:
    return _last_biz_day(year, _OJ_DELIVERY_MON[mc])


def _oj_fnd_fn(mc: str, year: int) -> date:
    return _first_biz_day(year, _OJ_DELIVERY_MON[mc])


def _oj_ric_fn(mc: str, year: int, today: date) -> str:
    return _ice_ric('OJ', mc, year, _oj_expiry_fn(mc, year), today)


_OJ_FH = SpreadDef('F', 'H', 0, 'FH')
_OJ_HK = SpreadDef('H', 'K', 0, 'HK')
_OJ_KN = SpreadDef('K', 'N', 0, 'KN')
_OJ_NU = SpreadDef('N', 'U', 0, 'NU')
_OJ_UX = SpreadDef('U', 'X', 0, 'UX')
_OJ_XF = SpreadDef('X', 'F', 1, 'XF')

OJ = MarketConfig(
    ticker       = 'OJ',
    month_codes  = ['F', 'H', 'K', 'N', 'U', 'X'],
    start_year   = 2000,
    spread_pairs = [_OJ_FH, _OJ_HK, _OJ_KN, _OJ_NU, _OJ_UX, _OJ_XF],
    fly_defs = [
        FlyDef('FHK', _OJ_FH, _OJ_HK, 0),
        FlyDef('HKN', _OJ_HK, _OJ_KN, 0),
        FlyDef('KNU', _OJ_KN, _OJ_NU, 0),
        FlyDef('NUX', _OJ_NU, _OJ_UX, 0),
        FlyDef('UXF', _OJ_UX, _OJ_XF, 0),
        FlyDef('XFH', _OJ_XF, _OJ_FH, 1),
    ],
    expiry_fn    = _oj_expiry_fn,
    build_ric_fn = _oj_ric_fn,
    fnd_fn       = _oj_fnd_fn,
)


DIRECT_MARKETS: list[MarketConfig] = [SB, LSU, KC, LRC, CC, LCC, CT, OJ]


# ── GBP — GBPUSD spot (FX input to COCARB only, not a tradeable market) ─────
GBP_RIC   = 'GBP='
GBP_START = date(2000, 1, 1)


# ── WP — White Premium = LSU - SB * factor ──────────────────────────────────
WP_TICKER = 'WP'
WP_FACTOR = 22.0462   # 1 LSU lot (tonne) / 1 SB lot (short ton)
# wp_month -> (sb_month, sb_year_offset)
WP_PAIR_MAP = {
    'H': ('H', 0),
    'K': ('K', 0),
    'Q': ('N', 0),
    'V': ('V', 0),
    'Z': ('H', 1),
}
_WP_HK = SpreadDef('H', 'K', 0, 'HK')
_WP_KQ = SpreadDef('K', 'Q', 0, 'KQ')
_WP_QV = SpreadDef('Q', 'V', 0, 'QV')
_WP_VZ = SpreadDef('V', 'Z', 0, 'VZ')
_WP_ZH = SpreadDef('Z', 'H', 1, 'ZH')

WP = MarketConfig(
    ticker       = WP_TICKER,
    month_codes  = ['H', 'K', 'Q', 'V', 'Z'],
    start_year   = 2000,
    spread_pairs = [_WP_HK, _WP_KQ, _WP_QV, _WP_VZ, _WP_ZH],
    fly_defs = [
        FlyDef('HKQ', _WP_HK, _WP_KQ, 0),
        FlyDef('KQV', _WP_KQ, _WP_QV, 0),
        FlyDef('QVZ', _WP_QV, _WP_VZ, 0),
        FlyDef('ZHK', _WP_ZH, _WP_HK, 1),
    ],
    is_derived = True,
)


# ── CFARB — Coffee Arbitrage = KC - LRC / factor ────────────────────────────
CFARB_TICKER = 'CFARB'
CFARB_FACTOR = 22.0462
# cfarb_month -> (kc_month, lrc_month, lrc_year_offset)
CFARB_PAIR_MAP = {
    'H':  ('H', 'H', 0),
    'K':  ('K', 'K', 0),
    'N':  ('N', 'N', 0),
    'U':  ('U', 'U', 0),
    'ZX': ('Z', 'X', 0),
    'ZF': ('Z', 'F', 1),
}
_CFARB_HK   = SpreadDef('H',  'K',  0, 'HK')
_CFARB_KN   = SpreadDef('K',  'N',  0, 'KN')
_CFARB_NU   = SpreadDef('N',  'U',  0, 'NU')
_CFARB_UZX  = SpreadDef('U',  'ZX', 0, 'UZX')
_CFARB_ZXZF = SpreadDef('ZX', 'ZF', 0, 'ZXZF')
_CFARB_ZFH  = SpreadDef('ZF', 'H',  1, 'ZFH')

CFARB = MarketConfig(
    ticker       = CFARB_TICKER,
    month_codes  = ['H', 'K', 'N', 'U', 'ZX', 'ZF'],
    start_year   = 2000,
    spread_pairs = [_CFARB_HK, _CFARB_KN, _CFARB_NU, _CFARB_UZX, _CFARB_ZXZF, _CFARB_ZFH],
    fly_defs = [
        FlyDef('HKN',   _CFARB_HK,   _CFARB_KN,   0),
        FlyDef('KNU',   _CFARB_KN,   _CFARB_NU,   0),
        FlyDef('NUZX',  _CFARB_NU,   _CFARB_UZX,  0),
        FlyDef('UZXZF', _CFARB_UZX,  _CFARB_ZXZF, 0),
        FlyDef('ZXZFH', _CFARB_ZXZF, _CFARB_ZFH,  0),
        FlyDef('ZFHK',  _CFARB_ZFH,  _CFARB_HK,   1),
    ],
    is_derived = True,
)


# ── COCARB — Cocoa Arbitrage = CC - LCC * GBPUSD ────────────────────────────
COCARB_TICKER = 'COCARB'
# cocarb_month -> (cc_month, lcc_month, lcc_year_offset)
COCARB_PAIR_MAP = {
    'H': ('H', 'H', 0),
    'K': ('K', 'K', 0),
    'N': ('N', 'N', 0),
    'U': ('U', 'U', 0),
    'Z': ('Z', 'Z', 0),
}
_COCARB_HK = SpreadDef('H', 'K', 0, 'HK')
_COCARB_KN = SpreadDef('K', 'N', 0, 'KN')
_COCARB_NU = SpreadDef('N', 'U', 0, 'NU')
_COCARB_UZ = SpreadDef('U', 'Z', 0, 'UZ')
_COCARB_ZH = SpreadDef('Z', 'H', 1, 'ZH')

COCARB = MarketConfig(
    ticker       = COCARB_TICKER,
    month_codes  = ['H', 'K', 'N', 'U', 'Z'],
    start_year   = 2000,
    spread_pairs = [_COCARB_HK, _COCARB_KN, _COCARB_NU, _COCARB_UZ, _COCARB_ZH],
    fly_defs = [
        FlyDef('HKN', _COCARB_HK, _COCARB_KN, 0),
        FlyDef('KNU', _COCARB_KN, _COCARB_NU, 0),
        FlyDef('NUZ', _COCARB_NU, _COCARB_UZ, 0),
        FlyDef('UZH', _COCARB_UZ, _COCARB_ZH, 0),
        FlyDef('ZHK', _COCARB_ZH, _COCARB_HK, 1),
    ],
    is_derived = True,
)


DERIVED_MARKETS: list[MarketConfig] = [WP, CFARB, COCARB]
ALL_MARKETS: list[MarketConfig] = DIRECT_MARKETS + DERIVED_MARKETS
TICKERS: list[str] = [m.ticker for m in ALL_MARKETS]
