"""Main event loop for the critical layer.

Subscribes (read-only) to the base-model pub/sub channels:

  * ticks.option.{INDEX}    → per-strike tick updates
  * ticks.index.{INDEX}     → spot updates
  * candles.{INDEX}.1m / .5m → closed-candle events

For every event it updates rolling state, runs the deterministic
Python Sniper triggers, asks the regime filter (cached), and fires
paper orders through `ScalpExecutor` when everything lines up.

Structure:

                   ┌─────────────────┐
                   │ redis pub/sub   │
                   └────────┬────────┘
                            ▼
                    dispatch(channel, data)
                            │
       ┌────────────┬───────┴───────┬─────────────┐
       ▼            ▼               ▼             ▼
   _on_index   _on_option       _on_candle    _manage_positions
       │            │               │             │
       │            └──► evaluate_entry            │
       │                                           │
       └──────► _refresh_regime_if_due ────────────┘
"""

from __future__ import annotations

import signal
import threading
import time
from datetime import date, datetime
from typing import Any

from trading.critical.config import CriticalConfig, load_config
from trading.critical.entry import pick_instrument
from trading.critical.executor import LOT_SIZES, ScalpExecutor
from trading.critical.exit import build_exit_levels, evaluate as evaluate_exit
from trading.critical.levels import compute_levels, detect_migration
from trading.critical.market_view import MarketView
from trading.critical.regime.client import RegimeClient
from trading.critical.regime.prompt import RegimeInput
from trading.critical.regime.schema import RegimeDecision, allow_entry as regime_allow
from trading.critical.risk import allow_entry as risk_gate
from trading.critical.state import CriticalState, Position
from trading.critical.triggers import (
    Signal, buildup_signal, classify_buildup, combine_signals,
    level_break_signal, microstructure_signal, momentum_signal,
)
from trading.events import EventBus
from trading.expiry import get_expiries
from trading.logging_setup import get_logger
from trading.schemas import now_ms

log = get_logger(__name__)


