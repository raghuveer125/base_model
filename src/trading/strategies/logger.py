"""Signal logger — durable append-only JSONL + Postgres + pub/sub.

Per-signal write order:
  1. JSONL append (fsync)    — durability before any network I/O
  2. Postgres insert          — queryable history
  3. Redis pub/sub emit       — downstream consumers
  4. structured log event

A failure in step 2 or 3 does not roll back step 1 — the jsonl file is the
authoritative record and can be used to rebuild the other two.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import orjson

from trading.config import get_settings
from trading.events import EventBus, ch_signal
from trading.logging_setup import get_logger
from trading.schemas import Signal
from trading.storage import insert_signals

log = get_logger(__name__)


class SignalLogger:
    def __init__(
        self,
        *,
        bus: EventBus | None = None,
        jsonl_path: Path | None = None,
        pg_persist: bool = True,
    ) -> None:
        s = get_settings()
        self.bus = bus or EventBus()
        self.pg_persist = pg_persist
        self.jsonl_path = (
            Path(jsonl_path) if jsonl_path is not None
            else s.log_dir / s.strategy_signal_log_file
        )
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.jsonl_path, "ab", buffering=0)  # noqa: SIM115
        self._lock = threading.Lock()
        log.info("signal_logger_open", path=str(self.jsonl_path))

    def record(self, sig: Signal) -> None:
        line = orjson.dumps(sig.model_dump(mode="json")) + b"\n"
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.write(line)
                    os.fsync(self._fh.fileno())
                except Exception as e:  # noqa: BLE001
                    log.error("signal_jsonl_failed", error=str(e))

        if self.pg_persist:
            try:
                insert_signals([sig])
            except Exception as e:  # noqa: BLE001
                log.error("signal_pg_failed", error=str(e), strategy=sig.strategy)

        try:
            self.bus.publish(ch_signal(sig.strategy), sig.model_dump(mode="json"))
        except Exception as e:  # noqa: BLE001
            log.warning("signal_publish_failed", error=str(e))

        log.info(
            "signal",
            strategy=sig.strategy, index=sig.index, action=sig.action,
            instrument=sig.instrument, confidence=sig.confidence, reason=sig.reason,
        )

    def close(self) -> None:
        with self._lock:
            if self._fh is not None:
                try:
                    self._fh.flush()
                    os.fsync(self._fh.fileno())
                finally:
                    self._fh.close()
                    self._fh = None  # type: ignore[assignment]
        log.info("signal_logger_closed")
