"""Greeks engine — Black-Scholes Δ, Γ, Θ, ν for the live option chain.

- Subscribes to Redis pub/sub `ticks.index.*` and `ticks.option.*`.
- On each option tick: compute Greeks for that contract using the current Redis spot,
  publish on `greeks.{INDEX}`, cache at `tpp:greeks:{INDEX}:{EXPIRY}:{STRIKE}:{TYPE}`.
- On index tick: trigger a chain-wide recompute only when spot moves more than
  `greeks_spot_trigger_points` — avoids O(N × ticks_per_sec) CPU churn.
- Only options within ATM ± `atm_strike_window` (per-index strike step) are computed.
- Black-Scholes is bucketized and memoized with lru_cache — S→int, σ→bp, T→minutes,
  r→bp — collapsing micro-jitter into a small key space.

Units:
  delta   dimensionless (CE: 0..1, PE: -1..0)
  gamma   1 / spot-points
  theta   per-day  (from per-year Black-Scholes θ / 365.25)
  vega    per 1% absolute σ  (per-σ ν × 0.01)
"""

from __future__ import annotations

import math
import threading
from datetime import date, datetime, time as dtime
from functools import lru_cache
from typing import Iterable
from zoneinfo import ZoneInfo

from trading.config import get_settings
from trading.events import EventBus, ch_greeks
from trading.logging_setup import get_logger
from trading.metrics import MetricsPublisher, metrics
from trading.schemas import (
    INDEX_STRIKE_STEP,
    OptionGreeks,
    OptionType,
    now_ms,
)
from trading.storage import LiveStore

log = get_logger(__name__)

_INV_SQRT_2 = 1.0 / math.sqrt(2.0)
_INV_SQRT_2PI = 1.0 / math.sqrt(2.0 * math.pi)

_IST = ZoneInfo("Asia/Kolkata")
_MARKET_CLOSE = dtime(15, 30)

_YEAR_MS = 365.25 * 86_400_000.0
_MINUTES_PER_YEAR = 365.25 * 24 * 60


def _phi(x: float) -> float:
    return _INV_SQRT_2PI * math.exp(-0.5 * x * x)


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x * _INV_SQRT_2))


@lru_cache(maxsize=4096)
def _bs_cached(
    option_type: str,
    strike_int: int,
    spot_int: int,
    iv_bp: int,
    t_min: int,
    r_bp: int,
) -> tuple[float, float, float, float]:
    """Return (delta, gamma, theta_per_day, vega_per_pct). Bucketed-input cache."""
    S = float(spot_int)
    K = float(strike_int)
    sigma = iv_bp / 10_000.0
    T = max(t_min, 0) / _MINUTES_PER_YEAR
    r = r_bp / 10_000.0

    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if option_type == "CE":
            delta = 1.0 if S > K else 0.0
        else:
            delta = -1.0 if S < K else 0.0
        return (delta, 0.0, 0.0, 0.0)

    sqrt_t = math.sqrt(T)
    sigma_sqrt_t = sigma * sqrt_t
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / sigma_sqrt_t
    d2 = d1 - sigma_sqrt_t
    phi_d1 = _phi(d1)

    gamma = phi_d1 / (S * sigma_sqrt_t)
    vega = (S * phi_d1 * sqrt_t) * 0.01
    disc_k = math.exp(-r * T) * K

    if option_type == "CE":
        delta = _norm_cdf(d1)
        theta_per_year = -S * phi_d1 * sigma / (2.0 * sqrt_t) - r * disc_k * _norm_cdf(d2)
    else:
        delta = _norm_cdf(d1) - 1.0
        theta_per_year = -S * phi_d1 * sigma / (2.0 * sqrt_t) + r * disc_k * _norm_cdf(-d2)

    theta_per_day = theta_per_year / 365.25
    return (delta, gamma, theta_per_day, vega)


def compute_greeks(
    option_type: OptionType,
    spot: float,
    strike: int,
    iv: float,
    time_to_expiry_years: float,
    risk_free_rate: float,
) -> tuple[float, float, float, float]:
    """Public entry. Bucketizes inputs, then calls cached BS."""
    spot_int = int(round(spot))
    iv_bp = int(round(iv * 10_000)) if iv > 0 else 0
    t_min = int(round(time_to_expiry_years * _MINUTES_PER_YEAR))
    r_bp = int(round(risk_free_rate * 10_000))
    return _bs_cached(option_type, int(strike), spot_int, iv_bp, t_min, r_bp)


def cache_info() -> dict:
    """Expose the underlying LRU stats for debugging."""
    info = _bs_cached.cache_info()
    return {
        "hits": info.hits, "misses": info.misses,
        "maxsize": info.maxsize, "currsize": info.currsize,
    }


def time_to_expiry_years(expiry_d: date, now_ms_: int | None = None) -> float:
    """Years (float) from now to 15:30 IST on `expiry_d`. Negative if past."""
    now = now_ms_ or now_ms()
    close_dt = datetime.combine(expiry_d, _MARKET_CLOSE, tzinfo=_IST)
    close_ms = int(close_dt.timestamp() * 1000)
    return (close_ms - now) / _YEAR_MS


