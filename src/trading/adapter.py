"""Adapter layer — normalize raw Fyers payloads into canonical Pydantic models.

Pure functions; no I/O. Caller supplies spot, expiry, etc.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Iterable

from trading.logging_setup import get_logger
from trading.schemas import (
    FYERS_INDEX_SYMBOL,
    FYERS_VIX_SYMBOL,
    INDEX_OPTION_ROOT,
    INDEX_STRIKE_STEP,
    Index,
    IndexTick,
    OptionTick,
    OptionType,
    now_ms,
)

log = get_logger(__name__)


def align_strike(index: str, price: float) -> int:
    step = INDEX_STRIKE_STEP[index]
    return int(round(price / step) * step)


def atm_strikes(index: str, spot: float, window: int) -> list[int]:
    step = INDEX_STRIKE_STEP[index]
    atm = align_strike(index, spot)
    return [atm + i * step for i in range(-window, window + 1)]


_MONTH_CODE = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
    "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
    "O": 10, "N": 11, "D": 12,
}

_SYMBOL_MONTHLY = re.compile(
    r"^(?P<exch>[A-Z]+):(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mon>[A-Z]{3})(?P<strike>\d+)(?P<ot>CE|PE)$"
)
_SYMBOL_WEEKLY = re.compile(
    r"^(?P<exch>[A-Z]+):(?P<root>[A-Z]+)(?P<yy>\d{2})(?P<mon>[A-Z1-9])(?P<dd>\d{2})(?P<strike>\d+)(?P<ot>CE|PE)$"
)


@dataclass(frozen=True)
class ParsedOptionSymbol:
    exchange: str
    root: str
    expiry: date
    strike: int
    option_type: OptionType


def parse_option_symbol(symbol: str) -> ParsedOptionSymbol | None:
    m = _SYMBOL_WEEKLY.match(symbol)
    if m:
        yy = 2000 + int(m["yy"])
        mon = _MONTH_CODE.get(m["mon"].upper())
        if mon is None:
            return None
        return ParsedOptionSymbol(
            exchange=m["exch"], root=m["root"],
            expiry=date(yy, mon, int(m["dd"])),
            strike=int(m["strike"]),
            option_type=m["ot"],  # type: ignore[arg-type]
        )
    m = _SYMBOL_MONTHLY.match(symbol)
    if m:
        yy = 2000 + int(m["yy"])
        mon = _MONTH_CODE.get(m["mon"].upper())
        if mon is None:
            return None
        return ParsedOptionSymbol(
            exchange=m["exch"], root=m["root"],
            expiry=date(yy, mon, _last_tuesday(yy, mon)),
            strike=int(m["strike"]),
            option_type=m["ot"],  # type: ignore[arg-type]
        )
    return None


def _last_tuesday(year: int, month: int) -> int:
    """Last Tuesday of the month — the post-SEBI-2024 monthly F&O expiry day
    for NIFTY, BANKNIFTY, and SENSEX. Was last Thursday pre-Oct-2024 for NSE."""
    for week in reversed(calendar.monthcalendar(year, month)):
        if week[calendar.TUESDAY] != 0:
            return week[calendar.TUESDAY]
    return 28


_INDEX_FROM_FYERS = {v: k for k, v in FYERS_INDEX_SYMBOL.items()}
_INDEX_FROM_ROOT = {v: k for k, v in INDEX_OPTION_ROOT.items()}


def canonical_index_from_fyers(symbol: str) -> str | None:
    return _INDEX_FROM_FYERS.get(symbol)


def canonical_index_from_root(root: str) -> str | None:
    return _INDEX_FROM_ROOT.get(root)


def _get(d: dict, *keys: str, default=None):
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return default


def normalize_index_tick(payload: dict) -> IndexTick | None:
    sym = _get(payload, "symbol", "sym")
    if not sym:
        return None
    index = canonical_index_from_fyers(sym)
    if not index:
        return None
    ltp = _get(payload, "ltp", "last_traded_price", "lp")
    ts_ex = _get(payload, "exch_feed_time", "timestamp", "tt", default=0)
    if ltp is None:
        return None
    try:
        return IndexTick(
            index=index, ltp=float(ltp),
            ts_exchange=int(_to_ms(ts_ex)), ts_received=now_ms(),
        )
    except (ValueError, TypeError) as e:
        log.warning("normalize_index_failed", sym=sym, error=str(e))
        return None


def _as_float(v) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    # Fyers sometimes sends 0 as a sentinel "no quote" — pass through as-is;
    # callers distinguish via None vs 0 explicitly.
    return f


def _as_int(v) -> int | None:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def normalize_option_tick(payload: dict) -> OptionTick | None:
    sym = _get(payload, "symbol", "sym")
    if not sym:
        return None
    parsed = parse_option_symbol(sym)
    if not parsed:
        return None
    index = canonical_index_from_root(parsed.root)
    if not index:
        return None
    ltp = _get(payload, "ltp", "last_traded_price", "lp", default=0)
    oi = _get(payload, "oi", "open_interest", default=0)
    oi_change = _get(payload, "oich", "oi_change", default=0)
    iv = _get(payload, "iv", "implied_volatility")
    ts_ex = _get(payload, "exch_feed_time", "timestamp", "tt", default=0)

    # Microstructure — Fyers WS v3 field names, plus short-form fallbacks
    bid = _as_float(_get(payload, "bid_price", "bid", "bp"))
    ask = _as_float(_get(payload, "ask_price", "ask", "ap"))
    bid_qty = _as_int(_get(payload, "bid_size", "bid_qty", "bq1", "bq"))
    ask_qty = _as_int(_get(payload, "ask_size", "ask_qty", "aq1", "aq"))
    volume = _as_int(_get(payload, "vol_traded_today", "volume", "v"))
    prev_close = _as_float(_get(payload, "prev_close_price", "prev_close"))
    change = _as_float(_get(payload, "ch", "change"))
    change_pct = _as_float(_get(payload, "chp", "change_pct", "change_percent"))

    try:
        ltp_f = float(ltp)
    except (TypeError, ValueError) as e:
        log.warning("normalize_option_failed", sym=sym, error=str(e))
        return None

    # Derive change / change_pct from prev_close when the feed didn't carry them.
    if change is None and prev_close is not None and prev_close > 0:
        change = ltp_f - prev_close
    if change_pct is None and prev_close is not None and prev_close > 0:
        change_pct = (ltp_f - prev_close) / prev_close * 100.0

    try:
        return OptionTick(
            index=index, strike=parsed.strike, option_type=parsed.option_type,
            expiry=parsed.expiry, ltp=ltp_f,
            oi=int(oi or 0), oi_change=int(oi_change or 0),
            iv=float(iv) if iv is not None else None,
            bid=bid, ask=ask, bid_qty=bid_qty, ask_qty=ask_qty,
            volume=volume, prev_close=prev_close,
            change=change, change_pct=change_pct,
            ts_exchange=int(_to_ms(ts_ex)), ts_received=now_ms(),
        )
    except (ValueError, TypeError) as e:
        log.warning("normalize_option_failed", sym=sym, error=str(e))
        return None


def _to_ms(ts: int | float | str) -> int:
    """Coerce seconds/ms/ns/ISO to epoch milliseconds."""
    if isinstance(ts, str):
        try:
            return int(datetime.fromisoformat(ts).timestamp() * 1000)
        except ValueError:
            return 0
    v = int(ts)
    if v > 1_000_000_000_000_00:   # ns
        return v // 1_000_000
    if v > 1_000_000_000_000:       # ms
        return v
    if v > 1_000_000_000:           # seconds
        return v * 1000
    return v


def build_index_subscription(indices: Iterable[str]) -> list[str]:
    """Build the Fyers WS subscription list for the configured indices.

    Always includes the India VIX symbol so the regime classifier has a
    live volatility signal — VIX is a scalar, so it doesn't cost
    chain / greeks / candle work downstream.
    """
    out: list[str] = []
    for idx in indices:
        sym = FYERS_INDEX_SYMBOL.get(idx)
        if sym:
            out.append(sym)
        else:
            log.warning("no_symbol_for_index", index=idx)
    out.append(FYERS_VIX_SYMBOL)
    return out


def _is_last_weekday_of_month(d: date) -> bool:
    """True if `d` is the final occurrence of its weekday in its calendar month.

    Fyers uses the monthly symbol format (e.g. NIFTY26APR25000CE) whenever an
    expiry falls on the last occurrence of its weekday in the month — which
    applies to NIFTY (last Tuesday post-Oct-2024), BANKNIFTY (last Tuesday
    post-Nov-2024 monthly-only) and SENSEX (last Tuesday). Weekly format is
    used for mid-month weekly expiries.
    """
    cal = calendar.monthcalendar(d.year, d.month)
    wd = d.weekday()
    for week in reversed(cal):
        if week[wd] != 0:
            return week[wd] == d.day
    return False


def build_option_subscription(
    index: str, expiry: date, spot: float, window: int,
) -> list[str]:
    """Build Fyers option subscription symbols for ATM ± window strikes.

    Uses the monthly format (YY + MMM) when `expiry` is the last occurrence of
    its weekday in the month, else the weekly format (YY + monthcode + DD).
    Nifty / BankNifty weeklies were discontinued in late 2024, so operators
    should pass the monthly expiry for those indices.
    """
    root = INDEX_OPTION_ROOT[index]
    exch = "NSE" if index != Index.SENSEX.value else "BSE"
    yy = expiry.strftime("%y")

    if _is_last_weekday_of_month(expiry):
        # Monthly format: e.g. NSE:NIFTY26APR25000CE
        mon_abbr = expiry.strftime("%b").upper()
        prefix = f"{exch}:{root}{yy}{mon_abbr}"
    else:
        # Weekly format: e.g. NSE:NIFTY2642125000CE (YY + mcode + DD)
        mon_num = expiry.month
        if mon_num <= 9:
            mon_code = str(mon_num)
        elif mon_num == 10:
            mon_code = "O"
        elif mon_num == 11:
            mon_code = "N"
        else:
            mon_code = "D"
        dd = expiry.strftime("%d")
        prefix = f"{exch}:{root}{yy}{mon_code}{dd}"

    symbols: list[str] = []
    for strike in atm_strikes(index, spot, window):
        for ot in ("CE", "PE"):
            symbols.append(f"{prefix}{strike}{ot}")
    return symbols
