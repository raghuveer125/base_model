"""Tests for WAL writer/reader round-trip + Dedup + GapDetector."""

from __future__ import annotations

from trading.ingest import Dedup, GapDetector, Orchestrator
from trading.wal import WALReader, WALWriter


def test_wal_round_trip_single_record(tmp_path):
    w = WALWriter(wal_dir=tmp_path)
    seq = w.append({"symbol": "NSE:NIFTY50-INDEX", "ltp": 25000}, kind="raw_tick")
    w.close()
    assert seq == 1

    r = WALReader(wal_dir=tmp_path)
    records = list(r.iter_records())
    assert len(records) == 1
    assert records[0]["seq"] == 1
    assert records[0]["kind"] == "raw_tick"
    assert records[0]["data"]["symbol"] == "NSE:NIFTY50-INDEX"


def test_wal_seq_persists_across_instances(tmp_path):
    w1 = WALWriter(wal_dir=tmp_path)
    w1.append({"x": 1})
    w1.append({"x": 2})
    w1.close()

    w2 = WALWriter(wal_dir=tmp_path)
    seq = w2.append({"x": 3})
    w2.close()
    assert seq == 3


def test_wal_iter_skips_malformed_lines(tmp_path):
    w = WALWriter(wal_dir=tmp_path)
    w.append({"ok": 1})
    w.close()

    seg = next(tmp_path.glob("*.jsonl"))
    with open(seg, "ab") as f:
        f.write(b"NOT JSON\n")
        f.write(b'{"seq":2,"kind":"raw_tick","ts_ns":1,"data":{"ok":2}}\n')

    recs = list(WALReader(wal_dir=tmp_path).iter_records())
    assert len(recs) == 2


def test_dedup_drops_duplicates():
    d = Dedup(maxsize=10)
    assert d.seen("S", 1) is False
    assert d.seen("S", 1) is True
    assert d.seen("S", 2) is False
    assert d.seen("T", 1) is False


def test_dedup_evicts_oldest_over_capacity():
    d = Dedup(maxsize=2)
    d.seen("a", 1)
    d.seen("b", 1)
    d.seen("c", 1)        # evicts ("a", 1); cache is [("b",1), ("c",1)]
    assert d.seen("b", 1) is True    # still there (most-recently-used now)
    assert d.seen("c", 1) is True    # still there


def test_gap_detector_fires_only_on_threshold():
    fired: list[tuple[str, int]] = []
    g = GapDetector(max_gap_s=1, on_gap=lambda s, ms: fired.append((s, ms)))
    g.observe("S", 1_000)
    g.observe("S", 1_500)
    assert fired == []
    g.observe("S", 3_600)
    assert len(fired) == 1
    assert fired[0][0] == "S"


def test_gap_detector_per_symbol_isolation():
    fired: list[tuple[str, int]] = []
    g = GapDetector(max_gap_s=1, on_gap=lambda s, ms: fired.append((s, ms)))
    g.observe("A", 1_000)
    g.observe("B", 1_000)
    g.observe("A", 3_500)
    g.observe("B", 1_200)
    assert [s for s, _ in fired] == ["A"]


def test_depth_frame_detection():
    """Depth payloads (L5 book) must route away from normalize_option_tick."""
    # L5 book marker
    assert Orchestrator._is_depth_only_frame({"bid1_price": 120, "ask1_price": 121}) is True
    # 'type' marker
    assert Orchestrator._is_depth_only_frame({"type": "dp"}) is True
    # Plain SymbolUpdate must NOT be misclassified as depth.
    symbol_update = {"symbol": "NSE:NIFTY26O0125000CE", "ltp": 120.5,
                     "bid_price": 120.0, "ask_price": 121.0, "type": "sf"}
    assert Orchestrator._is_depth_only_frame(symbol_update) is False
