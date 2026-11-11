"""partial fills + bracket coverage, insufficient collateral, cash/share double use."""
import json
from conftest import ReaderView
from sakibdesk.orders import OrderManager
from sakibdesk.ledger import Ledger
from sakibdesk import stock as S, cycle as C


def test_partial_fill_books_delta_once_and_reduces_reservation(cfg, journal, broker):
    led = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=broker, ledger=led)
    r = om.place(strategy="STOCK", symbol="MU", purpose="ENTRY", signal_key="s",
                 body={"symbol": "MU", "qty": "10", "side": "buy", "type": "limit", "limit_price": "100"},
                 reservations=[("STOCK", "ORDER_NOTIONAL", 1000.0)])
    broker.fill(r["client_order_id"], 4, 100)
    om.reconcile(ReaderView(broker)); om.reconcile(ReaderView(broker))   # duplicate reconcile
    assert led.cash("STOCK") == 20000 - 400
    assert om.reserved("STOCK") == 600
    broker.fill(r["client_order_id"], 10, 100)
    om.reconcile(ReaderView(broker))
    assert led.cash("STOCK") == 19000 and om.reserved("STOCK") == 0


def test_protection_gap_detects_uncovered_partial():
    legs = [{"type": "limit", "qty": "10", "status": "held"}, {"type": "stop", "qty": "10", "status": "held"}]
    g = S.protection_gap(4.0, legs)
    assert not g["ok"] and g["stop_cover"] == 10
    legs = [{"type": "limit", "qty": "4", "status": "new"}, {"type": "stop", "qty": "4", "status": "new"}]
    assert S.protection_gap(4.0, legs)["ok"]


def test_partial_bracket_gets_oco_for_exact_filled_qty(cfg, journal, broker):
    om = OrderManager(journal, cfg, writer=broker, ledger=Ledger(journal, cfg.policy))
    body, stop, tgt = S.bracket_body("MU", 10, 100.0, 4.0, cfg.policy["stock"])
    r = om.place(strategy="STOCK", symbol="MU", purpose="ENTRY", signal_key="s", body=body,
                 reservations=[("STOCK", "ORDER_NOTIONAL", 1000.0)])
    assert (stop, tgt) == (96.0, 112.0)
    broker.fill(r["client_order_id"], 4, 100, status="canceled")      # remainder cancelled
    broker.positions_list = [{"symbol": "MU", "qty": "4", "asset_class": "us_equity"}]
    om.reconcile(ReaderView(broker))
    summary = {"actions": [], "orders": []}
    C.reconcile_stock(cfg, journal, om, ReaderView(broker), broker.positions_list, "2026-10-28", summary)
    oco = [b for b in broker.submits if b.get("order_class") == "oco"]
    assert len(oco) == 1 and oco[0]["qty"] == "4" and oco[0]["stop_loss"]["stop_price"] == "96.0"
    assert set(broker.cancels) == {"OID-1-tp", "OID-1-sl"}


def test_insufficient_collateral_blocks_put(cfg, journal, broker):
    led = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=broker, ledger=led)
    led.post("WHEEL", "X", -79000, "test", "drain")
    r = om.place(strategy="WHEEL", symbol="MU261204P00150000", purpose="SELL_PUT_OPEN", signal_key="k",
                 body={"symbol": "MU261204P00150000", "qty": "1", "side": "sell", "type": "limit", "limit_price": "2.00"},
                 reservations=[("WHEEL", "PUT_COLLATERAL", 15002.0)],
                 cash_check=lambda db: C._cash_ok(db, led, "WHEEL", 15002.0))
    assert r["status"] == "BLOCKED" and not broker.submits


def test_cash_not_double_used_across_two_puts(cfg, journal, broker):
    led = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=broker, ledger=led)
    led.post("WHEEL", "X", -55000, "test", "drain")   # 25,000 free
    res = []
    for sym in ("AMD261204P00200000", "NVDA261204P00200000"):
        res.append(om.place(strategy="WHEEL", symbol=sym, purpose="SELL_PUT_OPEN", signal_key="k",
                            body={"symbol": sym, "qty": "1", "side": "sell", "type": "limit", "limit_price": "3.00"},
                            reservations=[("WHEEL", "PUT_COLLATERAL", 20002.0)],
                            cash_check=lambda db: C._cash_ok(db, led, "WHEEL", 20002.0)))
    assert [r["status"] for r in res] == ["accepted", "BLOCKED"]


def test_call_shares_not_double_reserved(cfg, journal, broker):
    om = OrderManager(journal, cfg, writer=broker)
    broker.positions_list = [{"symbol": "MU", "qty": "100", "asset_class": "us_equity"}]
    def check(db):
        held = db.execute("SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND kind='CALL_SHARES' AND symbol='MU'").fetchone()["a"]
        return (100 - held >= 100, "share coverage")
    out = [om.place(strategy="WHEEL", symbol="MU", purpose="SELL_CALL_OPEN", signal_key=k,
                    body={"symbol": f"MU2612{k}C00200000", "qty": "1", "side": "sell", "type": "limit", "limit_price": "2"},
                    reservations=[("WHEEL", "CALL_SHARES", 100)], cash_check=check)["status"] for k in ("04", "18")]
    assert out == ["accepted", "BLOCKED"]
