"""Order-engine tests — PaperExecutor slippage/fees + PositionBook accounting."""

from __future__ import annotations

import pytest

try:
    import fakeredis
except ImportError:  # pragma: no cover
    fakeredis = None

from trading.orders.base import Fill, Order
from trading.orders.book import PositionBook
from trading.orders.paper import PaperExecutor


@pytest.fixture
def redis_stub(monkeypatch):
    if fakeredis is None:
        pytest.skip("fakeredis not installed")
    fake = fakeredis.FakeStrictRedis(decode_responses=False)
    from trading import storage as _storage
    monkeypatch.setattr(_storage, "_redis_client", fake, raising=False)
    monkeypatch.setattr(_storage, "get_redis", lambda: fake)
    from trading.orders import book as _book
    monkeypatch.setattr(_book, "get_redis", lambda: fake)
    return fake


def _order(action="BUY", qty=10, ref=100.0, instrument="NIFTY50") -> Order:
    return Order(
        strategy="t", index="NIFTY50", instrument=instrument,
        action=action,  # type: ignore[arg-type]
        qty=qty, ref_price=ref, signal_ts=1, ordered_ts=2,
    )


def _fill(side="B", qty=10, price=100.0, fees=0.0, instrument="NIFTY50") -> Fill:
    return Fill(
        order_id=None, strategy="t", index="NIFTY50", instrument=instrument,
        side=side,  # type: ignore[arg-type]
        qty=qty, fill_price=price, fees=fees, slippage_bps=0.0, ts_ms=1,
    )


def test_paper_buy_adds_slippage_and_fees():
    ex = PaperExecutor(slippage_bps=10.0, fee_bps=3.0, flat_fee=20.0)
    r = ex.execute(_order("BUY", qty=100, ref=100.0))
    assert r.ok and r.fill is not None
    assert abs(r.fill.fill_price - 100.10) < 1e-6
    assert r.fill.side == "B"
    gross = 100.10 * 100
    expected_fee = 20.0 + gross * 3.0 / 10_000.0
    assert abs(r.fill.fees - round(expected_fee, 4)) < 1e-3


def test_paper_sell_subtracts_slippage():
    ex = PaperExecutor(slippage_bps=10.0, fee_bps=0.0, flat_fee=0.0)
    r = ex.execute(_order("SELL", qty=50, ref=200.0))
    assert r.ok and r.fill is not None
    assert abs(r.fill.fill_price - 199.80) < 1e-6
    assert r.fill.side == "S"


def test_paper_rejects_bad_ref_price():
    ex = PaperExecutor()
    r = ex.execute(_order("BUY", ref=0.0))
    assert r.ok is False
    assert "ref_price" in r.reason


def test_paper_rejects_hold_action():
    ex = PaperExecutor()
    r = ex.execute(_order("HOLD"))
    assert r.ok is False and "HOLD" in r.reason


def test_book_open_long_sets_qty_and_avg_price(redis_stub):
    book = PositionBook()
    result = book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=5.0))
    pos = result["position"]
    assert pos.qty == 10
    assert pos.avg_price == 100.0
    assert result["delta_realized"] == -5.0
    assert book.realized_pnl() == -5.0


def test_book_add_to_long_blends_avg_price(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    result = book.apply_fill(_fill(side="B", qty=10, price=110.0, fees=0.0))
    pos = result["position"]
    assert pos.qty == 20
    assert pos.avg_price == 105.0


def test_book_close_long_realizes_pnl(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    result = book.apply_fill(_fill(side="S", qty=10, price=110.0, fees=0.0))
    pos = result["position"]
    assert pos.qty == 0
    assert result["delta_realized"] == 100.0
    assert book.realized_pnl() == 100.0


def test_book_partial_close_keeps_remaining_and_realizes(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    result = book.apply_fill(_fill(side="S", qty=4, price=120.0, fees=0.0))
    pos = result["position"]
    assert pos.qty == 6
    assert pos.avg_price == 100.0
    assert result["delta_realized"] == 80.0


def test_book_flip_long_to_short(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    result = book.apply_fill(_fill(side="S", qty=15, price=110.0, fees=0.0))
    pos = result["position"]
    assert pos.qty == -5
    assert pos.avg_price == 110.0
    assert result["delta_realized"] == 100.0


def test_book_open_short_and_cover(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="S", qty=10, price=200.0, fees=0.0))
    result = book.apply_fill(_fill(side="B", qty=10, price=180.0, fees=0.0))
    pos = result["position"]
    assert pos.qty == 0
    assert result["delta_realized"] == 200.0


def test_book_survives_restart_via_redis(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    book2 = PositionBook()
    pos = book2.get("NIFTY50")
    assert pos is not None
    assert pos.qty == 10
    assert pos.avg_price == 100.0


def test_book_realized_pnl_accumulates_across_trades(redis_stub):
    book = PositionBook()
    book.apply_fill(_fill(side="B", qty=10, price=100.0, fees=0.0))
    book.apply_fill(_fill(side="S", qty=10, price=110.0, fees=2.0))
    book.apply_fill(_fill(side="S", qty=5, price=50.0, fees=1.0,
                          instrument="BANKNIFTY"))
    assert book.realized_pnl() == 97.0
