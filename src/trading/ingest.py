"""Ingest orchestrator — Fyers WS -> WAL -> normalize -> Redis + Postgres + pub/sub.

Critical path per message:
  1. WAL.append(raw)                 -- durability BEFORE processing
  2. Dedup.seen?                     -- drop silently if duplicate
  3. adapter.normalize_*             -- raw -> Pydantic
  4. LiveStore.set_*                 -- Redis live snapshot
  5. EventBus.publish                -- pub/sub fanout
  6. BatchBuffer -> insert_*         -- periodic Postgres flush
  7. GapDetector.observe             -- side-effect only
"""

from __future__ import annotations

import signal
import threading
from datetime import date, datetime
from typing import Any, Callable, Iterable
from collections import OrderedDict

from fyers_apiv3.FyersWebsocket import data_ws

from trading.adapter import (
    build_index_subscription,
    build_option_subscription,
    canonical_index_from_root,
    normalize_index_tick,
    normalize_option_tick,
    parse_option_symbol,
)
from trading.auth import ensure_access_token
from trading.config import get_settings
from trading.events import EventBus, ch_index_tick, ch_option_tick
from trading.logging_setup import get_logger
from trading.metrics import MetricsPublisher, metrics
from trading.schemas import FYERS_INDEX_SYMBOL, now_ms
from trading.storage import LiveStore, insert_index_ticks, insert_option_ticks
from trading.wal import get_writer, shutdown_writer

log = get_logger(__name__)


class Dedup:
    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._cache: OrderedDict[tuple[str, int], None] = OrderedDict()
        self._lock = threading.Lock()

    def seen(self, symbol: str, ts: int) -> bool:
        key = (symbol, ts)
        with self._lock:
            if key in self._cache:
                self._cache.move_to_end(key)
                return True
            self._cache[key] = None
            if len(self._cache) > self.maxsize:
                self._cache.popitem(last=False)
            return False


class GapDetector:
    def __init__(self, max_gap_s: int, on_gap: Callable[[str, int], None] | None = None) -> None:
        self.max_gap_s = max_gap_s
        self._last_ts: dict[str, int] = {}
        self._lock = threading.Lock()
        def _default_on_gap(sym: str, gap_ms: int) -> None:
            metrics.incr_gap()
            log.warning("gap_detected", symbol=sym, gap_ms=gap_ms)

        self._on_gap = on_gap or _default_on_gap

    def observe(self, symbol: str, ts_ms: int) -> None:
        with self._lock:
            prev = self._last_ts.get(symbol)
            self._last_ts[symbol] = ts_ms
        if prev is None:
            return
        gap_ms = ts_ms - prev
        if gap_ms > self.max_gap_s * 1000:
            self._on_gap(symbol, gap_ms)


