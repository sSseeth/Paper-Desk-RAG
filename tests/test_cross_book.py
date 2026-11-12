"""Cross-book exposure: a new stock entry and a new wheel put cannot share a symbol."""
import json
from datetime import datetime, timezone

from conftest import ReaderView, bars_trend, ts
from sakibdesk import cycle as C, research as R, stock as S
from sakibdesk.alpaca import BrokerError
from sakibdesk.calendar_util import Session
from sakibdesk.ledger import Ledger
from sakibdesk.orders import OrderManager
from test_research import record, wrap

NOW = datetime(2026, 10, 28, 14, 30, tzinfo=timezone.utc)  # both entry windows are open
TODAY = "2026-10-28"
PUT = "MU261204P00010000"
CALL = "MU261204C00200000"


def _session():
    return Session({"date": TODAY, "open": "09:30", "close": "16:00"})


def _reasons(journal, strategy=None):
    out = []
    for event in journal.events("GATE_FAIL"):
        if strategy and event["strategy"] != strategy:
            continue
        out.append(json.loads(event["payload"])["reason"])
    return out


def _intent_rows(journal):
    return journal.db.execute(
        "SELECT strategy, purpose, symbol, status FROM intents").fetchall()


def _arm(cfg, broker):
    """Bars, quotes, and one screened put so MU would enter either book."""
    mu = bars_trend()
    broker.bars["MU"] = mu
    broker.bars["SPY"] = bars_trend(start=400.0, step=0.5)
    completed = S.completed_bars(mu, TODAY)
    sig, why = S.signal(completed, cfg.policy["stock"])
    assert sig, why
    px = round(float(sig["signal_close"]), 2)
    stamp = ts(NOW)
    broker.quotes["MU"] = {"bp": round(px - 0.02, 2), "ap": px, "t": stamp}
    contract = {"symbol": PUT, "expiration_date": "2026-12-04", "strike_price": "10", "type": "put",
                "multiplier": "100", "size": "100", "style": "american", "root_symbol": "MU",
                "underlying_symbol": "MU", "tradable": True, "open_interest": "800",
                "open_interest_date": "2026-10-27"}
    broker.contracts = [contract]
    broker.snapshots[PUT] = {"latestQuote": {"bp": 1.0, "ap": 1.05, "t": stamp}, "greeks": {"delta": -0.22}}
    sess = _session()
    stock, wheel = cfg.policy["stock"], cfg.policy["wheel"]
    assert sess.in_window(NOW, stock["entry_window_start_min"], stock["entry_window_end_min"])
    assert sess.in_window(NOW, wheel["entry_start_min_after_open"],
                          stop_before_close_min=wheel["entry_stop_min_before_close"])


def _world(cfg, journal, broker):
    _arm(cfg, broker)
    ledger = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=broker, ledger=ledger)
    deps = C.Deps(reader=ReaderView(broker), writer=broker, md=broker)
    ok, errs, parsed = R.validate_record(record(), set(cfg.us_symbols), set(cfg.wheel_symbols), NOW)
    assert ok and parsed["wheel_ok"], (errs, parsed.get("wheel_reasons"))
    records = {"MU": (True, errs, parsed)}
    summary = {"orders": [], "no_trade": True, "actions": []}
    return ledger, om, deps, records, summary


def _stock(cfg, journal, broker, om, ledger, deps, records, summary):
    C.stock_entries(cfg, journal, om, ledger, deps, broker.cal, _session(), TODAY, NOW, records, summary)


def _wheel(cfg, journal, broker, om, ledger, deps, records, summary):
    C.wheel_entries(cfg, journal, om, ledger, deps, broker.cal, _session(), TODAY, NOW, records, summary)


