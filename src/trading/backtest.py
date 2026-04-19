"""Legacy backtest shim — delegates to the generalized ReplayEngine.

`BacktestRunner` wraps a `ReplayEngine(source=WALEventSource(...))`, preserving
the original signature and summary field names so existing callers and tests
continue to work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from trading.config import get_settings
from trading.replay.engine import ReplayEngine
from trading.replay.sources import WALEventSource
from trading.schemas import Timeframe


class BacktestRunner:
    def __init__(
        self,
        *,
        wal_dir: Path | None = None,
        date: str | None = None,
        indices: Iterable[str] | None = None,
        strategy_names: Iterable[str] = (),
        output_path: Path | None = None,
        timeframes: Iterable[Timeframe] = ("1m", "5m", "15m"),
        atm_range: int | None = None,
        apply_cooldown: bool = True,
        apply_risk: bool = True,
    ) -> None:
        s = get_settings()
        source = WALEventSource(wal_dir=wal_dir, date=date)
        idx_list = list(indices) if indices is not None else s.index_list

        # BacktestRunner's historical contract: output is ONE jsonl file at `output_path`,
        # not a directory. We give ReplayEngine a dedicated output dir and then
        # symlink / re-point its signals.jsonl to the operator's requested path.
        # Simpler: pass a pre-made output_dir containing output_path's parent, then
        # the signals.jsonl sits alongside summary.json in that dir. If the caller
        # passed an explicit output_path, honor it by using its parent as the dir
        # AND renaming the file after the fact via a small shim.
        target_path = (
            Path(output_path)
            if output_path is not None
            else (s.log_dir / f"backtest_signals_{(date or 'all')}.jsonl")
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        output_dir = target_path.parent / f".backtest_{target_path.stem}"
        output_dir.mkdir(parents=True, exist_ok=True)

        self._target_path = target_path
        self._engine = ReplayEngine(
            source=source,
            strategy_names=list(strategy_names),
            indices=idx_list,
            output_dir=output_dir,
            timeframes=list(timeframes),
            atm_range=atm_range,
            apply_cooldown=apply_cooldown,
            apply_risk=apply_risk,
        )
        # Expose for tests
        self.output_path = target_path
        self.run_id = self._engine.run_id

    def run(self) -> dict:
        summary = self._engine.run()

        # Move replay engine's signals.jsonl to the path the caller asked for
        signals_src = self._engine.output_dir / "signals.jsonl"
        try:
            if signals_src.exists():
                data = signals_src.read_bytes()
                self._target_path.write_bytes(data)
        except OSError:
            pass

        # Map generalized summary → legacy field names used by existing tests
        candles_closed = {}
        for tf, n in summary.get("candles_closed_by_tf", {}).items():
            candles_closed[tf] = n
        return {
            "records_read": summary["records_read"],
            "ticks_index": summary["ticks_index"],
            "ticks_option": summary["ticks_option"],
            "candles_closed": candles_closed,
            "greeks_computed": summary["greeks_computed"],
            "signals_emitted": summary["signals_emitted"],
            "signals_suppressed_cooldown": summary["signals_suppressed_cooldown"],
            "signals_suppressed_risk": summary["signals_suppressed_risk"],
            "signals_by_strategy": summary["signals_by_strategy"],
            "signals_by_action": summary["signals_by_action"],
            "ts_range_ms": summary["ts_range_ms"],
            "run_id": summary["run_id"],
        }
