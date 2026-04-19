"""Strategy framework tests — cooldown, risk, context.emit, registry."""

from __future__ import annotations

from trading.schemas import Signal, now_ms
from trading.strategies import STRATEGY_REGISTRY
from trading.strategies.base import StrategyContext
from trading.strategies.risk import CooldownManager, RiskEngine


def _sig(
    *,
    strategy: str = "test",
    index: str = "NIFTY50",
    action: str = "BUY",
    instrument: str = "NSE:NIFTY50-INDEX",
    ts: int | None = None,
) -> Signal:
    return Signal(
        strategy=strategy, index=index,
        action=action,  # type: ignore[arg-type]
        instrument=instrument, reason="unit",
        confidence=0.5, metadata={}, ts=ts or now_ms(),
    )


def test_cooldown_first_signal_passes():
    c = CooldownManager(cooldown_seconds=5)
    assert c.should_suppress(_sig(ts=1_000_000)) is False


def test_cooldown_suppresses_within_window():
    c = CooldownManager(cooldown_seconds=5)
    c.should_suppress(_sig(ts=1_000_000))
    assert c.should_suppress(_sig(ts=1_002_000)) is True


def test_cooldown_lets_through_after_window():
    c = CooldownManager(cooldown_seconds=5)
    c.should_suppress(_sig(ts=1_000_000))
    assert c.should_suppress(_sig(ts=1_006_000)) is False


def test_cooldown_is_per_instrument():
    c = CooldownManager(cooldown_seconds=60)
    assert c.should_suppress(_sig(instrument="A", ts=0)) is False
    assert c.should_suppress(_sig(instrument="B", ts=0)) is False
    assert c.should_suppress(_sig(instrument="A", ts=1)) is True
    assert c.should_suppress(_sig(instrument="B", ts=1)) is True


def test_cooldown_reset_clears_state():
    c = CooldownManager(cooldown_seconds=60)
    c.should_suppress(_sig(ts=0))
    c.reset()
    assert c.should_suppress(_sig(ts=1)) is False


def test_risk_denies_unknown_index_when_allowlist_set():
    r = RiskEngine(max_per_hour=10, max_per_day=100, allowed_indices={"NIFTY50"})
    ok, reason = r.allows(_sig(index="BANKNIFTY"))
    assert ok is False and "not allowed" in reason


def test_risk_denies_bad_action():
    r = RiskEngine(max_per_hour=10, max_per_day=100, allowed_actions={"BUY", "SELL"})
    ok, reason = r.allows(_sig(action="HOLD"))
    assert ok is False and "action" in reason


def test_risk_hour_cap_blocks_after_n():
    r = RiskEngine(max_per_hour=3, max_per_day=100)
    base = now_ms()
    for i in range(3):
        ok, _ = r.allows(_sig(ts=base + i))
        assert ok is True
    ok, reason = r.allows(_sig(ts=base + 4))
    assert ok is False and "hour cap" in reason


def test_risk_day_cap_blocks_after_n():
    r = RiskEngine(max_per_hour=100, max_per_day=2)
    base = now_ms()
    assert r.allows(_sig(ts=base))[0]
    assert r.allows(_sig(ts=base + 1))[0]
    ok, reason = r.allows(_sig(ts=base + 2))
    assert ok is False and "day cap" in reason


def test_risk_snapshot_returns_counts():
    r = RiskEngine(max_per_hour=100, max_per_day=1000)
    for _ in range(5):
        r.allows(_sig())
    snap = r.snapshot()
    assert snap["test"]["in_hour"] == 5
    assert snap["test"]["in_day"] == 5


def test_context_emit_builds_valid_signal_and_calls_callback():
    captured: list[Signal] = []
    ctx = StrategyContext(on_signal=captured.append, state={})
    sig = ctx.emit(
        strategy="mystrat", index="NIFTY50", action="BUY",
        instrument="NSE:NIFTY50-INDEX", reason="test", confidence=0.8,
    )
    assert len(captured) == 1
    assert captured[0] is sig
    assert sig.confidence == 0.8


def test_context_emit_defaults_ts_to_now():
    captured: list[Signal] = []
    ctx = StrategyContext(on_signal=captured.append, state={})
    before = now_ms()
    ctx.emit(strategy="s", index="NIFTY50", action="HOLD",
             instrument="X", confidence=0.1)
    after = now_ms()
    assert before <= captured[0].ts <= after


def test_heartbeat_strategy_registered_on_import():
    assert "heartbeat" in STRATEGY_REGISTRY
    cls = STRATEGY_REGISTRY["heartbeat"]
    instance = cls(indices=["NIFTY50"])
    assert instance.name == "heartbeat"
    assert instance.indices == ["NIFTY50"]
