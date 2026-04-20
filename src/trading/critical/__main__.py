"""`python -m trading.critical` — start the critical decision layer.

Expects the base pipeline (ingest + greeks + candles) to be running so
Redis has live chain data. Runs until SIGINT / SIGTERM / Ctrl+C.
"""

from __future__ import annotations

import sys
import time

from trading.config import get_settings
from trading.critical.engine import CriticalEngine, install_signal_handlers
from trading.critical.validator import Validator
from trading.logging_setup import configure_logging, get_logger
from trading.storage import get_redis


def main() -> None:
    configure_logging()
    log = get_logger("tpp-critical")
    try:
        get_redis().ping()
        indices = get_settings().index_list
        eng = CriticalEngine(indices=indices)
        validator = Validator(indices)
        install_signal_handlers(eng)
        eng.start()
        validator.start()
        # Park the main thread — daemon threads do the work.
        while not eng._stop.is_set():
            time.sleep(1.0)
        validator.stop()
    except KeyboardInterrupt:
        log.info("critical_keyboard_interrupt")
    except Exception as e:   # noqa: BLE001
        log.error("critical_fatal", error=str(e))
        sys.exit(1)


if __name__ == "__main__":
    main()