def _intent(journal, cid, strategy, symbol, status, purpose):
    journal.db.execute(
        "INSERT INTO intents(client_order_id,created_utc,strategy,symbol,side,qty,limit_price,purpose,"
        "body,status,attempt,updated_utc) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, ts(NOW), strategy, symbol, "buy", 1, 1.0, purpose, "{}", status, 0, ts(NOW)))


def _reserve(journal, res_id, ledger, kind, symbol, amount):
    journal.db.execute(
        "INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)",
        (res_id, ledger, kind, symbol, amount, "ref-" + res_id, "ACTIVE", ts(NOW), ts(NOW)))


def _assert_skipped(journal, broker, strategy, code):
    reasons = _reasons(journal, strategy)
    assert code in reasons, reasons
    # A seeded PERSISTED/UNKNOWN intent is the exposure under test. A new entry
    # would have been submitted to the broker.
    assert broker.submits == [], broker.submits
    live = [row for row in _intent_rows(journal) if row["status"] not in ("PERSISTED", "UNKNOWN")]
    assert live == [], [tuple(row) for row in live]


def test_stock_entry_skipped_when_wheel_position_is_open(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    journal.db.execute("INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)",
                       ("C-PUT", "MU", "PUT_OPEN", "{}", "", ""))
    books = C.symbol_exposure(journal, om, ledger, [], [])
    assert books["MU"] == {"WHEEL"}
    # Option inventory with no cycle is its own filled exposure. The entry path has to read it.
    journal.db.execute("DELETE FROM wheel_cycles")
    books = C.symbol_exposure(journal, om, ledger, [
        {"symbol": "AMD261204P00100000", "qty": "-1", "asset_class": "us_option"}], [])
    assert books["AMD"] == {"WHEEL"}
    broker.positions_list = [{"symbol": PUT, "qty": "-1", "asset_class": "us_option"}]
    _stock(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "STOCK", "CROSS_BOOK_OVERLAP:WHEEL")


def test_stock_entry_skipped_when_wheel_order_is_open(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    books = C.symbol_exposure(journal, om, ledger, [], [
        {"symbol": "AMD", "status": "filled", "asset_class": "us_equity", "qty": "1"}])
    assert "AMD" not in books  # terminal status is not exposure

    def orders(status="open"):
        return [{"id": "OID-W", "client_order_id": "COID-W", "status": "pending_cancel",
                 "symbol": PUT, "side": "sell", "qty": "1", "asset_class": "us_option"}]

    broker.orders = orders
    _stock(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "STOCK", "CROSS_BOOK_OVERLAP:WHEEL")


def test_stock_entry_skipped_when_wheel_intent_is_unacked(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    _intent(journal, "CID-W", "WHEEL", PUT, "PERSISTED", "SELL_PUT_OPEN")
    _stock(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "STOCK", "CROSS_BOOK_OVERLAP:WHEEL")


def test_stock_entry_skipped_when_wheel_assignment_reserve_is_active(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    _reserve(journal, "put-collateral", "WHEEL", "PUT_COLLATERAL", "MU", 1003.0)
    assert om.reserved("WHEEL", kind="PUT_COLLATERAL", symbol="MU") == 1003.0
    _stock(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "STOCK", "CROSS_BOOK_OVERLAP:WHEEL")


def test_wheel_put_skipped_when_stock_position_is_open(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    journal.db.execute("INSERT INTO stock_positions VALUES(?,?,?)", ("MU", "{}", ts(NOW)))
    books = C.symbol_exposure(journal, om, ledger, [], [])
    assert books["MU"] == {"STOCK"}
    journal.db.execute("DELETE FROM stock_positions")
    books = C.symbol_exposure(journal, om, ledger, [
        {"symbol": "AMD", "qty": "8", "asset_class": "us_equity"}], [])
    assert books["AMD"] == {"STOCK"}
    broker.positions_list = [{"symbol": "MU", "qty": "8", "asset_class": "us_equity"}]
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "WHEEL", "CROSS_BOOK_OVERLAP:STOCK")


def test_wheel_put_skipped_when_stock_order_is_open(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)

    def orders(status="open"):
        return [{"id": "OID-S", "client_order_id": "COID-S", "status": "done_for_day",
                 "symbol": "MU", "side": "buy", "qty": "10", "asset_class": "us_equity"}]

    broker.orders = orders
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "WHEEL", "CROSS_BOOK_OVERLAP:STOCK")


def test_wheel_put_skipped_when_stock_intent_is_unacked(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    _intent(journal, "CID-S", "STOCK", "MU", "UNKNOWN", "ENTRY")
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "WHEEL", "CROSS_BOOK_OVERLAP:STOCK")


def test_wheel_put_skipped_when_stock_reserve_is_active(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    _reserve(journal, "stock-notional", "STOCK", "ORDER_NOTIONAL", "MU", 1500.0)
    assert om.reserved("STOCK", symbol="MU") == 1500.0
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    _assert_skipped(journal, broker, "WHEEL", "CROSS_BOOK_OVERLAP:STOCK")


def test_same_pass_keeps_only_the_first_intent(cfg, journal, root, broker, clock):
    clock.t = NOW
    _arm(cfg, broker)
    journal.set("healthy_streak", 3)
    journal.db.execute(
        "INSERT INTO research_cache(path, etag, content, fetched_utc) VALUES(?,?,?,?)",
        ("research/mu.json", None, json.dumps(wrap(record())), ts(NOW)))
    summary = C.run_cycle(cfg, journal, "AUTO_PAPER",
                          C.Deps(reader=ReaderView(broker), writer=broker, md=broker,
                                 expected_account_id="ACC-1"), root)
    rows = _intent_rows(journal)
    assert len(rows) == 1, ([(r["strategy"], r["purpose"], r["symbol"]) for r in rows],
                            _reasons(journal), summary.get("entry_blocks"),
                            summary.get("stock"), summary.get("wheel"))
    assert (rows[0]["strategy"], rows[0]["purpose"], rows[0]["symbol"]) == ("STOCK", "ENTRY", "MU")
    assert rows[0]["status"] in ("accepted", "new", "PERSISTED", "SUBMITTED", "UNKNOWN")
    assert "CROSS_BOOK_OVERLAP:STOCK" in _reasons(journal, "WHEEL")
    assert [body.get("position_intent") for body in broker.submits] == ["buy_to_open"]


def test_new_entries_fail_closed_when_open_orders_cannot_be_read(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)

    def orders(status="open"):
        raise BrokerError("open orders unavailable")

    broker.orders = orders
    _stock(cfg, journal, broker, om, ledger, deps, records, summary)
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    assert "CROSS_BOOK_OVERLAP:UNKNOWN" in _reasons(journal, "STOCK")
    assert "CROSS_BOOK_OVERLAP:UNKNOWN" in _reasons(journal, "WHEEL")
    assert _intent_rows(journal) == []
    assert broker.submits == []


def test_covered_call_on_assigned_shares_is_still_allowed(cfg, journal, broker):
    ledger, om, deps, records, summary = _world(cfg, journal, broker)
    journal.db.execute("INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)",
                       ("C-SHARES", "MU", "SHARES_HELD", json.dumps({"acquisition_price": 90.0}), "", ""))
    broker.positions_list = [{"symbol": "MU", "qty": "100", "asset_class": "us_equity"}]
    stamp = ts(NOW)
    broker.contracts.append(
        {"symbol": CALL, "expiration_date": "2026-12-04", "strike_price": "200", "type": "call",
         "multiplier": "100", "size": "100", "style": "american", "root_symbol": "MU",
         "underlying_symbol": "MU", "tradable": True, "open_interest": "800",
         "open_interest_date": "2026-10-27"})
    broker.snapshots[CALL] = {"latestQuote": {"bp": 1.0, "ap": 1.05, "t": stamp}, "greeks": {"delta": 0.22}}
    books = C.symbol_exposure(journal, om, ledger, broker.positions(), [])
    assert books["MU"] == {"WHEEL"}
    _wheel(cfg, journal, broker, om, ledger, deps, records, summary)
    calls = [row for row in _intent_rows(journal) if row["purpose"] == "SELL_CALL_OPEN"]
    puts = [row for row in _intent_rows(journal) if row["purpose"] == "SELL_PUT_OPEN"]
    assert len(calls) == 1 and puts == [], (_reasons(journal, "WHEEL"),
                                            [tuple(row) for row in _intent_rows(journal)])
    assert calls[0]["symbol"] == CALL
    assert not [reason for reason in _reasons(journal) if reason.startswith("CROSS_BOOK_OVERLAP")]
    assert any(body.get("symbol") == CALL and body.get("position_intent") == "sell_to_open"
               for body in broker.submits)
