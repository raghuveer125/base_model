"""Background OI poller — Fyers REST /options-chain-v3 for open interest.

Why this exists
---------------
fyers-apiv3's WebSocket output drops OI for options: inside `data_ws.py` the
`scrips` branch explicitly does `response.pop("OI")`, and the `DepthUpdate`
branch's field map (`depthvalue`) only carries L5 bid/ask prices + sizes +
order counts — no OI at all. So for F&O OI we poll REST.

Why optionchain (not quotes)
----------------------------
Tried `/data/quotes` first; it returned empty bodies on option strings
("Expecting value: line 1 column 1 (char 0)"). The purpose-built
`/options-chain-v3` endpoint instead accepts an INDEX symbol + strike count
and returns the full chain with {strike, option_type, ltp, bid, ask, oi,
prev_oi, volume} in a single request — one call per index per interval,
well inside Fyers' 10 RPS limit.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time as dtime
from typing import Any, Callable
from zoneinfo import ZoneInfo

from fyers_apiv3 import fyersModel

from trading.auth import ensure_access_token
from trading.config import get_settings
from trading.events import EventBus, ch_option_tick
from trading.logging_setup import get_logger
from trading.schemas import FYERS_INDEX_SYMBOL
from trading.storage import LiveStore

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE = dtime(15, 30)


def _expiry_epoch_seconds(d: date) -> int:
    """Fyers wants the expiry-day market-close instant as an epoch-seconds int."""
    return int(datetime.combine(d, _MARKET_CLOSE, tzinfo=_IST).timestamp())


class OIPoller:
    """Poll Fyers /options-chain-v3 per index and overlay OI on chain cache."""

    def __init__(
        self,
        get_chains: Callable[[], list[tuple[str, str, date]]],
        *,
        interval_s: float = 3.0,
        strike_count: int | None = None,
        store: LiveStore | None = None,
        bus: EventBus | None = None,
    ) -> None:
        """
        Args:
            get_chains: callable returning [(fyers_index_sym, canonical_index,
                expiry_date), ...] describing which chains to refresh.
            interval_s: seconds between full sweeps (one call per chain).
            strike_count: how many strikes each side of ATM to fetch. Defaults
                to the configured ATM window so it matches the WS subscription.
        """
        self._get_chains = get_chains
        self._interval_s = max(interval_s, 1.0)
        self._store = store or LiveStore()
        self._bus = bus or EventBus()
        self._settings = get_settings()
        self._strike_count = strike_count or self._settings.atm_strike_window
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fyers: fyersModel.FyersModel | None = None

    # ---- lifecycle ----

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="oi-poller", daemon=True,
        )
        self._thread.start()
        log.info("oi_poller_started", interval_s=self._interval_s,
                 strike_count=self._strike_count)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        log.info("oi_poller_stopped")

    # ---- internals ----

    def _client(self) -> fyersModel.FyersModel:
        if self._fyers is None:
            self._fyers = fyersModel.FyersModel(
                client_id=self._settings.fyers_client_id,
                token=ensure_access_token(),
                is_async=False,
                log_path="",
            )
        return self._fyers

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                chains = self._get_chains()
                for fyers_sym, index, expiry_d in chains:
                    if self._stop.is_set():
                        return
                    self._poll_chain(fyers_sym, index, expiry_d)
            except Exception as e:  # noqa: BLE001
                log.warning("oi_poll_failed", error=str(e))
            self._stop.wait(self._interval_s)

    def _poll_chain(self, fyers_sym: str, index: str, expiry_d: date) -> None:
        client = self._client()
        try:
            resp: Any = client.optionchain(data={
                "symbol": fyers_sym,
                "strikecount": int(self._strike_count),
                "timestamp": str(_expiry_epoch_seconds(expiry_d)),
                # We compute our own greeks; save bandwidth.
                "greeks": "0",
            })
        except Exception as e:  # noqa: BLE001
            log.warning("oi_optionchain_call_failed",
                        index=index, error=str(e))
            return

        if not isinstance(resp, dict) or resp.get("s") != "ok":
            # Fyers echoes status "ok" / "error" — log the tail for diagnosis.
            log.warning("oi_optionchain_non_ok",
                        index=index, resp=str(resp)[:300])
            return

        chain = ((resp.get("data") or {}).get("optionsChain")) or []
        expiry_iso = expiry_d.isoformat()
        updated = 0
        for row in chain:
            if not isinstance(row, dict):
                continue
            ot = row.get("option_type")
            if ot not in ("CE", "PE"):
                continue   # skip the underlying-index row at index 0
            strike = row.get("strike_price")
            oi = row.get("oi")
            if strike is None or oi is None:
                continue
            try:
                strike_int = int(strike)
                oi_int = int(oi)
            except (TypeError, ValueError):
                continue
            oi_change: int | None = None
            prev_oi = row.get("prev_oi")
            if prev_oi is not None:
                try:
                    oi_change = oi_int - int(prev_oi)
                except (TypeError, ValueError):
                    oi_change = None
            try:
                merged = self._store.merge_option_oi(
                    index, expiry_iso, strike_int, ot,
                    oi_int, oi_change=oi_change,
                )
            except Exception as e:  # noqa: BLE001
                log.warning("oi_merge_failed",
                            index=index, strike=strike_int, ot=ot, error=str(e))
                continue
            if merged is not None:
                updated += 1
                # Push the merged view to WS subscribers so the UI reflects
                # the new OI without waiting for the next WS tick (which
                # wouldn't carry OI anyway).
                try:
                    self._bus.publish(ch_option_tick(index), merged)
                except Exception as e:  # noqa: BLE001
                    log.warning("oi_publish_failed",
                                index=index, strike=strike_int, ot=ot, error=str(e))
        if updated:
            log.debug("oi_poll_updated", index=index, updated=updated)
