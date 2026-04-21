"""Vigilante daemon — the background monitor loop.

Toggle on/off by starting/stopping the process. The engine has zero
awareness of the vigilante — this is a strict sidecar.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from trading.config import get_settings
from trading.logging_setup import get_logger
from trading.storage import LiveStore
from trading.critical.vigilante.checks import (
    MANAGED_MODULES, ProcInfo, detect_collisions,
    evaluate_heartbeat, heartbeat_threshold_s, verify_prompt_shape,
)

log = get_logger(__name__)

_IST = ZoneInfo("Asia/Kolkata")


@dataclass
class VigilanteState:
    """Rolling state per index — fed back into the next check cycle."""
    streaks: dict[str, int] = field(default_factory=dict)   # index → consecutive stale samples
    alerts: dict[str, int] = field(default_factory=dict)    # key → last alert ms
    cycles: int = 0


class Vigilante:
    """Loop owner. Run `.start()` for a background thread, or `.tick()`
    once per cycle from an external scheduler / test harness.
    """

    def __init__(
        self,
        *,
        indices: list[str] | None = None,
        store: LiveStore | None = None,
        tick_interval_s: float = 2.0,
        alert_cooldown_s: float = 30.0,
    ) -> None:
        self.indices = indices or get_settings().index_list
        self.store = store or LiveStore()
        self.tick_interval_s = tick_interval_s
        self.alert_cooldown_s = alert_cooldown_s
        self.state = VigilanteState()
        self._stop = threading.Event()

    # ---- lifecycle ----

    def start(self) -> threading.Thread:
        t = threading.Thread(
            target=self._loop, name="vigilante-loop", daemon=True,
        )
        t.start()
        return t

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("vigilante_start", indices=self.indices,
                 tick_interval_s=self.tick_interval_s)
        while not self._stop.wait(self.tick_interval_s):
            try:
                self.tick()
            except Exception as e:   # noqa: BLE001 — sidecar must never crash main
                log.warning("vigilante_tick_failed", error=str(e))
        log.info("vigilante_stop")

    # ---- one cycle of checks ----

    def tick(self) -> dict[str, Any]:
        """Run all checks once. Returns a summary dict (useful for tests
        and the `--once` CLI mode)."""
        self.state.cycles += 1
        summary: dict[str, Any] = {"cycle": self.state.cycles}

        # 1) PID-collision detection
        try:
            procs = _snapshot_processes()
            collisions = detect_collisions(procs)
            summary["collisions"] = [
                {"module": c.module, "pids": list(c.launcher_pids),
                 "collided": c.collided, "detail": c.detail}
                for c in collisions
            ]
            for c in collisions:
                if c.collided and self._should_alert(f"collision:{c.module}"):
                    log.error("vigilante_pid_collision",
                              module=c.module, pids=c.launcher_pids,
                              detail=c.detail)
                    self._set_alert_flag(
                        f"pid_collision:{c.module}",
                        detail=c.detail, pids=list(c.launcher_pids),
                    )
        except Exception as e:   # noqa: BLE001
            summary["collisions_error"] = str(e)

        # 2) Heartbeat monitor — per-index, time-of-day damped
        ist_now = datetime.now(_IST)
        threshold = heartbeat_threshold_s(ist_now)
        now_ms = int(time.time() * 1000)
        hb_out: list[dict] = []
        for idx in self.indices:
            last_ms = self.store.last_seen_ms(idx)
            prior = self.state.streaks.get(idx, 0)
            report = evaluate_heartbeat(
                idx, last_ms, now_ms, prior,
                threshold_s=threshold,
            )
            self.state.streaks[idx] = report.streak
            hb_out.append({
                "index": idx, "age_s": report.age_s,
                "verdict": report.verdict, "streak": report.streak,
                "threshold_s": report.threshold_s,
            })
            if report.verdict == "stale_sustained":
                if self._should_alert(f"heartbeat:{idx}"):
                    log.warning("vigilante_feed_stale",
                                index=idx, age_s=report.age_s,
                                streak=report.streak,
                                threshold_s=report.threshold_s)
                self._set_reconnect_flag(idx, age_s=report.age_s,
                                          streak=report.streak)
        summary["heartbeats"] = hb_out

        # 3) Payload verification — render what the regime call would
        #    send right now and assert per-index isolation + units.
        summary["payloads"] = self._check_payloads()
        return summary

    # ---- payload verification ----

    def _check_payloads(self) -> list[dict]:
        """Render the user prompt for each index using current state and
        assert the shape. Silent on transient warm-up (chain empty etc.)."""
        out: list[dict] = []
        try:
            # Lazy import so the daemon doesn't need the whole critical
            # layer graph at import time.
            from trading.critical.engine import CriticalEngine
            from trading.critical.regime.prompt import build_user_prompt
            from trading.expiry import get_expiries
        except ImportError as e:
            out.append({"index": "*", "ok": False,
                         "violations": [f"imports failed: {e}"]})
            return out
        try:
            eng = CriticalEngine(indices=self.indices)
            eng._expiries = get_expiries(self.indices)
        except Exception as e:   # noqa: BLE001
            out.append({"index": "*", "ok": False,
                         "violations": [f"engine build failed: {e}"]})
            return out
        for idx in self.indices:
            try:
                snap = eng._build_regime_input(idx)
            except Exception as e:   # noqa: BLE001
                out.append({"index": idx, "ok": False,
                             "violations": [f"snapshot failed: {e}"]})
                continue
            if snap is None:
                out.append({"index": idx, "ok": True,
                             "violations": (), "skipped": "warming_up"})
                continue
            rendered = build_user_prompt(snap)
            report = verify_prompt_shape(idx, rendered)
            if not report.ok and self._should_alert(f"payload:{idx}"):
                log.warning("vigilante_payload_violation",
                            index=idx, violations=report.violations)
            out.append({"index": idx, "ok": report.ok,
                         "violations": list(report.violations)})
        return out

    # ---- redis flags + throttled alerts ----

    def _should_alert(self, key: str) -> bool:
        """Suppress repeated alerts on the same key within cooldown."""
        now_ms = int(time.time() * 1000)
        last = self.state.alerts.get(key, 0)
        if now_ms - last < self.alert_cooldown_s * 1000:
            return False
        self.state.alerts[key] = now_ms
        return True

    def _set_alert_flag(self, key: str, **extras: Any) -> None:
        import orjson
        payload = {"ts_ms": int(time.time() * 1000), **extras}
        try:
            self.store.r.setex(
                f"tpp:vigilante:alert:{key}", 300,
                orjson.dumps(payload),
            )
        except Exception as e:   # noqa: BLE001
            log.warning("vigilante_flag_failed", key=key, error=str(e))

    def _set_reconnect_flag(
        self, index: str, *, age_s: float | None, streak: int,
    ) -> None:
        import orjson
        payload = {
            "ts_ms": int(time.time() * 1000),
            "age_s": age_s, "streak": streak,
        }
        try:
            self.store.r.setex(
                f"tpp:vigilante:reconnect:{index}", 60,
                orjson.dumps(payload),
            )
        except Exception as e:   # noqa: BLE001
            log.warning("vigilante_flag_failed", key=f"reconnect:{index}",
                         error=str(e))


# ────────────────────────────────────────────────────────────────────────
# helpers
# ────────────────────────────────────────────────────────────────────────


def _snapshot_processes() -> list[ProcInfo]:
    """Enumerate python processes running our managed modules.

    `is_launcher` is True when the process's parent is NOT itself a
    python process running one of our modules — i.e. this is the
    top-level launcher stub (venv python) rather than the re-execed
    interpreter child.
    """
    # Lazy import so `scan` / test paths don't need psutil installed.
    import psutil
    procs: list[ProcInfo] = []
    # Match longest module strings first so `trading.critical.vigilante`
    # wins over `trading.critical` (which is a substring of it). Without
    # this, every vigilante process gets mis-tagged as a `trading.critical`
    # collision — which is exactly what happened on 2026-04-21.
    modules_by_specificity = sorted(
        MANAGED_MODULES, key=len, reverse=True,
    )
    # First pass: collect all python procs for parent-lookup.
    matching: dict[int, tuple[str, str, float]] = {}
    for p in psutil.process_iter(["pid", "name", "cmdline", "create_time"]):
        try:
            info = p.info
            name = (info.get("name") or "").lower()
            if "python" not in name:
                continue
            cmd = " ".join(info.get("cmdline") or [])
            # Require the module to appear as a full `-m <module>` token,
            # not a bare substring, so similar-named modules don't collide.
            matched = next(
                (m for m in modules_by_specificity
                 if f"-m {m} " in cmd + " " or cmd.endswith(f"-m {m}")),
                None,
            )
            if not matched:
                continue
            exe = info.get("cmdline", [""])[0] if info.get("cmdline") else ""
            matching[info["pid"]] = (matched, exe, info["create_time"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    # Second pass: identify parents.
    for pid, (module, _exe, ct) in matching.items():
        try:
            parent = psutil.Process(pid).parent()
            parent_pid = parent.pid if parent else -1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            parent_pid = -1
        is_launcher = parent_pid not in matching
        procs.append(ProcInfo(
            pid=pid, module=module, create_time=ct,
            is_launcher=is_launcher,
        ))
    return procs