class CriticalEngine:
    def __init__(
        self,
        indices: list[str],
        *,
        cfg: CriticalConfig | None = None,
        bus: EventBus | None = None,
        market: MarketView | None = None,
        executor: ScalpExecutor | None = None,
        regime: RegimeClient | None = None,
    ) -> None:
        self.indices = list(indices)
        self.cfg = cfg or load_config()
        self.bus = bus or EventBus()
        self.market = market or MarketView()
        self.executor = executor or ScalpExecutor(bus=self.bus)
        self.regime = regime or RegimeClient(self.cfg)
        self.state = CriticalState()
        self._expiries: dict[str, date] = {}
        self._stop = threading.Event()
        # remember last-observed OI map per (index, side) for migration
        # detection between chain snapshots
        self._prev_oi: dict[tuple[str, str], dict[int, int]] = {}

    # ---- lifecycle ----

    def start(self) -> None:
        self._expiries = get_expiries(self.indices)
        log.info("critical_start",
                 indices=self.indices,
                 expiries={k: v.isoformat() for k, v in self._expiries.items()},
                 max_concurrent=self.cfg.max_concurrent)
        channels = []
        for idx in self.indices:
            channels += [f"ticks.index.{idx}", f"ticks.option.{idx}",
                         f"candles.{idx}.1m", f"candles.{idx}.5m"]
        # Subscribe to everything we need in one pass; callback routes by prefix.
        threading.Thread(
            target=self._subscribe_forever, args=(channels,),
            name="critical-sub", daemon=True,
        ).start()
        # Periodic position manager — drives exits even if no new tick arrives.
        threading.Thread(
            target=self._manage_loop, name="critical-manage", daemon=True,
        ).start()

    def shutdown(self) -> None:
        log.info("critical_shutdown_begin")
        self._stop.set()
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("critical_shutdown_ok")

    # ---- subscriber ----

    def _subscribe_forever(self, channels: list[str]) -> None:
        # Reconnect loop — matches the pattern used elsewhere in the base.
        patterns = tuple(channels)
        while not self._stop.is_set():
            try:
                self.bus.subscribe(patterns, self._dispatch)
            except Exception as e:   # noqa: BLE001
                log.warning("critical_subscribe_error", error=str(e))
                if self._stop.wait(1.0):
                    return

    def _dispatch(self, channel: str, data: dict) -> None:
        try:
            if channel.startswith("ticks.option."):
                self._on_option_tick(data)
            elif channel.startswith("ticks.index."):
                self._on_index_tick(data)
            elif channel.startswith("candles."):
                self._on_candle(channel, data)
        except Exception as e:   # noqa: BLE001
            log.warning("critical_dispatch_failed",
                        channel=channel, error=str(e))

    # ---- event handlers ----

    def _on_index_tick(self, data: dict) -> None:
        index = data.get("index")
        if not index:
            return
        # Reset per-day counters on first tick of a new IST day.
        self.state.reset_if_new_day(datetime.now().date())

        # Exit-management: if we hold a position, evaluate on every spot tick
        idx_state = self.state.get(index)
        if idx_state.position is not None:
            self._try_exit(index, idx_state.position)

    def _on_option_tick(self, data: dict) -> None:
        index = data.get("index")
        strike = data.get("strike")
        ot = data.get("option_type")
        if not index or not isinstance(strike, int) or ot not in ("CE", "PE"):
            return
        idx_state = self.state.get(index)
        hist = idx_state.get_strike(strike, ot)  # type: ignore[arg-type]
        hist.observe(data.get("ltp"), data.get("oi"))

        # Exit-mgmt also triggers on option tick (gets us fresh LTP faster
        # than the spot tick would).
        if idx_state.position is not None:
            self._try_exit(index, idx_state.position, fresh_ltp_tick=data)

        # Entry evaluation is driven off option ticks — that's where the
        # microstructure signals live.
        if idx_state.position is None:
            self._try_entry(index)

    def _on_candle(self, channel: str, data: dict) -> None:
        tf = channel.rsplit(".", 1)[-1]
        index = data.get("index")
        if not index:
            return
        idx_state = self.state.get(index)
        buf = idx_state.candles_1m if tf == "1m" else idx_state.candles_5m \
            if tf == "5m" else None
        if buf is None:
            return
        buf.append({
            "o": data.get("open"), "h": data.get("high"),
            "l": data.get("low"), "c": data.get("close"),
            "ts": data.get("close_ts"),
        })

    # ---- entry evaluation ----

    def _try_entry(self, index: str) -> None:
        expiry = self._expiries.get(index)
        if expiry is None:
            return
        if index not in self.cfg.lots_per_index:
            return
        gate = risk_gate(
            self.state, index, ts_ms=now_ms(),
            max_concurrent=self.cfg.max_concurrent,
            cooldown_s=self.cfg.cooldown_s,
            circuit_losses=self.cfg.circuit_losses,
            no_trade_open_min=self.cfg.no_trade_open_min,
            no_trade_close_min=self.cfg.no_trade_close_min,
        )
        if not gate.allowed:
            return

        rows = self.market.get_chain_snapshot(index, expiry.isoformat())
        if not rows:
            return
        walls = self.market.get_oi_walls(index, expiry.isoformat(), n=3)
        levels = compute_levels(walls)

        # ATM strike / closest-to-spot row — where we evaluate entries
        spot = self.market.get_spot(index)
        if spot is None:
            return
        atm_row = min(rows, key=lambda r: abs(r.strike - spot))
        atm_ce = atm_row.ce_metrics or {}
        atm_pe = atm_row.pe_metrics or {}

        idx_state = self.state.get(index)

        # --- signal collection ---
        sigs: list[Signal | None] = []

        # 1. microstructure on the ATM CE + ATM PE separately — whichever
        # side has both imbalance and tick momentum contributes one signal
        ce_hist = idx_state.get_strike(atm_row.strike, "CE")
        pe_hist = idx_state.get_strike(atm_row.strike, "PE")
        sigs.append(microstructure_signal(
            spread_pct=atm_ce.get("spread_pct"),
            imbalance=atm_ce.get("imbalance"),
            ltp_momentum=ce_hist.ltp_momentum(),
            max_spread_pct=self.cfg.max_spread_pct,
        ))
        sigs.append(microstructure_signal(
            spread_pct=atm_pe.get("spread_pct"),
            imbalance=atm_pe.get("imbalance"),
            ltp_momentum=pe_hist.ltp_momentum(),
            max_spread_pct=self.cfg.max_spread_pct,
        ))

        # 2. OI buildup using the ATM CE history
        ce_tick_ltp = (atm_row.ce_tick or {}).get("ltp")
        ce_prev_ltp = ce_hist.ltps[0] if len(ce_hist.ltps) >= 2 else None
        price_delta = (
            ce_tick_ltp - ce_prev_ltp
            if isinstance(ce_tick_ltp, (int, float)) and ce_prev_ltp is not None
            else None
        )
        oi_delta = ce_hist.oi_delta()
        buildup = classify_buildup(price_delta, oi_delta)
        sigs.append(buildup_signal(buildup=buildup))

        # 3. momentum from 1-min candles
        candles = list(idx_state.candles_1m)
        if candles:
            opens = [c["o"] for c in candles if c.get("o") is not None]
            closes = [c["c"] for c in candles if c.get("c") is not None]
            if len(opens) == len(closes) and opens:
                sigs.append(momentum_signal(opens, closes, run_threshold=3))

        # 4. wall-break signal using OI migration since last snapshot
        curr_ce_oi = {
            r.strike: int((r.ce_tick or {}).get("oi") or 0)
            for r in rows if r.ce_tick
        }
        curr_pe_oi = {
            r.strike: int((r.pe_tick or {}).get("oi") or 0)
            for r in rows if r.pe_tick
        }
        prev_ce = self._prev_oi.get((index, "CE"), {})
        prev_pe = self._prev_oi.get((index, "PE"), {})
        ce_migration = detect_migration(prev_ce, curr_ce_oi)
        pe_migration = detect_migration(prev_pe, curr_pe_oi)
        self._prev_oi[(index, "CE")] = curr_ce_oi
        self._prev_oi[(index, "PE")] = curr_pe_oi
        sigs.append(level_break_signal(
            spot=spot,
            primary_resistance=levels.primary_resistance,
            primary_support=levels.primary_support,
            breaking_ce_walls=ce_migration.breaking,
            breaking_pe_walls=pe_migration.breaking,
        ))

        # Consult regime, then aggregate
        regime = self._regime_for(index)
        final = combine_signals(sigs, regime_bias=regime.bias, min_agreement=2)
        if final is None:
            return
        if not regime_allow(regime, final.side):
            log.info("critical_entry_blocked_by_regime",
                     index=index, side=final.side,
                     regime=regime.regime, bias=regime.bias,
                     confidence=regime.confidence)
            return

        # Pick an ITM contract in the configured delta window
        cand = pick_instrument(
            rows, spot, final.side,
            delta_min=self.cfg.delta_min,
            delta_max=self.cfg.delta_max,
            max_spread_pct=self.cfg.max_spread_pct,
        )
        if cand is None:
            return

        lots = self.cfg.lots_per_index[index]
        lot_size = LOT_SIZES.get(index, 1)
        target, stop, time_stop = build_exit_levels(
            entry_ltp=cand.ltp,
            spread=None,  # spread already enforced via max_spread_pct gate
            max_loss_rupees=self.cfg.max_loss_rupees,
            lots=lots, lot_size=lot_size,
            time_stop_s=self.cfg.time_stop_s, now_ms=now_ms(),
        )
        outcome = self.executor.enter(
            index=index, expiry_d=expiry, cand=cand, signal=final,
            lots=lots, target_ltp=target, stop_ltp=stop,
            time_stop_ms=time_stop, signal_ts_ms=now_ms(),
        )
        if outcome.ok and outcome.position is not None:
            idx_state.position = outcome.position

    # ---- exit evaluation ----

    def _try_exit(
        self, index: str, pos: Position, fresh_ltp_tick: dict | None = None,
    ) -> None:
        # Prefer the freshest quote available for the held instrument
        ltp = None
        if (fresh_ltp_tick
                and fresh_ltp_tick.get("strike") == pos.strike
                and fresh_ltp_tick.get("option_type") == pos.option_type):
            ltp = fresh_ltp_tick.get("ltp")
        if ltp is None:
            rows = self.market.get_chain_snapshot(index, pos.expiry_iso)
            row = next((r for r in rows if r.strike == pos.strike), None)
            if row:
                leg = row.ce_tick if pos.option_type == "CE" else row.pe_tick
                if leg:
                    ltp = leg.get("ltp")
        if not isinstance(ltp, (int, float)) or ltp <= 0:
            return
        spot = self.market.get_spot(index)
        regime = self._regime_for(index)
        walls = self.market.get_oi_walls(index, pos.expiry_iso, n=1)
        levels = compute_levels(walls)
        decision = evaluate_exit(
            pos, ltp=float(ltp), now_ms=now_ms(),
            regime_bias=regime.bias,
            spot=spot,
            primary_resistance=levels.primary_resistance,
            primary_support=levels.primary_support,
        )
        if not decision.should_exit:
            return
        self._close_position(index, pos, ltp=float(ltp),
                             reason=f"{decision.reason}: {decision.detail}")

    def _close_position(
        self, index: str, pos: Position, *, ltp: float, reason: str,
    ) -> None:
        outcome = self.executor.exit(
            pos=pos, current_ltp=ltp, reason=reason, signal_ts_ms=now_ms(),
        )
        idx_state = self.state.get(index)
        idx_state.position = None
        if outcome.ok and outcome.fill is not None:
            qty = pos.lots * pos.lot_size
            pnl = (outcome.fill.fill_price - pos.entry_ltp) * qty - outcome.fill.fees
            if pnl < 0:
                idx_state.consecutive_losses += 1
                idx_state.last_loss_ts_ms = now_ms()
            else:
                idx_state.consecutive_losses = 0

    # ---- position manager loop ----

    def _manage_loop(self) -> None:
        """Independent timer-driven loop that catches time-stops even on
        a silent feed (no option ticks arriving)."""
        while not self._stop.wait(1.0):
            for index in self.indices:
                pos = self.state.get(index).position
                if pos is not None and now_ms() >= pos.time_stop_ms:
                    self._try_exit(index, pos)

    # ---- regime ----

    def _regime_for(self, index: str) -> RegimeDecision:
        ts_ms = now_ms()
        last_ms = self.state.last_regime_update_ms.get(index, 0)
        cached = self.state.last_regime.get(index)
        if cached and ts_ms - last_ms < self.cfg.regime_interval_s * 1000:
            return RegimeDecision(**cached)

        snap = self._build_regime_input(index)
        if snap is None:
            return RegimeDecision(
                regime="ranging", bias="neutral",
                confidence=20, source="fallback",
            )
        decision = self.regime.classify(snap, ts_ms=ts_ms)
        self.state.last_regime[index] = decision.model_dump()
        self.state.last_regime_update_ms[index] = ts_ms
        try:
            self.bus.publish(f"critical.regime.{index}", decision.model_dump())
        except Exception:   # noqa: BLE001
            pass
        log.info("critical_regime",
                 index=index, regime=decision.regime, bias=decision.bias,
                 confidence=decision.confidence, source=decision.source)
        return decision

    def _build_regime_input(self, index: str) -> RegimeInput | None:
        spot = self.market.get_spot(index)
        if spot is None:
            return None
        expiry = self._expiries.get(index)
        if expiry is None:
            return None
        idx_state = self.state.get(index)
        candles = list(idx_state.candles_1m)[-15:]
        recent = tuple(
            (float(c.get("o") or 0), float(c.get("h") or 0),
             float(c.get("l") or 0), float(c.get("c") or 0))
            for c in candles if c.get("c") is not None
        )
        rows = self.market.get_chain_snapshot(index, expiry.isoformat())
        total_call_oi  = sum(int((r.ce_tick or {}).get("oi") or 0) for r in rows)
        total_put_oi   = sum(int((r.pe_tick or {}).get("oi") or 0) for r in rows)
        total_call_chg = sum(int((r.ce_tick or {}).get("oi_change") or 0) for r in rows)
        total_put_chg  = sum(int((r.pe_tick or {}).get("oi_change") or 0) for r in rows)
        walls = self.market.get_oi_walls(index, expiry.isoformat(), n=1)
        hi_ce = walls["CE"][0].strike if walls["CE"] else None
        hi_pe = walls["PE"][0].strike if walls["PE"] else None
        # Approximate "recent LTPs" with the last few candle closes — cheap
        # and deterministic.
        recent_ltps = tuple(float(c[3]) for c in recent[-5:])
        return RegimeInput(
            index=index, spot=float(spot),
            recent_candles=recent, recent_ltps=recent_ltps,
            total_call_oi=total_call_oi, total_put_oi=total_put_oi,
            total_call_oi_change=total_call_chg,
            total_put_oi_change=total_put_chg,
            highest_call_oi_strike=hi_ce,
            highest_put_oi_strike=hi_pe,
        )


def install_signal_handlers(eng: CriticalEngine) -> None:
    def _h(signum: int, _frame: Any) -> None:
        log.info("critical_signal_received", signum=signum)
        eng.shutdown()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _h)
        except (ValueError, OSError):
            pass
