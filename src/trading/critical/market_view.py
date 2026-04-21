"""Read-only view of the base-model data surfaces.

The critical layer *never* mutates anything here — it only reads
`LiveStore` (Redis) and the `index_candles` Postgres table. If the
base model later restricts any of these, a single place to adapt.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from trading.derived import build_metrics
from trading.storage import LiveStore, get_pg_pool

OptionType = Literal["CE", "PE"]


@dataclass(frozen=True)
class ChainRow:
    strike: int
    ce_tick: dict[str, Any] | None
    pe_tick: dict[str, Any] | None
    ce_greeks: dict[str, Any] | None
    pe_greeks: dict[str, Any] | None
    ce_metrics: dict[str, Any] = field(default_factory=dict)
    pe_metrics: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Wall:
    strike: int
    option_type: OptionType
    oi: int
    oi_change: int | None


@dataclass(frozen=True)
class Candle:
    open_ts_ms: int
    open: float
    high: float
    low: float
    close: float
    volume: int
    tick_count: int


class MarketView:
    """Thin read-only aggregator — no caching (each call hits Redis/PG)."""

    def __init__(
        self,
        store: LiveStore | None = None,
        *,
        max_tick_age_ms: int | None = 60_000,
    ) -> None:
        """`max_tick_age_ms` sets the staleness threshold for per-leg
        tick data returned by `get_chain_snapshot`. A leg whose
        `ts_received` (or `ts_exchange`) is older than this window is
        treated as absent (`ce_tick=None` / `pe_tick=None`) so the entry
        picker can't act on zombie data lingering in Redis from a prior
        session or an out-of-window strike that stopped ticking.

        Pass `None` to disable the filter (tests use this)."""
        self._store = store or LiveStore()
        self._max_tick_age_ms = max_tick_age_ms

    # ---------- live spot + chain ----------

    def get_spot(self, index: str) -> float | None:
        return self._store.get_spot(index)

    def get_vix(self) -> float | None:
        """Latest India VIX LTP (scalar volatility gauge), or None if the
        ingest pipeline hasn't populated it yet."""
        return self._store.get_vix()

    def get_vix_change_pct_1h(self) -> float | None:
        """Percent change in India VIX vs 1h ago. None if history is
        shallower than 1h. Feeds the expiry/trend gate: sharp drops
        punish PE buyers on vega, sharp spikes raise uncertainty."""
        return self._store.vix_change_pct_1h()

    def get_chain_snapshot(
        self, index: str, expiry_iso: str,
    ) -> list[ChainRow]:
        """Assemble the full chain for an expiry, strikes sorted ascending.

        Per-leg tick freshness gate: a tick with `ts_received` /
        `ts_exchange` older than `max_tick_age_ms` is dropped. This stops
        the entry picker from selecting a stale Redis key with an
        unrealistic (out-of-date) LTP — the pattern that produced
        duplicate "NIFTY50 25000 CE @ 120.50" entries on 2026-04-21
        when today's real price was ₹0.45.
        """
        spot = self.get_spot(index)
        raw = self._store.get_chain(index, expiry_iso)
        now_ms = int(time.time() * 1000)
        by_strike: dict[int, dict[str, tuple[dict, dict | None]]] = {}
        for field_key, tick in raw.items():
            try:
                strike_s, ot = field_key.split(":")
                strike = int(strike_s)
            except ValueError:
                continue
            if ot not in ("CE", "PE"):
                continue
            if not self._is_tick_fresh(tick, now_ms):
                continue
            greeks = self._store.get_greeks(index, expiry_iso, strike, ot)
            by_strike.setdefault(strike, {})[ot] = (tick, greeks)

        rows: list[ChainRow] = []
        for strike in sorted(by_strike):
            ce = by_strike[strike].get("CE")
            pe = by_strike[strike].get("PE")
            ce_tick, ce_greeks = (ce if ce else (None, None))
            pe_tick, pe_greeks = (pe if pe else (None, None))
            ce_metrics = build_metrics(ce_tick, ce_greeks, spot) if ce_tick else {}
            pe_metrics = build_metrics(pe_tick, pe_greeks, spot) if pe_tick else {}
            rows.append(ChainRow(
                strike=strike,
                ce_tick=ce_tick, pe_tick=pe_tick,
                ce_greeks=ce_greeks, pe_greeks=pe_greeks,
                ce_metrics=ce_metrics, pe_metrics=pe_metrics,
            ))
        return rows

    def _is_tick_fresh(self, tick: dict, now_ms: int) -> bool:
        """Return True if the tick has a usable timestamp within the
        freshness window. Missing timestamps → treat as fresh (can't
        decide — preserve v1 behaviour)."""
        if self._max_tick_age_ms is None:
            return True
        ts = tick.get("ts_received") or tick.get("ts_exchange")
        if not isinstance(ts, (int, float)) or ts <= 0:
            return True
        return (now_ms - int(ts)) <= self._max_tick_age_ms

    def get_oi_walls(
        self, index: str, expiry_iso: str, n: int = 3,
    ) -> dict[OptionType, list[Wall]]:
        """Top-N OI walls per side, sorted by OI desc.

        These act as dynamic support (PE walls) / resistance (CE walls).
        Strikes with oi==0 are excluded.
        """
        rows = self.get_chain_snapshot(index, expiry_iso)
        ce: list[Wall] = []
        pe: list[Wall] = []
        for r in rows:
            if r.ce_tick:
                oi = int(r.ce_tick.get("oi") or 0)
                if oi > 0:
                    ce.append(Wall(strike=r.strike, option_type="CE",
                                   oi=oi,
                                   oi_change=r.ce_tick.get("oi_change")))
            if r.pe_tick:
                oi = int(r.pe_tick.get("oi") or 0)
                if oi > 0:
                    pe.append(Wall(strike=r.strike, option_type="PE",
                                   oi=oi,
                                   oi_change=r.pe_tick.get("oi_change")))
        ce.sort(key=lambda w: w.oi, reverse=True)
        pe.sort(key=lambda w: w.oi, reverse=True)
        return {"CE": ce[:n], "PE": pe[:n]}

    # ---------- historical candles (read-only) ----------

    def get_recent_candles(
        self, index: str, timeframe: str, n: int = 30,
    ) -> list[Candle]:
        """Fetch the last `n` closed candles for the given timeframe.

        Uses the same `index_candles` table the base UI reads from;
        no writes, no schema changes.
        """
        with get_pg_pool().connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT open_ts, open, high, low, close, volume, tick_count "
                "FROM index_candles "
                "WHERE index = %s AND timeframe = %s "
                "ORDER BY open_ts DESC "
                "LIMIT %s",
                (index, timeframe, n),
            )
            rows = cur.fetchall()
        out = [
            Candle(
                open_ts_ms=int(r[0].timestamp() * 1000) if r[0] else 0,
                open=float(r[1]), high=float(r[2]),
                low=float(r[3]), close=float(r[4]),
                volume=int(r[5] or 0),
                tick_count=int(r[6] or 0),
            )
            for r in rows
        ]
        out.reverse()
        return out
