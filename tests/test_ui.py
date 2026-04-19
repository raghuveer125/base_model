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


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


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
