"""OrderEngine — subscribes signals.*, executes via Executor, maintains book + PnL.

Per signal:
  signal → Order {qty, ref_price, action}
         → Executor.execute → Fill
         → PositionBook.apply_fill
         → publish orders.* / fills.* / pnl.tick
         → persist to orders + fills tables

EXIT is rewritten to BUY / SELL based on the current position so the paper
executor and the book agree on direction. Zero coupling to ingest/candles/
greeks/strategies — pure subscriber.
"""

from __future__ import annotations

import threading

import orjson

from trading.config import get_settings
from trading.events import ALL_SIGNAL_CHANNELS, CH_PNL, EventBus, ch_fill, ch_order
from trading.logging_setup import get_logger
from trading.metrics import MetricsPublisher
from trading.orders.base import Executor, ExecutionResult, Order
from trading.orders.book import PositionBook
from trading.orders.paper import PaperExecutor
from trading.schemas import Signal, now_ms
from trading.storage import LiveStore, insert_fill, insert_order

log = get_logger(__name__)


class OrderEngine:
    def __init__(
        self,
        *,
        executor: Executor | None = None,
        book: PositionBook | None = None,
        bus: EventBus | None = None,
        store: LiveStore | None = None,
    ) -> None:
        self.settings = get_settings()
        self.executor = executor or PaperExecutor()
        self.book = book or PositionBook()
        self.bus = bus or EventBus()
        self.store = store or LiveStore()
        self.metrics_pub = MetricsPublisher()
        self._stop = threading.Event()

    def _resolve_ref_price(self, sig: Signal) -> float:
        meta = sig.metadata or {}
        ref = meta.get("ref_price")
        if ref:
            try:
                return float(ref)
            except (TypeError, ValueError):
                pass
        if sig.instrument == sig.index:
            spot = self.store.get_spot(sig.index)
            if spot:
                return float(spot)
        try:
            expiry = meta.get("expiry")
            strike = meta.get("strike")
            option_type = meta.get("option_type")
            if expiry and strike and option_type:
                raw = self.store.r.get(
                    f"tpp:tick:opt:{sig.index}:{expiry}:{strike}:{option_type}"
                )
                if raw:
                    data = orjson.loads(raw)
                    ltp = data.get("ltp")
                    if ltp:
                        return float(ltp)
        except Exception as e:  # noqa: BLE001
            log.warning("ref_price_lookup_failed", error=str(e))
        return 0.0

    def _resolve_qty(self, sig: Signal) -> int:
        meta = sig.metadata or {}
        q = meta.get("qty")
        if q:
            try:
                q = int(q)
                if q > 0:
                    return q
            except (TypeError, ValueError):
                pass
        return int(self.settings.orders_default_qty)

    def _on_signal(self, channel: str, data: dict) -> None:
        _ = channel
        try:
            sig = Signal.model_validate(data)
        except Exception as e:  # noqa: BLE001
            log.warning("order_signal_invalid", error=str(e))
            return
        if sig.action == "HOLD":
            return

        action = sig.action
        if action == "EXIT":
            pos = self.book.get(sig.instrument)
            if pos is None or pos.qty == 0:
                log.info("exit_without_position",
                         strategy=sig.strategy, instrument=sig.instrument)
                return
            action = "SELL" if pos.qty > 0 else "BUY"

        qty = self._resolve_qty(sig)
        ref = self._resolve_ref_price(sig)

        try:
            order = Order(
                strategy=sig.strategy, index=sig.index,
                instrument=sig.instrument, action=action,
                qty=qty, ref_price=ref,
                signal_ts=sig.ts, ordered_ts=now_ms(),
                signal_reason=sig.reason,
                signal_confidence=sig.confidence,
            )
        except Exception as e:  # noqa: BLE001
            log.warning("order_build_failed", error=str(e))
            return

        result: ExecutionResult = self.executor.execute(order)
        if not result.ok or result.fill is None:
            self._persist_rejected(order, result.reason)
            log.warning("order_rejected", strategy=sig.strategy,
                        instrument=sig.instrument,
                        reason=result.reason, ref=ref)
            return

        fill = result.fill
        order_id = self._persist_filled(order)
        fill = fill.model_copy(update={"order_id": order_id or None})
        self._persist_fill(fill)

        update = self.book.apply_fill(fill)
        self._publish(order, fill, update)
        log.info(
            "order_filled",
            strategy=order.strategy, action=order.action,
            instrument=order.instrument, qty=order.qty,
            fill_price=fill.fill_price, fees=fill.fees,
            position_qty=update["position"].qty,
            avg_price=update["position"].avg_price,
            delta_realized=update["delta_realized"],
            realized_session=self.book.realized_pnl(),
        )

    def _persist_filled(self, order: Order) -> int:
        try:
            return insert_order(order, status="filled")
        except Exception as e:  # noqa: BLE001
            log.warning("order_pg_failed", error=str(e))
            return 0

    def _persist_rejected(self, order: Order, reason: str) -> None:
        try:
            insert_order(order, status="rejected", reject_reason=reason)
        except Exception as e:  # noqa: BLE001
            log.warning("order_pg_failed", error=str(e))

    def _persist_fill(self, fill) -> None:
        try:
            insert_fill(fill)
        except Exception as e:  # noqa: BLE001
            log.warning("fill_pg_failed", error=str(e))

    def _publish(self, order: Order, fill, update: dict) -> None:
        try:
            self.bus.publish(ch_order(order.strategy),
                             order.model_dump(mode="json"))
            self.bus.publish(ch_fill(order.strategy),
                             fill.model_dump(mode="json"))
            pos = update["position"]
            self.bus.publish(CH_PNL, {
                "strategy": order.strategy,
                "instrument": order.instrument,
                "index": order.index,
                "realized_session": self.book.realized_pnl(),
                "delta_realized": update["delta_realized"],
                "position": pos.model_dump(mode="json"),
                "ts": now_ms(),
            })
        except Exception as e:  # noqa: BLE001
            log.warning("order_publish_failed", error=str(e))

    def run(self) -> None:
        log.info("order_engine_start",
                 mode=self.settings.orders_mode,
                 executor=self.executor.name,
                 default_qty=self.settings.orders_default_qty,
                 slippage_bps=self.settings.paper_slippage_bps,
                 fee_bps=self.settings.paper_fee_bps,
                 flat_fee=self.settings.paper_flat_fee)
        self.metrics_pub.start()
        try:
            self.bus.subscribe(list(ALL_SIGNAL_CHANNELS), self._on_signal)
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
        log.info("order_engine_shutdown",
                 realized_session=self.book.realized_pnl(),
                 open_positions=len(self.book.all_positions()))
