"""Auto-fetch nearest expiry dates from Fyers symbol master.

Fyers publishes a CSV symbol master at:
  https://public.fyers.in/sym_details/{EXCHANGE}_FO.csv

Each row contains: (Fytoken, Symbol Description, ..., Expiry(epoch), ...)

This module downloads the master, filters for the configured indices,
and returns the nearest expiry date >= today for each index.

Results are cached in Redis (`tpp:expiry:{INDEX}`) with a TTL so we
only hit the Fyers endpoint once per day.
"""

from __future__ import annotations

import csv
import io
import time
from datetime import date, datetime, timezone

import httpx

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.schemas import INDEX_OPTION_ROOT, Index
from trading.storage import get_redis

log = get_logger(__name__)

# Fyers symbol master CSVs (public, no auth needed)
_MASTER_URLS: dict[str, str] = {
    "NSE": "https://public.fyers.in/sym_details/NSE_FO.csv",
    "BSE": "https://public.fyers.in/sym_details/BSE_FO.csv",
}

# Redis key + TTL for caching
_REDIS_KEY = "tpp:expiry:{index}"
_CACHE_TTL_S = 12 * 3600  # 12 hours — refresh twice a day at most

# Column indices in the Fyers CSV (0-based)
# Columns: [0] Fytoken, [1] Symbol Description, [2] Exchange Instrument type,
#   [3] Lot size, [4] Tick size, [5] ISIN, [6] Trading Session,
#   [7] Last update date, [8] Expiry date (epoch seconds),
#   [9] Symbol ticker (e.g. NSE:NIFTY26APR25000CE),
#   [10] Exchange code, [11] Segment, [12] Scrip code,
#   [13] Underlying/Root name (NIFTY, BANKNIFTY, SENSEX, ...),
#   [14] Underlying scrip code, [15] Strike price,
#   [16] Option type (CE, PE, XX=futures), [17] Underlying FyToken, ...
_COL_EXPIRY_EPOCH = 8
_COL_ROOT = 13          # e.g. "NIFTY", "BANKNIFTY", "SENSEX"
_COL_OPTION_TYPE = 16   # "CE", "PE", or "XX" (futures)
_MIN_COLS = 17


def _fetch_master(exchange: str) -> list[list[str]]:
    """Download and parse the Fyers symbol master CSV."""
    url = _MASTER_URLS[exchange]
    log.info("fetching_symbol_master", exchange=exchange, url=url)
    with httpx.Client(timeout=30.0) as client:
        r = client.get(url)
        r.raise_for_status()
    reader = csv.reader(io.StringIO(r.text))
    return list(reader)


def _nearest_expiry_from_master(
    rows: list[list[str]], option_root: str, today: date,
) -> date | None:
    """Find the nearest expiry >= today for the given option root."""
    candidates: set[date] = set()
    for row in rows:
        if len(row) < _MIN_COLS:
            continue
        # Filter: only options (CE/PE), not futures (XX)
        opt_type = row[_COL_OPTION_TYPE].strip()
        if opt_type not in ("CE", "PE"):
            continue
        # Match the underlying root name exactly
        root = row[_COL_ROOT].strip().upper()
        if root != option_root:
            continue
        # Parse expiry epoch
        try:
            epoch = int(row[_COL_EXPIRY_EPOCH].strip())
            exp_date = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        except (ValueError, OSError):
            continue
        if exp_date >= today:
            candidates.add(exp_date)

    if not candidates:
        return None
    return min(candidates)


def fetch_expiry(index: str, today: date | None = None) -> date:
    """Fetch the nearest expiry for an index from Fyers symbol master.

    Raises RuntimeError if no valid expiry is found.
    """
    today = today or date.today()
    root = INDEX_OPTION_ROOT[index]
    exchange = "BSE" if index == Index.SENSEX.value else "NSE"
    rows = _fetch_master(exchange)
    exp = _nearest_expiry_from_master(rows, root, today)
    if exp is None:
        raise RuntimeError(
            f"no expiry found for {index} (root={root}) on or after {today} "
            f"in Fyers {exchange}_FO symbol master"
        )
    log.info("expiry_resolved", index=index, expiry=exp.isoformat(), source="fyers_master")
    return exp


def fetch_all_expiries(
    indices: list[str] | None = None, today: date | None = None,
) -> dict[str, date]:
    """Fetch nearest expiry for all configured indices.

    Returns dict like {"NIFTY50": date(2026, 4, 30), ...}.
    """
    indices = indices or get_settings().index_list
    today = today or date.today()
    result: dict[str, date] = {}

    # Group by exchange to avoid downloading the same CSV twice
    by_exchange: dict[str, list[str]] = {}
    for idx in indices:
        exch = "BSE" if idx == Index.SENSEX.value else "NSE"
        by_exchange.setdefault(exch, []).append(idx)

    for exchange, idx_list in by_exchange.items():
        rows = _fetch_master(exchange)
        for idx in idx_list:
            root = INDEX_OPTION_ROOT[idx]
            exp = _nearest_expiry_from_master(rows, root, today)
            if exp is None:
                raise RuntimeError(
                    f"no expiry found for {idx} (root={root}) on or after {today}"
                )
            result[idx] = exp
            log.info("expiry_resolved", index=idx, expiry=exp.isoformat())

    return result


def get_expiries(indices: list[str] | None = None) -> dict[str, date]:
    """Get expiries — from Redis cache if fresh, else fetch from Fyers.

    This is the main entry point. Call it anywhere you need expiries.
    """
    indices = indices or get_settings().index_list
    r = get_redis()
    today = date.today()
    result: dict[str, date] = {}
    missing: list[str] = []

    # Check cache first
    for idx in indices:
        key = _REDIS_KEY.format(index=idx)
        cached = r.get(key)
        if cached:
            exp = date.fromisoformat(cached.decode())
            # Only use cache if expiry hasn't passed
            if exp >= today:
                result[idx] = exp
                log.debug("expiry_cache_hit", index=idx, expiry=exp.isoformat())
                continue
        missing.append(idx)

    # Fetch missing from Fyers
    if missing:
        log.info("fetching_expiries", indices=missing)
        fetched = fetch_all_expiries(missing, today)
        for idx, exp in fetched.items():
            result[idx] = exp
            # Cache in Redis
            key = _REDIS_KEY.format(index=idx)
            r.set(key, exp.isoformat(), ex=_CACHE_TTL_S)
            log.info("expiry_cached", index=idx, expiry=exp.isoformat(), ttl_s=_CACHE_TTL_S)

    return result