class FreezeDetector:
    def __init__(
        self,
        threshold_s: int,
        get_last_ms: Callable[[], int | None],
        on_freeze: Callable[[int], None] | None = None,
    ) -> None:
        self.threshold_s = threshold_s
        self._get_last = get_last_ms
        self._on_freeze = on_freeze or (lambda idle_ms: log.error("ws_freeze", idle_ms=idle_ms))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._alerted = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, daemon=True, name="freeze-detector")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        check_every_s = max(1, self.threshold_s // 2)
        while not self._stop.wait(check_every_s):
            if not _is_market_open():
                self._alerted = False
                continue
            last = self._get_last()
            if last is None:
                continue
            idle_ms = now_ms() - last
            if idle_ms > self.threshold_s * 1000:
                if not self._alerted:
                    self._on_freeze(idle_ms)
                    self._alerted = True
            else:
                self._alerted = False


def _is_market_open() -> bool:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(get_settings().market_tz)
    now = datetime.now(tz=tz)
    if now.weekday() >= 5:
        return False
    t = now.time()
    return (t.hour, t.minute) >= (9, 15) and (t.hour, t.minute) <= (15, 30)


class BatchBuffer:
    """Time + size triggered buffer that flushes to a sink function."""

    def __init__(
        self,
        flush_fn: Callable[[list], int],
        max_items: int = 500,
        max_ms: int = 1000,
    ) -> None:
        self.flush_fn = flush_fn
        self.max_items = max_items
        self.max_ms = max_ms
        self._buf: list = []
        self._last_flush_ms = now_ms()
        self._lock = threading.Lock()

    def add(self, item: Any) -> None:
        with self._lock:
            self._buf.append(item)
            if len(self._buf) >= self.max_items or (now_ms() - self._last_flush_ms) >= self.max_ms:
                self._flush_locked()

    def flush(self) -> None:
        with self._lock:
            self._flush_locked()

    def _flush_locked(self) -> None:
        if not self._buf:
            self._last_flush_ms = now_ms()
            return
        items, self._buf = self._buf, []
        self._last_flush_ms = now_ms()
        try:
            self.flush_fn(items)
            metrics.incr_pg_flush(len(items))
        except Exception as e:  # noqa: BLE001
            log.error("batch_flush_failed", error=str(e), count=len(items))


class Orchestrator:
    def __init__(self, indices: Iterable[str], expiries: dict[str, date]) -> None:
        self.indices = list(indices)
        self.expiries = expiries
        self.settings = get_settings()

        self.store = LiveStore()
        self.bus = EventBus()
        self.wal = get_writer()

        self.dedup = Dedup(self.settings.dedup_cache_size)
        self.gaps = GapDetector(self.settings.ws_gap_max_seconds)
        self.freeze = FreezeDetector(
            threshold_s=self.settings.ws_freeze_threshold_s,
            get_last_ms=self._latest_tick_ms,
        )

        self.idx_buffer = BatchBuffer(flush_fn=insert_index_ticks)
        self.opt_buffer = BatchBuffer(flush_fn=insert_option_ticks)

        self.metrics_pub = MetricsPublisher()

        self._ws: data_ws.FyersDataSocket | None = None
        self._stop = threading.Event()
        self._ws_closed = threading.Event()
        self._subscribed: list[str] = []
        self._connected_once = False

        # OI must come from REST — Fyers v3 WS strips OI from option scrips
        # and omits it from depth frames. The poller overlays OI onto the
        # same Redis chain entries as the WS tick path.
        from trading.oi_poller import OIPoller
        self.oi_poller = OIPoller(
            get_chains=self._oi_chains_to_poll,
            bus=self.bus,
        )

    def _latest_tick_ms(self) -> int | None:
        seen: list[int] = []
        for idx in self.indices:
            v = self.store.last_seen_ms(idx)
            if v is not None:
                seen.append(v)
        return max(seen) if seen else None

    def _oi_chains_to_poll(self) -> list[tuple[str, str, date]]:
        """Return [(fyers_index_sym, canonical_index, expiry_date), ...] for
        every index that has a configured expiry — input for the OI poller."""
        out: list[tuple[str, str, date]] = []
        for idx in self.indices:
            exp = self.expiries.get(idx)
            if exp is None:
                continue
            sym = FYERS_INDEX_SYMBOL.get(idx)
            if not sym:
                continue
            out.append((sym, idx, exp))
        return out

    def _desired_symbols(self) -> list[str]:
        syms = list(build_index_subscription(self.indices))
        for idx in self.indices:
            exp = self.expiries.get(idx)
            if exp is None:
                log.warning("no_expiry_configured", index=idx)
                continue
            spot = self.store.get_spot(idx)
            if spot is None:
                log.info("spot_unknown_skipping_options", index=idx)
                continue
            syms.extend(
                build_option_subscription(
                    idx, exp, spot, window=self.settings.atm_strike_window
                )
            )
        return syms

    # ---- Depth (OI) handling ----

    # Keys that distinguish a DepthUpdate payload from a SymbolUpdate one.
    # Fyers v3 depth frames expose L5 book via bid1_price…bid5_price and the
    # matching size / order-count siblings. SymbolUpdate carries only a single
    # bid_price/ask_price (no numeric suffix).
    _DEPTH_MARKERS = ("bid1_price", "ask1_price", "bid1_size", "ask1_size")

    @classmethod
    def _is_depth_only_frame(cls, payload: dict) -> bool:
        """True when the frame looks like a DepthUpdate (no fresh LTP)."""
        if any(k in payload for k in cls._DEPTH_MARKERS):
            return True
        # Some firmware versions mark depth frames via `type` ("dp" / "depth").
        t = payload.get("type")
        if isinstance(t, str) and t.lower() in ("dp", "depth", "if"):
            return True
        return False

    def _handle_depth_oi(self, sym: str, payload: dict) -> None:
        """Merge OI from a depth frame into the cached option tick."""
        oi = payload.get("oi")
        if oi is None:
            oi = payload.get("open_interest")
        if oi is None:
            return
        prev_oi = payload.get("prev_oi")
        try:
            oi_int = int(oi)
        except (TypeError, ValueError):
            return
        oi_change: int | None = None
        if prev_oi is not None:
            try:
                oi_change = oi_int - int(prev_oi)
            except (TypeError, ValueError):
                oi_change = None
        parsed = parse_option_symbol(sym)
        if parsed is None:
            return
        index = canonical_index_from_root(parsed.root)
        if not index:
            return
        try:
            self.store.merge_option_oi(
                index, parsed.expiry.isoformat(),
                parsed.strike, parsed.option_type,
                oi_int, oi_change=oi_change,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("depth_oi_merge_failed", sym=sym, error=str(e))

    # ---- WS callbacks ----

    def _on_open(self) -> None:
        log.info("ws_open", indices=self.indices)
        syms = self._desired_symbols()
        if self._ws is None:
            return
        if syms:
            self._ws.subscribe(symbols=syms, data_type="SymbolUpdate")
            self._subscribed = syms
            log.info("ws_subscribed", count=len(syms))
            # Option symbols also get a DepthUpdate subscription — SymbolUpdate
            # alone does not carry OI for F&O on Fyers v3.
            opt_syms = [s for s in syms if s.endswith("CE") or s.endswith("PE")]
            if opt_syms:
                try:
                    self._ws.subscribe(symbols=opt_syms, data_type="DepthUpdate")
                    log.info("ws_subscribed_depth", count=len(opt_syms))
                except Exception as e:  # noqa: BLE001
                    log.warning("ws_depth_subscribe_failed", error=str(e))
        self._ws.keep_running()

    def _on_close(self, message: Any) -> None:
        log.warning("ws_close", message=str(message))
        self._ws_closed.set()

    def _on_error(self, message: Any) -> None:
        log.error("ws_error", message=str(message))

    def _on_message(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        try:
            self.wal.append(payload, kind="raw_tick")  # WAL FIRST
            metrics.incr_wal_append()
            sym = payload.get("symbol") or payload.get("sym") or ""

            if sym in FYERS_INDEX_SYMBOL.values():
                tick = normalize_index_tick(payload)
                if tick is None:
                    return
                if self.dedup.seen(sym, tick.ts_exchange):
                    metrics.incr_dedup_drop()
                    return
                self.gaps.observe(sym, tick.ts_exchange)
                metrics.observe_tick(tick.ts_exchange, tick.ts_received)
                self.store.set_index_tick(tick)
                self.bus.publish(ch_index_tick(tick.index), tick.model_dump(mode="json"))
                self.idx_buffer.add(tick)
            else:
                # Depth frames for options carry OI but often no fresh LTP.
                # Route them to a partial-merge that only touches OI so we
                # don't clobber the last-known quote.
                if self._is_depth_only_frame(payload):
                    self._handle_depth_oi(sym, payload)
                    return
                tick = normalize_option_tick(payload)
                if tick is None:
                    return
                if self.dedup.seen(sym, tick.ts_exchange):
                    metrics.incr_dedup_drop()
                    return
                self.gaps.observe(sym, tick.ts_exchange)
                metrics.observe_tick(tick.ts_exchange, tick.ts_received)
                # Publish the stored payload, which has the REST-sourced OI
                # merged in, so WS clients don't see a 0 and clobber their
                # last-known OI on every incoming tick.
                stored = self.store.set_option_tick(tick)
                self.bus.publish(ch_option_tick(tick.index), stored)
                self.opt_buffer.add(tick)
        except Exception as e:  # noqa: BLE001
            log.error("on_message_failed", error=str(e))

    # ---- lifecycle ----

    def _connect_once(self, access_token: str) -> None:
        self._ws_closed.clear()
        self._ws = data_ws.FyersDataSocket(
            access_token=access_token,
            log_path=str(self.settings.log_dir),
            litemode=False,
            write_to_file=False,
            reconnect=False,
            on_connect=self._on_open,
            on_close=self._on_close,
            on_error=self._on_error,
            on_message=self._on_message,
        )
        # connect() is non-blocking — it starts a background thread and returns.
        # We sit on self._ws_closed.wait() in run() until on_close fires.
        self._ws.connect()

    def run(self) -> None:
        log.info("ingest_start", indices=self.indices)
        self.freeze.start()
        self.metrics_pub.start()
        self.oi_poller.start()
        backoff = 1
        while not self._stop.is_set():
            if self._connected_once:
                metrics.incr_reconnect()
            connected = False
            try:
                token = ensure_access_token()
                fyers_token = f"{self.settings.fyers_client_id}:{token}"
                self._connect_once(fyers_token)
                self._connected_once = True
                connected = True
                backoff = 1
            except Exception as e:  # noqa: BLE001
                log.error("ws_connect_failed", error=str(e), backoff_s=backoff)

            if connected:
                # Block until the WS actually disconnects (on_close fires and sets
                # the event) or the orchestrator is asked to stop. Without this
                # wait we would spin-reconnect because FyersDataSocket.connect()
                # returns immediately after spawning its background thread.
                while not self._stop.is_set() and not self._ws_closed.is_set():
                    if self._stop.wait(1.0):
                        break
                if self._stop.is_set():
                    break
                log.warning("ws_disconnected_reconnecting")

            sleep_s = min(backoff, self.settings.ws_reconnect_max_backoff_s)
            if self._stop.wait(sleep_s):
                break
            backoff = min(backoff * 2, self.settings.ws_reconnect_max_backoff_s)

    def shutdown(self) -> None:
        log.info("ingest_shutdown_begin")
        self._stop.set()
        self._ws_closed.set()   # release the run-loop wait
        try:
            if self._ws is not None:
                try:
                    self._ws.close_connection()
                except Exception:  # noqa: BLE001
                    pass
            self.idx_buffer.flush()
            self.opt_buffer.flush()
        finally:
            self.oi_poller.stop()
            self.metrics_pub.stop()
            self.freeze.stop()
            shutdown_writer()
            self.bus.close()
            log.info("ingest_shutdown_ok", final=metrics.snapshot())


def install_signal_handlers(orc: Orchestrator) -> None:
    def _handler(signum, _frame):  # noqa: ANN001
        log.info("signal_received", signum=signum)
        orc.shutdown()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handler)
        except (ValueError, OSError):
            pass
