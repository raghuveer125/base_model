"""UI REST smoke tests — TestClient + fakeredis (no Postgres, no live pub/sub)."""

from __future__ import annotations

import orjson
import pytest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from fastapi.testclient import TestClient

from trading.schemas import IndexTick, now_ms


@pytest.fixture
def client(monkeypatch):
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    from trading import storage as _storage
    monkeypatch.setattr(_storage, "_redis_client", fake, raising=False)
    monkeypatch.setattr(_storage, "get_redis", lambda: fake)

    tick = IndexTick(
        index="NIFTY50", ltp=25_000.5,
        ts_exchange=now_ms(), ts_received=now_ms(),
    )
    fake.set("tpp:tick:idx:NIFTY50", orjson.dumps(tick.model_dump(mode="json")))
    fake.set("tpp:spot:NIFTY50", 25_000.5)
    fake.set("tpp:last_seen:NIFTY50", tick.ts_received)
    fake.set("tpp:atm:NIFTY50", 25_000)

    from trading.ui.app import create_app
    return TestClient(create_app())


def test_healthz_lite_probe(client):
    r = client.get("/api/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_health_full_shape_with_redis_ok(client):
    # fakeredis is healthy; PG is not reachable in tests so it should be reported crit.
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] in {"healthy", "degraded", "down"}
    assert "checks" in body and len(body["checks"]) >= 5
    assert "alerts" in body
    assert "indices" in body and len(body["indices"]) >= 1

    by_name = {c["name"]: c for c in body["checks"]}
    assert by_name["redis"]["status"] == "ok"
    # postgres should fail to connect during tests
    assert by_name["postgres"]["status"] == "crit"
    assert body["status"] in {"down", "degraded"}
    # crit check must appear in alerts
    assert any(a["rule"] == "postgres" for a in body["alerts"])


def test_indices_returns_configured_list(client):
    r = client.get("/api/indices")
    assert r.status_code == 200
    assert "NIFTY50" in r.json()


def test_state_known_index_returns_spot(client):
    r = client.get("/api/state/NIFTY50")
    assert r.status_code == 200
    body = r.json()
    assert body["index"] == "NIFTY50"
    assert body["spot"] == 25_000.5
    assert body["atm"] == 25_000
    assert body["latest_tick"]["index"] == "NIFTY50"


def test_state_unknown_index_404(client):
    r = client.get("/api/state/UNKNOWN")
    assert r.status_code == 404


def test_static_index_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert b"Trading_Plug&amp;Play" in r.content
    assert b"/app.js" in r.content


def test_metrics_reads_redis_hash(client, monkeypatch):
    from trading import storage as _storage
    fake = _storage.get_redis()
    fake.hset("tpp:metrics", mapping={
        "ticks_total": "1234",
        "tick_rate_per_s": "42.5",
        "ingest_latency_p50_ms": "7",
        "gap_count": "2",
        "signals_emitted": "9",
    })
    fake.set("tpp:metrics:ts", 1_729_300_000_000)
    r = client.get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    assert body["ticks_total"] == 1234
    assert body["tick_rate_per_s"] == 42.5
    assert body["ingest_latency_p50_ms"] == 7
    assert body["gap_count"] == 2
    assert body["signals_emitted"] == 9
    assert body["updated_ms"] == 1_729_300_000_000


def test_metrics_empty_when_hash_missing(client):
    r = client.get("/api/metrics")
    assert r.status_code == 200
    body = r.json()
    # fixture seeded a few Redis keys but not tpp:metrics
    assert body.get("updated_ms") is None


def test_replays_list_empty_when_dir_missing(client):
    r = client.get("/api/replays")
    assert r.status_code == 200
    assert r.json() == []


def test_replays_roundtrip(client, tmp_path, monkeypatch):
    # point LOG_DIR at a tmp location and seed a fake run
    import json
    from trading import config as _config
    _config.get_settings.cache_clear()
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
    _config.get_settings.cache_clear()

    run_id = "abc123def4567890"
    run_dir = tmp_path / "replays" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "run_id": run_id, "source": "wal(test)",
        "strategies": ["heartbeat"], "indices": ["NIFTY50"],
        "timeframes": ["1m"], "atm_range": 10,
        "apply_cooldown": True, "apply_risk": True,
        "started_at": "2026-04-19T09:15:00+00:00",
        "finished_at": "2026-04-19T09:16:00+00:00",
    }))
    (run_dir / "summary.json").write_text(json.dumps({
        "run_id": run_id, "records_read": 100, "ticks_index": 50,
        "ticks_option": 50, "candles_synthesized": 3,
        "signals_emitted": 5, "signals_suppressed_cooldown": 1,
        "signals_suppressed_risk": 0,
        "signals_by_strategy": {"heartbeat": 5},
        "signals_by_action": {"BUY": 3, "SELL": 2},
        "candles_closed_by_tf": {"1m": 3},
        "ts_range_ms": [1_729_300_000_000, 1_729_300_600_000],
        "wall_seconds": 1.23,
    }))
    sig_line = '{"strategy":"heartbeat","index":"NIFTY50","action":"BUY",' \
               '"instrument":"NIFTY50","reason":"t","confidence":0.5,' \
               '"metadata":{},"ts":1}\n'
    (run_dir / "signals.jsonl").write_text(sig_line * 3)

    r = client.get("/api/replays")
    assert r.status_code == 200
    lst = r.json()
    assert len(lst) == 1
    assert lst[0]["run_id"] == run_id
    assert lst[0]["headline"]["signals_emitted"] == 5

    r = client.get(f"/api/replays/{run_id}/summary")
    assert r.status_code == 200
    assert r.json()["records_read"] == 100

    r = client.get(f"/api/replays/{run_id}/manifest")
    assert r.status_code == 200
    assert r.json()["strategies"] == ["heartbeat"]

    r = client.get(f"/api/replays/{run_id}/signals?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_replays_rejects_unsafe_run_id(client):
    r = client.get("/api/replays/..%2Fetc/summary")
    # either 400 (caught by guard) or 404 (url decoded to something not found) — both safe
    assert r.status_code in (400, 404)