class GreeksEngine:
    def __init__(
        self,
        indices: Iterable[str],
        *,
        bus: EventBus | None = None,
        store: LiveStore | None = None,
        atm_range: int | None = None,
        spot_trigger_points: float | None = None,
    ) -> None:
        self.settings = get_settings()
        self.indices = list(indices)
        self.bus = bus or EventBus()
        self.store = store or LiveStore()
        self.metrics_pub = MetricsPublisher()
        self.atm_range = atm_range if atm_range is not None else self.settings.atm_strike_window
        self.spot_trigger = (
            spot_trigger_points
            if spot_trigger_points is not None
            else self.settings.greeks_spot_trigger_points
        )
        self._last_spot: dict[str, float] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def _in_atm_range(self, index: str, strike: int, spot: float) -> bool:
        step = INDEX_STRIKE_STEP.get(index)
        if not step:
            return False
        return abs(strike - spot) <= self.atm_range * step

    def _compute_and_publish(
        self,
        index: str,
        strike: int,
        option_type: OptionType,
        expiry_d: date,
        spot: float,
        iv: float,
    ) -> None:
        T = time_to_expiry_years(expiry_d)
        if T <= 0 or spot <= 0:
            metrics.incr_greek_skip()
            return
        delta, gamma, theta, vega = compute_greeks(
            option_type, spot, strike, iv, T, self.settings.risk_free_rate,
        )
        g = OptionGreeks(
            index=index, strike=strike, option_type=option_type, expiry=expiry_d,
            spot=spot, iv=iv if iv > 0 else None,
            time_to_expiry_years=T,
            delta=delta, gamma=gamma, theta=theta, vega=vega,
            ts=now_ms(),
        )
        try:
            self.store.set_greeks(g)
        except Exception as e:  # noqa: BLE001
            log.warning("greeks_cache_failed", error=str(e))
        try:
            self.bus.publish(ch_greeks(index), g.model_dump(mode="json"))
        except Exception as e:  # noqa: BLE001
            log.warning("greeks_publish_failed", error=str(e))
        metrics.incr_greek()

    def _on_option_tick(self, data: dict) -> None:
        index = data.get("index")
        option_type = data.get("option_type")
        expiry_iso = data.get("expiry")
        try:
            strike = int(data.get("strike") or 0)
        except (TypeError, ValueError):
            return
        if not index or option_type not in ("CE", "PE") or not expiry_iso or strike <= 0:
            return
        spot = self.store.get_spot(index)
        if spot is None or spot <= 0:
            return
        if not self._in_atm_range(index, strike, spot):
            return
        try:
            iv_f = float(data.get("iv") or 0.0)
        except (TypeError, ValueError):
            iv_f = 0.0
        try:
            expiry_d = date.fromisoformat(expiry_iso)
        except ValueError:
            return
        self._compute_and_publish(index, strike, option_type, expiry_d, spot, iv_f)

    def _on_index_tick(self, data: dict) -> None:
        index = data.get("index")
        try:
            ltp = float(data.get("ltp") or 0.0)
        except (TypeError, ValueError):
            return
        if not index or ltp <= 0:
            return
        with self._lock:
            prev = self._last_spot.get(index, 0.0)
            moved = abs(ltp - prev) if prev > 0 else float("inf")
            self._last_spot[index] = ltp
        if moved < self.spot_trigger:
            return
        self._recompute_chain(index, ltp)

    def _recompute_chain(self, index: str, spot: float) -> None:
        client = self.store.r
        pattern = f"tpp:chain:{index}:*"
        cursor: int = 0
        while True:
            cursor, keys = client.scan(cursor=cursor, match=pattern, count=32)
            for key in keys or []:
                key_s = key.decode() if isinstance(key, bytes) else key
                expiry_iso = key_s.rsplit(":", 1)[-1]
                try:
                    expiry_d = date.fromisoformat(expiry_iso)
                except ValueError:
                    continue
                chain = self.store.get_chain(index, expiry_iso)
                for field, tick in chain.items():
                    try:
                        strike_s, option_type = field.split(":")
                        strike = int(strike_s)
                    except ValueError:
                        continue
                    if option_type not in ("CE", "PE"):
                        continue
                    if not self._in_atm_range(index, strike, spot):
                        continue
                    try:
                        iv = float(tick.get("iv") or 0.0)
                    except (TypeError, ValueError):
                        iv = 0.0
                    self._compute_and_publish(
                        index, strike, option_type, expiry_d, spot, iv,
                    )
            if cursor == 0:
                break

    def _dispatch(self, channel: str, data: dict) -> None:
        try:
            if channel.startswith("ticks.option."):
                self._on_option_tick(data)
            elif channel.startswith("ticks.index."):
                self._on_index_tick(data)
        except Exception as e:  # noqa: BLE001
            log.error("greeks_dispatch_failed", channel=channel, error=str(e))

    def run(self) -> None:
        log.info(
            "greeks_engine_start",
            indices=self.indices,
            atm_range=self.atm_range,
            spot_trigger=self.spot_trigger,
        )
        self.metrics_pub.start()
        try:
            self.bus.subscribe(["ticks.index.*", "ticks.option.*"], self._dispatch)
        finally:
            self.shutdown()

    def shutdown(self) -> None:
        if self._stop.is_set():
            return
        self._stop.set()
        self.metrics_pub.stop()
        try:
            self.bus.close()
        except Exception:  # noqa: BLE001
            pass
        log.info("greeks_engine_shutdown_ok",
                 cache=cache_info(), final=metrics.snapshot())
