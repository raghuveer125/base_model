"""Live-data validator daemon for the critical layer.

Runs alongside (or inside) the engine, consumes the `scalp.{INDEX}` and
`critical.regime.{INDEX}` events, and continuously tracks quality
metrics:

  * total entries fired, per index and per side
  * win / loss counts, hit rate, avg PnL per trade
  * average hold time, reason breakdown (target / stop / time / flip)
  * regime breakdown (how many trades per regime)
  * reject reasons (why setups were filtered by risk gate or low
    confidence — pulled from log lines)

Writes a rolling JSON summary to `logs/critical/validation.json` and
prints a one-line status to stdout every `summary_interval_s` seconds
so an operator can `tail -f` progress.

Deleting the `critical` folder removes this daemon entirely — the
report file is gitignored via the `logs/` entry.
"""

from __future__ import annotations

import json
import threading
import time
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from trading.events import EventBus
from trading.logging_setup import get_logger
from trading.schemas import now_ms

log = get_logger(__name__)


@dataclass
class IndexStats:
    entries: int = 0
    exits: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    total_hold_ms: int = 0
    by_reason: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    by_side: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    last_50_pnls: deque = field(default_factory=lambda: deque(maxlen=50))

    @property
    def hit_rate(self) -> float:
        n = self.wins + self.losses
        return (self.wins / n) if n else 0.0

    @property
    def avg_pnl(self) -> float:
        n = self.wins + self.losses
        return (self.total_pnl / n) if n else 0.0

    @property
    def avg_hold_s(self) -> float:
        n = self.wins + self.losses
        return (self.total_hold_ms / 1000 / n) if n else 0.0

    def snapshot(self) -> dict:
        return {
            "entries": self.entries,
            "exits": self.exits,
            "wins": self.wins,
            "losses": self.losses,
            "hit_rate": round(self.hit_rate, 3),
            "avg_pnl": round(self.avg_pnl, 2),
            "total_pnl": round(self.total_pnl, 2),
            "avg_hold_s": round(self.avg_hold_s, 1),
            "by_reason": dict(self.by_reason),
            "by_side": dict(self.by_side),
            "recent_pnls": list(self.last_50_pnls),
        }


@dataclass
class RegimeStats:
    trending: int = 0
    ranging: int = 0
    volatile: int = 0
    source_llm: int = 0
    source_cache: int = 0
    source_fallback: int = 0

    def snapshot(self) -> dict:
        return asdict(self)


class Validator:
    def __init__(
        self,
        indices: list[str],
        *,
        out_path: Path | None = None,
        summary_interval_s: float = 60.0,
        bus: EventBus | None = None,
    ) -> None:
        self.indices = list(indices)
        self.out_path = out_path or (
            Path("logs/critical/validation.json").resolve()
        )
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        self.summary_interval_s = summary_interval_s
        self._bus = bus or EventBus()
        self._stop = threading.Event()
        self._stats: dict[str, IndexStats] = {i: IndexStats() for i in indices}
        self._regime: dict[str, RegimeStats] = {i: RegimeStats() for i in indices}
        self._started_ms = now_ms()

    # ---- lifecycle ----

    def start(self) -> None:
        threading.Thread(
            target=self._subscribe_loop, name="critical-validator-sub",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._summary_loop, name="critical-validator-sum",
            daemon=True,
        ).start()
        log.info("validator_started", out=str(self.out_path))

    def stop(self) -> None:
        self._stop.set()
        self._write_summary()

    # ---- subscribers ----

    def _subscribe_loop(self) -> None:
        patterns = [f"scalp.{i}" for i in self.indices] + \
                   [f"critical.regime.{i}" for i in self.indices]
        while not self._stop.is_set():
            try:
                self._bus.subscribe(tuple(patterns), self._on_event)
            except Exception as e:   # noqa: BLE001
                log.warning("validator_subscribe_error", error=str(e))
                if self._stop.wait(1.0):
                    return

    def _on_event(self, channel: str, data: dict) -> None:
        if channel.startswith("scalp."):
            index = channel.split(".", 1)[1]
            kind = data.get("kind")
            s = self._stats.setdefault(index, IndexStats())
            if kind == "entry":
                s.entries += 1
                side = str(data.get("side") or "")
                s.by_side[side] += 1
            elif kind == "exit":
                s.exits += 1
                pnl = float(data.get("pnl") or 0.0)
                hold = int(data.get("held_ms") or 0)
                reason = str(data.get("reason") or "")
                s.total_pnl += pnl
                s.total_hold_ms += max(hold, 0)
                s.by_reason[reason.split(":", 1)[0]] += 1
                s.last_50_pnls.append(round(pnl, 2))
                if pnl >= 0:
                    s.wins += 1
                else:
                    s.losses += 1
        elif channel.startswith("critical.regime."):
            index = channel.split(".", 2)[2]
            r = self._regime.setdefault(index, RegimeStats())
            regime = str(data.get("regime") or "")
            if regime == "trending": r.trending += 1
            elif regime == "ranging": r.ranging += 1
            elif regime == "volatile": r.volatile += 1
            src = str(data.get("source") or "")
            if src == "llm": r.source_llm += 1
            elif src == "cache": r.source_cache += 1
            elif src == "fallback": r.source_fallback += 1

    # ---- summarizer ----

    def _summary_loop(self) -> None:
        while not self._stop.wait(self.summary_interval_s):
            self._write_summary()

    def _write_summary(self) -> None:
        payload = self._snapshot()
        try:
            tmp = self.out_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp.replace(self.out_path)
        except Exception as e:   # noqa: BLE001
            log.warning("validator_write_failed", error=str(e))
            return
        overall = payload["overall"]
        log.info(
            "validator_summary",
            entries=overall["entries"],
            hit_rate=overall["hit_rate"],
            total_pnl=overall["total_pnl"],
            uptime_s=payload["uptime_s"],
        )

    def _snapshot(self) -> dict:
        per_idx = {i: s.snapshot() for i, s in self._stats.items()}
        per_regime = {i: r.snapshot() for i, r in self._regime.items()}
        total_entries = sum(s.entries for s in self._stats.values())
        total_wins    = sum(s.wins for s in self._stats.values())
        total_losses  = sum(s.losses for s in self._stats.values())
        total_pnl     = sum(s.total_pnl for s in self._stats.values())
        n_closed = total_wins + total_losses
        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "uptime_s": (now_ms() - self._started_ms) // 1000,
            "overall": {
                "entries": total_entries,
                "closed":  n_closed,
                "wins":    total_wins,
                "losses":  total_losses,
                "hit_rate": round(total_wins / n_closed, 3) if n_closed else 0.0,
                "total_pnl": round(total_pnl, 2),
            },
            "per_index":  per_idx,
            "per_regime": per_regime,
        }


def main() -> None:
    """Stand-alone validator entry: `python -m trading.critical.validator`."""
    from trading.config import get_settings
    from trading.logging_setup import configure_logging

    configure_logging()
    indices = get_settings().index_list
    val = Validator(indices)
    val.start()
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        val.stop()


if __name__ == "__main__":
    main()
