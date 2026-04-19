"""AlertNotifier tests — dispatch, dedup, resolution, severity filter."""

from __future__ import annotations

import pytest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from trading.ui.notifier import AlertNotifier


class StubSink:
    def __init__(self, name: str = "stub", ok: bool = True) -> None:
        self.name = name
        self.ok = ok
        self.sent: list[tuple[str, dict]] = []

    def send(self, subject: str, body: dict) -> bool:
        self.sent.append((subject, dict(body)))
        return self.ok


@pytest.fixture
def redis_stub(monkeypatch):
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    from trading import storage as _storage
    monkeypatch.setattr(_storage, "_redis_client", fake, raising=False)
    monkeypatch.setattr(_storage, "get_redis", lambda: fake)
    from trading.ui import notifier as _notifier
    monkeypatch.setattr(_notifier, "get_redis", lambda: fake)
    return fake


def _evaluator_with(*alerts: dict):
    def _ev() -> dict:
        return {
            "status": "degraded" if alerts else "healthy",
            "alerts": list(alerts),
        }
    return _ev


def _notify_settings(monkeypatch, **overrides):
    from trading import config as _config
    env = {
        "NOTIFY_ENABLED": "true",
        "NOTIFY_MIN_SEVERITY": "crit",
        "NOTIFY_DEDUP_SECONDS": "600",
        "NOTIFY_POLL_SECONDS": "30",
    }
    env.update({k: str(v) for k, v in overrides.items()})
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _config.get_settings.cache_clear()


def test_tick_returns_no_sinks_when_none_configured(monkeypatch, redis_stub):
    _notify_settings(monkeypatch)
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "x", "severity": "crit", "detail": "d"}),
        sinks_builder=lambda: [],
    )
    out = notifier.tick()
    assert out["no_sinks"] is True
    assert out["dispatched"] == 0


def test_first_crit_alert_dispatches_to_sink(monkeypatch, redis_stub):
    _notify_settings(monkeypatch)
    sink = StubSink()
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "postgres", "severity": "crit", "detail": "down"}),
        sinks_builder=lambda: [sink],
        clock=lambda: 1_000.0,
    )
    out = notifier.tick()
    assert out["dispatched"] == 1
    assert out["skipped"] == 0
    assert len(sink.sent) == 1
    subject, body = sink.sent[0]
    assert "CRIT" in subject and "postgres" in subject
    assert body["rule"] == "postgres"
    assert body["severity"] == "crit"


def test_warn_alerts_ignored_when_min_severity_crit(monkeypatch, redis_stub):
    _notify_settings(monkeypatch, NOTIFY_MIN_SEVERITY="crit")
    sink = StubSink()
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "latency_p95", "severity": "warn", "detail": "slow"}),
        sinks_builder=lambda: [sink],
        clock=lambda: 1_000.0,
    )
    out = notifier.tick()
    assert out["active"] == 0
    assert out["dispatched"] == 0
    assert sink.sent == []


def test_warn_alerts_fire_when_min_severity_warn(monkeypatch, redis_stub):
    _notify_settings(monkeypatch, NOTIFY_MIN_SEVERITY="warn")
    sink = StubSink()
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "latency_p95", "severity": "warn", "detail": "slow"}),
        sinks_builder=lambda: [sink],
        clock=lambda: 1_000.0,
    )
    out = notifier.tick()
    assert out["dispatched"] == 1


def test_second_tick_within_cooldown_is_deduped(monkeypatch, redis_stub):
    _notify_settings(monkeypatch, NOTIFY_DEDUP_SECONDS="600")
    sink = StubSink()
    clock_val = {"t": 1_000.0}
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "redis", "severity": "crit", "detail": "down"}),
        sinks_builder=lambda: [sink],
        clock=lambda: clock_val["t"],
    )
    out1 = notifier.tick()
    assert out1["dispatched"] == 1

    clock_val["t"] = 1_060.0   # 60s later
    out2 = notifier.tick()
    assert out2["dispatched"] == 0
    assert out2["skipped"] == 1
    assert len(sink.sent) == 1


def test_re_fires_after_cooldown(monkeypatch, redis_stub):
    _notify_settings(monkeypatch, NOTIFY_DEDUP_SECONDS="600")
    sink = StubSink()
    clock_val = {"t": 1_000.0}
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "redis", "severity": "crit", "detail": "down"}),
        sinks_builder=lambda: [sink],
        clock=lambda: clock_val["t"],
    )
    notifier.tick()
    clock_val["t"] = 1_000.0 + 601.0
    out = notifier.tick()
    assert out["dispatched"] == 1
    assert len(sink.sent) == 2


def test_resolution_notification_when_alert_clears(monkeypatch, redis_stub):
    _notify_settings(monkeypatch)
    sink = StubSink()
    clock_val = {"t": 1_000.0}
    alerts_var = {"a": [{"rule": "redis", "severity": "crit", "detail": "down"}]}

    def _ev():
        return {"status": "degraded" if alerts_var["a"] else "healthy",
                "alerts": list(alerts_var["a"])}

    notifier = AlertNotifier(
        evaluator=_ev,
        sinks_builder=lambda: [sink],
        clock=lambda: clock_val["t"],
    )
    out1 = notifier.tick()
    assert out1["dispatched"] == 1

    # Alert clears
    alerts_var["a"] = []
    clock_val["t"] = 1_100.0
    out2 = notifier.tick()
    assert out2["resolved"] == 1
    assert len(sink.sent) == 2
    assert sink.sent[-1][0].startswith("[RESOLVED]")
    assert sink.sent[-1][1]["rule"] == "redis"

    # Another clean tick — resolution already acknowledged, no re-notify
    out3 = notifier.tick()
    assert out3["resolved"] == 0
    assert len(sink.sent) == 2


def test_failed_sink_keeps_rule_unfired(monkeypatch, redis_stub):
    _notify_settings(monkeypatch)
    sink = StubSink(ok=False)
    notifier = AlertNotifier(
        evaluator=_evaluator_with({"rule": "redis", "severity": "crit", "detail": "down"}),
        sinks_builder=lambda: [sink],
        clock=lambda: 1_000.0,
    )
    out = notifier.tick()
    assert out["dispatched"] == 0
    assert out["skipped"] == 1
    out2 = notifier.tick()
    assert out2["skipped"] == 1


def test_tick_test_sends_to_all_sinks(monkeypatch, redis_stub):
    _notify_settings(monkeypatch)
    s1, s2 = StubSink("a"), StubSink("b")
    notifier = AlertNotifier(
        evaluator=_evaluator_with(),
        sinks_builder=lambda: [s1, s2],
    )
    out = notifier.tick_test()
    assert out["attempted"] == 2
    assert out["sent"] == 2
    assert s1.sent and s2.sent
    assert out["sinks"] == ["a", "b"]
