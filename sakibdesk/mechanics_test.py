"""Guided lifecycle tests on the mechanics paper account.

Orders go through OrderManager. Stop, target, and the strategy exit use the
same bodies as the cycle. Wheel close, assignment, and the covered call use
the wheel state machine. Nothing here writes the main journal or the
investment-performance series.
"""
import json
import os
import shutil
import tempfile
import time
import uuid
from datetime import date, timedelta

from . import cycle as C
from . import lifecycle_gate as LG
from . import mechanics_config as MC
from . import stock as S
from . import wheel as W
from .alpaca import BrokerError
from .config import PAPER_TRADING_HOST
from .indicators import atr_wilder
from .journal import Journal, iso
from .ledger import Ledger
from .orders import OrderManager

_RESTING = {"new", "accepted", "pending_new", "accepted_for_bidding", "held"}
_OPEN = _RESTING | {"partially_filled"}
_PAPER_HOST = "https://paper-api.alpaca.markets"
_CLOSED = (
    "Refused: this suite runs only during regular US market hours "
    f"({MC.NY_HOURS}; currently {MC.SYDNEY_HOURS}). "
    "The Alpaca clock reports the session is closed. Nothing was sent."
)


def _closed_message():
    return _CLOSED


def session_open(reader):
    """True only when the Alpaca clock says the regular session is open."""
    try:
        clock = reader.clock() or {}
    except BrokerError:
        return False, {}
    return clock.get("is_open") is True, clock


def poll_order(reader, order_id, accept, attempts, interval, sleep):
    """Poll a broker order until its status is in `accept`, or attempts run out."""
    last = None
    for i in range(max(1, attempts)):
        last = reader.order(order_id) if order_id else None
        if (last or {}).get("status") in accept:
            return last
        if i + 1 < attempts:
            sleep(interval)
    return last


def equity_qty(positions, symbol):
    """Signed equity quantity for `symbol`. Short lots count negative."""
    total = 0.0
    for pos in positions or []:
        if pos.get("symbol") != symbol:
            continue
        if pos.get("asset_class") not in (None, "us_equity"):
            continue
        try:
            qty = float(pos.get("qty") or 0)
        except (TypeError, ValueError):
            continue
        if pos.get("side") == "short" and qty > 0:
            qty = -qty
        total += qty
    return total


def option_qty(positions, symbol):
    """Absolute contracts in `symbol`, or 0 when the position is gone."""
    total = 0.0
    for pos in positions or []:
        if pos.get("symbol") != symbol:
            continue
        try:
            total += abs(float(pos.get("qty") or 0))
        except (TypeError, ValueError):
            continue
    return total


def _row(step, result, evidence, order, note=""):
    return {
        "step": step,
        "result": result,
        "evidence": evidence or "",
        "status": (order or {}).get("status") or "",
        "broker_order_id": (order or {}).get("id") or "",
        "note": note,
        "order": order,
    }


def journal_step(journal, step, order, evidence, result, symbol, extra=None):
    """Append one mechanics step. `series` keeps it out of the performance book."""
    payload = {
        "series": MC.SERIES,
        "step": step,
        "result": result,
        "evidence": evidence,
        "broker_order_id": (order or {}).get("id"),
        "observed_status": (order or {}).get("status"),
        "observed_at_utc": iso(journal.clock()),
        "submitted_at": (order or {}).get("submitted_at"),
        "run_id": journal.run_id,
    }
    if extra:
        payload.update(extra)
    journal.event("MECHANICS_STEP", payload, strategy="MECHANICS", symbol=symbol)
    return payload


def _print_table(rows):
    print(f"{'step':<28} {'result':<6} {'evidence':<28} {'status':<18} broker_order_id")
    for row in rows:
        print(f"{row['step']:<28} {row['result']:<6} {row['evidence']:<28} "
              f"{row['status']:<18} {row['broker_order_id']}")
        if row.get("note"):
            print(f"  {row['note']}")


def _print_state(state):
    print("final broker state:")
    print(json.dumps(state, indent=2, default=str))


def _command(check, evidence):
    return (f'py desk.py lifecycle-record --account mechanics --check {check} '
            f'--evidence "{evidence}"')


def _print_command(check, evidence):
    print("lifecycle-record command:")
    print(_command(check, evidence))


def broker_snapshot(reader, symbols):
    """Positions and open orders for the test symbols. No account id."""
    wanted = set(symbols)
    positions = []
    for pos in reader.positions() or []:
        if pos.get("symbol") in wanted:
            positions.append({
                "symbol": pos.get("symbol"),
                "qty": pos.get("qty"),
                "side": pos.get("side"),
                "asset_class": pos.get("asset_class"),
            })
    orders = []
    for order in reader.orders("open") or []:
        if order.get("symbol") in wanted:
            orders.append({
                "id": order.get("id"),
                "symbol": order.get("symbol"),
                "status": order.get("status"),
                "side": order.get("side"),
                "qty": order.get("qty"),
            })
    equity = {sym: equity_qty(reader.positions(), sym) for sym in wanted if len(sym) <= 6}
    return {"positions": positions, "open_orders": orders, "equity_qty": equity}


def _preflight(reader, writer, expected_account_id):
    """Refuse before any order when the mechanics client is not the paper account."""
    if PAPER_TRADING_HOST != _PAPER_HOST:
        return "Refused: the trading host is not the hard-coded paper host. Nothing was sent."
    if writer is None or reader is None:
        return "Refused: mechanics-test has no paper writer. Nothing was sent."
    try:
        acct = reader.account()
    except BrokerError as exc:
        return f"Refused: could not read the mechanics account ({type(exc).__name__}). Nothing was sent."
    if not C.account_matches(acct, expected_account_id):
        return ("Refused: the broker account does not match the mechanics expected account id. "
                "Nothing was sent.")
    return None


def _quote(md, symbol):
    try:
        return (md.latest_quotes([symbol]) or {}).get(symbol) or {}
    except BrokerError:
        return {}


def _limit_ok(qty, px):
    return qty * float(px) <= MC.STOCK_MAX_NOTIONAL + 1e-9


def far_limit_price(bid):
    """A resting buy, about half the bid, still a positive tick under the market."""
    px = S.round_px(float(bid) * 0.50)
    if px >= float(bid):
        px = S.round_px(max(0.01, float(bid) - 0.01))
    if px <= 0:
        px = 0.01
    return px


def marketable_buy_limit(ask):
    """Buy limit at the ask, bumped one cent when rounding would sit below it."""
    px = S.round_px(float(ask))
    if px + 1e-9 < float(ask):
        px = S.round_px(float(ask) + 0.01)
    return px


def _risk_unit(cfg, md, symbol, entry, today):
    """The runner's ATR risk unit, or 5% of the fill when bars are unavailable."""
    p = cfg.policy["stock"]
    risk = None
    try:
        start = (today - timedelta(days=420)).isoformat()
        raw = (md.daily_bars([symbol], start) or {}).get(symbol, [])
        bars = S.completed_bars(raw, today.isoformat())
        atr = atr_wilder(bars, p["atr_period"])
        if atr and atr > 0:
            risk = p["r_atr_mult"] * atr
    except (BrokerError, AttributeError, KeyError, TypeError, ValueError):
        risk = None
    if not risk or risk <= 0 or risk >= entry:
        risk = max(0.05, entry * 0.05)
        if risk >= entry:
            risk = entry * 0.5
    return risk


_DONE = {"filled", "canceled", "expired", "rejected"}


def _cancel_open(om, reader, order_id, client_order_id=None):
    order = reader.order(order_id) if order_id else None
    if order is None:
        return None
    if order.get("status") in _DONE:
        for leg in order.get("legs") or []:
            if leg.get("status") not in _DONE and leg.get("id"):
                om.cancel_and_confirm(reader, leg["id"])
        return order
    return om.cancel_and_confirm(reader, order_id, client_order_id)


def _cancel_confirmed(om, reader, order_id, client_order_id=None):
    """Cancel one order. True only when that order and its legs are terminal.

    An accepted OCO holds the shares (`held_for_orders`) until this returns.
    """
    order = reader.order(order_id) if order_id else None
    if order is None:
        return False
    if order.get("status") not in _DONE:
        confirmed = om.cancel_and_confirm(reader, order_id, client_order_id)
        if confirmed is None or confirmed.get("status") not in _DONE:
            return False
        order = reader.order(order_id) or confirmed
    for leg in order.get("legs") or []:
        if leg.get("status") in _DONE or not leg.get("id"):
            continue
        confirmed = om.cancel_and_confirm(reader, leg["id"])
        if confirmed is None or confirmed.get("status") not in _DONE:
            return False
    return True


def _release_sells(om, reader, ids, symbol):
    """Confirm-cancel resting sells for `symbol`, including an OCO parent limit."""
    targets, seen = [], set()

    def add(order_id, cid=None):
        if order_id and order_id not in seen:
            seen.add(order_id)
            targets.append((order_id, cid))

    for item in list(ids):
        order = reader.order(item["id"]) if item.get("id") else None
        if not order or order.get("symbol") != symbol:
            continue
        if order.get("side") == "sell" and order.get("status") not in _DONE:
            add(item["id"], item.get("cid"))
        for leg in order.get("legs") or []:
            if leg.get("side") in (None, "sell") and leg.get("status") not in _DONE:
                add(leg.get("id"))
    for order in reader.orders("open") or []:
        if order.get("symbol") == symbol and order.get("side") == "sell" and order.get("status") not in _DONE:
            add(order.get("id"))
    for order_id, cid in targets:
        if not _cancel_confirmed(om, reader, order_id, cid):
            return False
    return True


def _remember(ids, result):
    order = result.get("order") or {}
    if order.get("id"):
        ids.append({"id": order["id"], "cid": result.get("client_order_id")})
    return result


def _resting_sell(leg, types):
    """One resting exit. A missing side is allowed; a buy is not the target or the stop."""
    if not leg or leg.get("side") not in (None, "sell"):
        return False
    return leg.get("type") in types and leg.get("status") in _RESTING


def _legs_live(order):
    """Exactly one resting limit sell and one resting stop, parent and legs together.

    OCO (Alpaca, nested): the parent is the take-profit limit (`new`, `accepted`,
    or `held`) and `legs` contains only the stop, often `held`. Bracket: the
    parent is the entry buy and both exits are in `legs`. Requiring the limit
    to appear inside `legs` rejects a live OCO.
    """
    group = S.protection_orders(order)
    stops = [leg for leg in group if _resting_sell(leg, ("stop", "stop_limit"))]
    targets = [leg for leg in group if _resting_sell(leg, ("limit",))]
    return len(stops) == 1 and len(targets) == 1


class _ScratchBroker:
    """In-memory broker for the partial-fill reservation proof. Sends nothing."""

    def __init__(self):
        self.by_cid = {}
        self.submits = 0
        self.n = 0

    def submit(self, body):
        self.submits += 1
        self.n += 1
        order = {
            "id": f"S{self.n}", "client_order_id": body["client_order_id"], "status": "accepted",
            "filled_qty": "0", "filled_avg_price": None, "side": body["side"], "symbol": body["symbol"],
            "qty": body["qty"], "asset_class": "us_equity",
        }
        self.by_cid[body["client_order_id"]] = order
        return dict(order)

    def order_by_client_id(self, cid):
        return self.by_cid.get(cid)

    def partial(self, cid, qty, px):
        order = self.by_cid[cid]
        order["status"] = "partially_filled"
        order["filled_qty"] = str(qty)
        order["filled_avg_price"] = str(px)


def prove_partial_fill(cfg, clock):
    """Runner partial-fill path on a scratch book.

    A live broker often will not partial-fill a 2-share far limit. This checks
    the same OrderManager: a partial keeps a reservation and a second place()
    with the same client_order_id does not submit again. Evidence is MOCK.
    """
    folder = tempfile.mkdtemp(prefix="mech-partial-")
    try:
        journal = Journal(os.path.join(folder, "partial.sqlite3"), cfg, clock=clock)
        broker = _ScratchBroker()
        ledger = Ledger(journal, cfg.policy)
        om = OrderManager(journal, cfg, writer=broker, ledger=ledger)
        body = {"symbol": MC.STOCK_SYMBOL, "qty": "2", "side": "buy", "type": "limit",
                "time_in_force": "day", "limit_price": "5.00", "position_intent": "buy_to_open"}
        first = om.place(strategy="STOCK", symbol=MC.STOCK_SYMBOL, purpose="MECH_PARTIAL_PROBE",
                         signal_key="scratch", body=body, reservations=[("STOCK", "ORDER_NOTIONAL", 10.0)])
        broker.partial(first["client_order_id"], 1, 5.0)
        om.reconcile(broker)
        reserved = om.reserved("STOCK")
        second = om.place(strategy="STOCK", symbol=MC.STOCK_SYMBOL, purpose="MECH_PARTIAL_PROBE",
                          signal_key="scratch", body=body, reservations=[("STOCK", "ORDER_NOTIONAL", 10.0)])
        return (first.get("status") == "accepted" and second.get("status") == "DUPLICATE"
                and broker.submits == 1 and 0 < reserved < 10)
    except (OSError, KeyError, TypeError, ValueError, BrokerError):
        return False
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _open_ids(reader, symbol):
    return sorted(order.get("id") for order in (reader.orders("open") or []) if order.get("symbol") == symbol)


def _finish(rows, state, passed, alert=""):
    _print_table(rows)
    _print_state(state)
    if alert:
        print(alert)
    print("RESULT:", "PASS" if passed else "FAIL")
    return 0 if passed else 2


def _record(root, check, expected_account_id, config_hash, evidence, enabled):
    _print_command(check, evidence)
    if not enabled:
        return True
    ok, msg = LG.record_check(root, check, expected_account_id, config_hash, evidence)
    if not ok:
        print(f"lifecycle-record refused: {msg}")
        return False
    print(f"Recorded {msg}.")
    return True


def run_stock_suite(cfg, journal, reader, writer, md, *, root, expected_account_id, record=False,
                    attempts=20, interval=1.0, sleep=time.sleep):
    """Limit, cancel, partial-fill protection, stop/target, and strategy exit.

    Cleans up test orders and the shares this run bought. A pre-existing
    position on the test symbol is refused so an assignment lot is not sold.
    """
    why = _preflight(reader, writer, expected_account_id)
    if why:
        print(why)
        return 2
    symbol = MC.STOCK_SYMBOL
    track = journal.get(MC.TRACK_KEY) or {}
    if track and not track.get("completed") and not track.get("closed_without_assignment"):
        if track.get("underlying") == symbol:
            print(f"Refused: a wheel-expiry track is open on {symbol}. "
                  "Reconcile it before the stock suite. Nothing was sent.")
            return 2
    if _symbol_busy(reader, symbol):
        print(f"Refused: {symbol} already has a position or an open order on the mechanics account. "
              "Nothing was sent.")
        return 2
    open_now, _clock = session_open(reader)
    if not open_now:
        print(_closed_message())
        return 2
    quote = _quote(md, symbol)
    try:
        bid, ask = float(quote["bp"]), float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        print(f"Refused: no two-sided quote for {symbol}. Nothing was sent.")
        return 2
    if ask < MC.STOCK_MIN_PRICE or ask > MC.STOCK_MAX_PRICE:
        print(f"Refused: {symbol} ask {ask:.2f} is outside the ${MC.STOCK_MIN_PRICE:.0f}-"
              f"${MC.STOCK_MAX_PRICE:.0f} mechanics band. Nothing was sent.")
        return 2

    ids = []
    bought = {"qty": 0.0}
    rows = []
    om = OrderManager(journal, cfg, writer=writer, ledger=Ledger(journal, cfg.policy))

    def _going():
        return bool(rows) and rows[-1]["result"] == "PASS"

    try:
        _step_limit_cancel(cfg, journal, om, reader, ids, rows, symbol, bid, attempts, interval, sleep)
        if _going():
            _step_partial(cfg, journal, om, reader, md, ids, rows, bought, symbol, bid,
                          attempts, interval, sleep)
        if _going():
            _step_buy(cfg, journal, om, reader, ids, rows, bought, symbol, ask, attempts, interval, sleep)
        if _going():
            _step_protect(cfg, journal, om, reader, md, ids, rows, symbol, attempts, interval, sleep)
        if _going():
            _step_exit(cfg, journal, om, reader, md, ids, rows, symbol, attempts, interval, sleep)
    except (BrokerError, OSError, KeyError, TypeError, ValueError) as exc:
        rows.append(_row("broker_error", "FAIL", "BROKER", None, type(exc).__name__))
        journal_step(journal, "broker_error", None, "BROKER", "FAIL", symbol, {"error": type(exc).__name__})
    finally:
        _flatten_shares(cfg, journal, om, reader, md, ids, symbol, bought["qty"], attempts, interval, sleep)
    state = broker_snapshot(reader, [symbol])
    flat = state["equity_qty"].get(symbol, 0) == 0 and not state["open_orders"]
    passed = bool(rows) and all(row["result"] == "PASS" for row in rows) and flat
    code = _finish(rows, state, passed)
    if not passed:
        return code
    partial = next(row["evidence"] for row in rows if row["step"] == "partial_fill_protection")
    evidence = (f"stock_lifecycle run {journal.run_id} broker-verified limit cancel, fill, "
                f"stop/target and strategy exit; partial-fill protection {partial}")
    if not _record(root, "stock", expected_account_id, cfg.config_hash, evidence, record):
        print("RESULT: FAIL")
        return 2
    return 0


def _symbol_busy(reader, symbol):
    if abs(equity_qty(reader.positions(), symbol)) > 0:
        return True
    return any(order.get("symbol") == symbol for order in (reader.orders("open") or []))


def _step_limit_cancel(cfg, journal, om, reader, ids, rows, symbol, bid, attempts, interval, sleep):
    px = far_limit_price(bid)
    if not _limit_ok(MC.STOCK_ENTRY_QTY, px):
        row = _row("limit_cancel", "FAIL", "BROKER", None, "far limit exceeds the notional cap")
        rows.append(row)
        return
    body = _stock_buy_body(symbol, MC.STOCK_ENTRY_QTY, px)
    result = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="MECH_LIMIT_ENTRY", signal_key=f"far:{journal.run_id}",
        body=body, reservations=[("STOCK", "ORDER_NOTIONAL", MC.STOCK_ENTRY_QTY * px)]))
    order = poll_order(reader, (result.get("order") or {}).get("id"), _RESTING | {"filled"}, attempts, interval, sleep)
    if (order or {}).get("status") not in _RESTING:
        row = _row("limit_cancel", "FAIL", "BROKER", order, "far limit was not accepted as a resting order")
        rows.append(row)
        journal_step(journal, "limit_cancel", order, "BROKER", "FAIL", symbol, {"note": row["note"]})
        return
    canceled = _cancel_open(om, reader, order["id"], result.get("client_order_id"))
    seen = canceled or poll_order(reader, order["id"], {"canceled"}, attempts, interval, sleep)
    ok = (seen or {}).get("status") == "canceled"
    row = _row("limit_cancel", "PASS" if ok else "FAIL", "BROKER", seen or order,
               "" if ok else "cancel was not confirmed")
    rows.append(row)
    journal_step(journal, "limit_cancel", seen or order, "BROKER", row["result"], symbol)


def _stock_buy_body(symbol, qty, px):
    return {"symbol": symbol, "qty": str(int(qty)), "side": "buy", "type": "limit",
            "time_in_force": "day", "limit_price": str(S.round_px(float(px))),
            "position_intent": "buy_to_open"}


def _step_partial(cfg, journal, om, reader, md, ids, rows, bought, symbol, bid, attempts, interval, sleep):
    px = far_limit_price(bid)
    qty = MC.STOCK_PARTIAL_QTY
    if not _limit_ok(qty, px):
        rows.append(_row("partial_fill_protection", "FAIL", MC.MOCK_EVIDENCE, None, "notional cap"))
        return
    body = _stock_buy_body(symbol, qty, px)
    first = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="MECH_PARTIAL_PROBE", signal_key=f"partial:{journal.run_id}",
        body=body, reservations=[("STOCK", "ORDER_NOTIONAL", qty * px)]))
    oid = (first.get("order") or {}).get("id")
    if first.get("status") in ("rejected", "BLOCKED", "DUPLICATE") or not oid:
        rows.append(_row("partial_fill_protection", "FAIL", "BROKER", first.get("order"),
                         f"probe {first.get('status')}"))
        journal_step(journal, "partial_fill_protection", first.get("order"), "BROKER", "FAIL", symbol)
        return
    order = poll_order(reader, oid, _OPEN | {"filled"}, attempts, interval, sleep)
    resting = _open_ids(reader, symbol)
    second = om.place(
        strategy="STOCK", symbol=symbol, purpose="MECH_PARTIAL_PROBE", signal_key=f"partial:{journal.run_id}",
        body=body, reservations=[("STOCK", "ORDER_NOTIONAL", qty * px)])
    after = _open_ids(reader, symbol)
    duplicate_ok = second.get("status") == "DUPLICATE" and resting == after
    om.reconcile(reader)
    reserved = om.reserved("STOCK")
    status = (order or {}).get("status")
    filled = float((order or {}).get("filled_qty") or 0)
    evidence = "BROKER"
    note = "duplicate submit blocked at the broker"
    ok = duplicate_ok and reserved > 0 and status in _OPEN
    if status == "partially_filled" and filled > 0:
        evidence = "BROKER"
        note = "broker partial fill kept a reservation and blocked a second order"
        ok = ok and filled < qty
    elif status in _RESTING and filled == 0:
        mock_ok = prove_partial_fill(cfg, journal.clock)
        evidence = MC.MOCK_EVIDENCE
        note = ("pending order kept its reservation and blocked a second order; "
                "partial-fill reservation checked on a scratch book (MOCK)"
                if mock_ok else "scratch partial-fill proof failed")
        ok = duplicate_ok and reserved > 0 and mock_ok
    else:
        ok = False
        note = f"unexpected status {status}"
    row = _row("partial_fill_protection", "PASS" if ok else "FAIL", evidence, order, note)
    rows.append(row)
    journal_step(journal, "partial_fill_protection", order, evidence, row["result"], symbol, {
        "duplicate_blocked": duplicate_ok, "reserved": reserved, "note": note,
    })
    _cancel_open(om, reader, oid, first.get("client_order_id"))
    om.reconcile(reader)
    held = equity_qty(reader.positions(), symbol)
    if held > 0:
        _flatten_shares(cfg, journal, om, reader, md, ids, symbol, held, attempts, interval, sleep)
        if equity_qty(reader.positions(), symbol) > 0:
            bought["qty"] += held
            rows[-1] = _row("partial_fill_protection", "FAIL", evidence, order, "partial fill did not flatten")


def _step_buy(cfg, journal, om, reader, ids, rows, bought, symbol, ask, attempts, interval, sleep):
    px = marketable_buy_limit(ask)
    if not _limit_ok(MC.STOCK_ENTRY_QTY, px):
        rows.append(_row("marketable_buy", "FAIL", "BROKER", None, "notional cap"))
        return
    body = _stock_buy_body(symbol, MC.STOCK_ENTRY_QTY, px)
    result = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="MECH_ENTRY", signal_key=f"buy:{journal.run_id}",
        body=body, reservations=[("STOCK", "ORDER_NOTIONAL", MC.STOCK_ENTRY_QTY * px)]))
    order = poll_order(reader, (result.get("order") or {}).get("id"), {"filled"}, attempts, interval, sleep)
    held = equity_qty(reader.positions(), symbol)
    ok = (order or {}).get("status") == "filled" and held >= MC.STOCK_ENTRY_QTY
    if ok:
        bought["qty"] += float(order.get("filled_qty") or MC.STOCK_ENTRY_QTY)
    row = _row("marketable_buy", "PASS" if ok else "FAIL", "BROKER", order,
               "" if ok else "buy was not filled or the position is missing")
    rows.append(row)
    journal_step(journal, "marketable_buy", order, "BROKER", row["result"], symbol, {"position_qty": held})


def _step_protect(cfg, journal, om, reader, md, ids, rows, symbol, attempts, interval, sleep):
    order = _latest(rows, "marketable_buy")
    try:
        entry = float((order or {}).get("filled_avg_price"))
    except (TypeError, ValueError):
        rows.append(_row("protect_stop_target", "FAIL", "BROKER", None, "fill price missing"))
        return
    clock = reader.clock() or {}
    today = date.fromisoformat(str(clock.get("timestamp") or iso(journal.clock()))[:10])
    risk = _risk_unit(cfg, md, symbol, entry, today)
    bracket, _stop, _target = S.bracket_body(symbol, MC.STOCK_ENTRY_QTY, entry, risk, cfg.policy["stock"])
    body = C.protection_oco_body(symbol, MC.STOCK_ENTRY_QTY, bracket["take_profit"], bracket["stop_loss"])
    entry_cid = _entry_cid(journal, rows)
    first = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="PROTECT_OCO", signal_key=entry_cid or f"oco:{journal.run_id}",
        body=body, reservations=[]))
    parent = poll_order(reader, (first.get("order") or {}).get("id"), _RESTING, attempts, interval, sleep)
    if not _legs_live(parent):
        row = _row("protect_stop_target", "FAIL", "BROKER", parent, "stop and target were not both accepted")
        rows.append(row)
        journal_step(journal, "protect_stop_target", parent, "BROKER", "FAIL", symbol, {"note": row["note"]})
        return
    journal_step(journal, "protect_stop_target", parent, "BROKER", "PASS", symbol)
    rows.append(_row("protect_stop_target", "PASS", "BROKER", parent, "stop and target accepted"))
    confirmed = _cancel_open(om, reader, parent["id"], first.get("client_order_id"))
    if confirmed is None or confirmed.get("status") not in ("canceled", "filled", "expired", "rejected"):
        row = _row("protect_cancel_replace", "FAIL", "BROKER", confirmed, "protective order cancel was not confirmed")
        rows.append(row)
        journal_step(journal, "protect_cancel_replace", confirmed, "BROKER", "FAIL", symbol)
        return
    replaced = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="PROTECT_OCO", signal_key=f"replace:{first.get('client_order_id')}",
        body=body, reservations=[], attempt=1))
    parent2 = poll_order(reader, (replaced.get("order") or {}).get("id"), _RESTING, attempts, interval, sleep)
    ok = _legs_live(parent2)
    row = _row("protect_cancel_replace", "PASS" if ok else "FAIL", "BROKER", parent2,
               "" if ok else "replacement stop and target were not both accepted")
    rows.append(row)
    journal_step(journal, "protect_cancel_replace", parent2, "BROKER", row["result"], symbol)


def _latest(rows, step):
    for row in reversed(rows):
        if row["step"] == step:
            return row.get("order")
    return None


def _entry_cid(journal, rows):
    order = _latest(rows, "marketable_buy") or {}
    cid = order.get("client_order_id")
    if cid:
        return cid
    return None


def _step_exit(cfg, journal, om, reader, md, ids, rows, symbol, attempts, interval, sleep):
    held = equity_qty(reader.positions(), symbol)
    if held != MC.STOCK_ENTRY_QTY:
        row = _row("strategy_exit", "FAIL", "BROKER", None, f"position qty {held} is not the test quantity")
        rows.append(row)
        journal_step(journal, "strategy_exit", None, "BROKER", "FAIL", symbol, {"position_qty": held})
        return
    # The OCO parent is a resting limit sell, so the shares are held_for_orders
    # until that cancel is confirmed. The cycle does the same before a strategy exit.
    if not _release_sells(om, reader, ids, symbol):
        row = _row("strategy_exit", "FAIL", "BROKER", None,
                   "protective order cancel was not confirmed; shares stay held_for_orders")
        rows.append(row)
        journal_step(journal, "strategy_exit", None, "BROKER", "FAIL", symbol, {"note": row["note"]})
        return
    held = equity_qty(reader.positions(), symbol)
    if held != MC.STOCK_ENTRY_QTY:
        row = _row("strategy_exit", "FAIL", "BROKER", None, f"position qty {held} is not the test quantity")
        rows.append(row)
        journal_step(journal, "strategy_exit", None, "BROKER", "FAIL", symbol, {"position_qty": held})
        return
    quote = _quote(md, symbol)
    if not quote.get("bp"):
        rows.append(_row("strategy_exit", "FAIL", "BROKER", None, "no bid for the strategy exit"))
        journal_step(journal, "strategy_exit", None, "BROKER", "FAIL", symbol)
        return
    body = C.strategy_exit_body(symbol, held, quote["bp"])
    result = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="STRATEGY_EXIT", signal_key=f"exit:{journal.run_id}",
        body=body, reservations=[]))
    order = poll_order(reader, (result.get("order") or {}).get("id"), {"filled"}, attempts, interval, sleep)
    left = equity_qty(reader.positions(), symbol)
    open_orders = [item for item in (reader.orders("open") or []) if item.get("symbol") == symbol]
    ok = (order or {}).get("status") == "filled" and left == 0 and not open_orders
    row = _row("strategy_exit", "PASS" if ok else "FAIL", "BROKER", order,
               "" if ok else "exit was not filled or orders remain")
    rows.append(row)
    journal_step(journal, "strategy_exit", order, "BROKER", row["result"], symbol, {"position_qty": left})


def _flatten_shares(cfg, journal, om, reader, md, ids, symbol, qty, attempts, interval, sleep):
    """Cancel this run's orders and sell only the shares this run bought.

    The sell waits until the protective OCO cancel is confirmed. A working
    OCO keeps the shares held_for_orders, and a limit sell submitted then is rejected.
    """
    if not _release_sells(om, reader, ids, symbol):
        return
    held = equity_qty(reader.positions(), symbol)
    sell_qty = int(min(held, qty)) if qty and held > 0 else 0
    if sell_qty < 1:
        return
    quote = _quote(md, symbol)
    if not quote.get("bp"):
        return
    body = C.strategy_exit_body(symbol, sell_qty, quote["bp"])
    result = _remember(ids, om.place(
        strategy="STOCK", symbol=symbol, purpose="STRATEGY_EXIT", signal_key=f"cleanup:{journal.run_id}:{sell_qty}",
        body=body, reservations=[]))
    order = poll_order(reader, (result.get("order") or {}).get("id"), {"filled"}, attempts, interval, sleep)
    if (order or {}).get("status") != "filled":
        _cancel_open(om, reader, (result.get("order") or {}).get("id"), result.get("client_order_id"))


def _allowance(cfg):
    return float(cfg.policy["wheel"]["put_reserve_cost_allowance_per_contract"])


def _cash(reader):
    acct = reader.account() or {}
    try:
        return float(acct["cash"])
    except (KeyError, TypeError, ValueError):
        return None


def _filter_summary(counts):
    """One line an operator can match to the stage that emptied the chain."""
    return ("contracts listed {listed}, after DTE filter {dte}, "
            "after strike filter {strike}, with positive bid {bid}").format(**counts)


def _choose_put(contracts, snaps, *, underlying, today, spot_bid, spot_ask, cash, allowance, mode):
    """Return `(chosen or None, counts)` for a far or slightly-ITM put.

    `far` is the highest strike at or below `FAR_OTM_FRACTION` of the bid on
    the nearest expiry that has a positive, sellable bid. `itm` is the lowest
    strike at or above 101% of the ask. Strikes above the mechanics cap, or
    whose strike*100+allowance exceeds cash, are skipped. Counts are the
    contracts that survived each stage, so a refusal says which filter was empty.
    """
    counts = {"listed": len(contracts or []), "dte": 0, "strike": 0, "bid": 0}
    by_exp = {}
    max_dte = MC.SELL_BUYBACK_MAX_DTE if mode == "far" else MC.EXPIRY_MAX_DTE
    for contract in contracts or []:
        if contract.get("type") != "put":
            continue
        standard, _why = W.standard_contract(contract, underlying)
        if not standard:
            continue
        exp = contract.get("expiration_date")
        if not exp:
            continue
        dte = (date.fromisoformat(exp) - today).days
        if dte < MC.MIN_DTE or dte > max_dte:
            continue
        counts["dte"] += 1
        try:
            strike = float(contract["strike_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if strike <= 0 or strike > MC.WHEEL_MAX_STRIKE:
            continue
        need = strike * 100 + allowance
        if need > cash:
            continue
        if mode == "far" and strike > float(spot_bid) * MC.FAR_OTM_FRACTION:
            continue
        if mode == "itm" and strike + 1e-9 < float(spot_ask) * MC.ITM_FRACTION:
            continue
        counts["strike"] += 1
        quote = ((snaps or {}).get(contract.get("symbol")) or {}).get("latestQuote") or {}
        try:
            bid = float(quote.get("bp") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        if bid > 0:
            counts["bid"] += 1
        if bid <= 0 or W.tick_round_down(bid) <= 0:
            continue
        by_exp.setdefault(exp, []).append({
            "symbol": contract["symbol"], "strike": strike, "expiry": exp, "dte": dte,
            "bid": bid, "need": need,
        })
    for exp in sorted(by_exp):
        cands = by_exp[exp]
        if mode == "far":
            return max(cands, key=lambda item: (item["strike"], item["symbol"])), counts
        return min(cands, key=lambda item: (item["strike"], item["symbol"])), counts
    return None, counts


def select_put(contracts, snaps, *, underlying, today, spot_bid, spot_ask, cash, allowance, mode):
    """Nearest listed put that fits the account. See `_choose_put` for the filters."""
    chosen, _counts = _choose_put(
        contracts, snaps, underlying=underlying, today=today, spot_bid=spot_bid, spot_ask=spot_ask,
        cash=cash, allowance=allowance, mode=mode)
    return chosen


def select_call(contracts, snaps, *, underlying, today, spot_ask):
    """Nearest call at or above the ask, one contract, with a live bid."""
    by_exp = {}
    ceiling = max(float(spot_ask) * 1.5, MC.WHEEL_MAX_STRIKE)
    for contract in contracts or []:
        if contract.get("type") != "call":
            continue
        standard, _why = W.standard_contract(contract, underlying)
        if not standard:
            continue
        exp = contract.get("expiration_date")
        if not exp:
            continue
        dte = (date.fromisoformat(exp) - today).days
        if dte < MC.MIN_DTE or dte > MC.SELL_BUYBACK_MAX_DTE:
            continue
        try:
            strike = float(contract["strike_price"])
        except (KeyError, TypeError, ValueError):
            continue
        if strike <= float(spot_ask) or strike > ceiling:
            continue
        quote = ((snaps or {}).get(contract.get("symbol")) or {}).get("latestQuote") or {}
        try:
            bid = float(quote.get("bp") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        if bid <= 0 or W.tick_round_down(bid) <= 0:
            continue
        by_exp.setdefault(exp, []).append({
            "symbol": contract["symbol"], "strike": strike, "expiry": exp, "dte": dte, "bid": bid,
        })
    for exp in sorted(by_exp):
        return min(by_exp[exp], key=lambda item: (item["strike"], item["symbol"]))
    return None


def _session_date(reader, journal):
    _open, clock = session_open(reader)
    stamp = (clock or {}).get("timestamp") or iso(journal.clock())
    return date.fromisoformat(str(stamp)[:10])


def _listed(reader, md, underlying, today, kind, horizon, strike_lte=None, strike_gte=None):
    """Contracts in the mechanics DTE window, then indicative snapshots for those symbols."""
    start = (today + timedelta(days=MC.MIN_DTE)).isoformat()
    end = (today + timedelta(days=horizon)).isoformat()
    kwargs = {}
    if strike_lte is not None:
        kwargs["strike_lte"] = strike_lte
    if strike_gte is not None:
        kwargs["strike_gte"] = strike_gte
    contracts = reader.option_contracts(underlying, start, end, kind, **kwargs) or []
    symbols = [contract.get("symbol") for contract in contracts if contract.get("symbol")]
    snaps = md.option_snapshots(symbols) if symbols else {}
    return contracts, snaps or {}


def _option_snapshot(md, symbol):
    try:
        return ((md.option_snapshots([symbol]) or {}).get(symbol) or {}).get("latestQuote") or {}
    except BrokerError:
        return {}


def _cycle(journal, cycle_id):
    for cycle in C.cycles(journal, active=None):
        if cycle["cycle_id"] == cycle_id:
            return cycle
    return None


def _open_cycle(journal, underlying, occ, strike, expiry, client_order_id, reserved, event):
    cycle_id = uuid.uuid4().hex[:12]
    now = iso(journal.clock())
    journal.db.execute(
        "INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)",
        (cycle_id, underlying, "CASH", "{}", now, now))
    cycle = {"cycle_id": cycle_id, "symbol": underlying, "state": "CASH", "data": {}}
    C.set_state(journal, cycle, event, option_symbol=occ, strike=strike, expiry=expiry,
                pending_cid=client_order_id, reserved=reserved)
    return cycle


def _activity_label(occ, activities):
    types = {row.get("activity_type") for row in activities or [] if row.get("symbol") == occ}
    if types & {"OPASN", "OPXRC"}:
        return MC.OBSERVED_ASSIGNMENT
    if "OPEXP" in types:
        return "OPEXP"
    return MC.MOCK_EVIDENCE


def _sync_wheel(journal, om, ledger, reader):
    summary = {"actions": [], "orders": []}
    om.reconcile(reader)
    positions = reader.positions() or []
    activities = reader.activities() or []
    C.reconcile_wheel(journal, om, ledger, positions, activities, summary)
    return positions, activities


def run_wheel_sell_buyback(cfg, journal, reader, writer, md, *, root, expected_account_id, record=True,
                           attempts=MC.PRICE_WALK_ATTEMPTS, interval=MC.PRICE_WALK_INTERVAL_SEC,
                           sleep=time.sleep):
    """Sell one far-OTM cash-secured put and buy it back. Records on PASS.

    The option walk polls ``MC.PRICE_WALK_ATTEMPTS`` times at
    ``MC.PRICE_WALK_INTERVAL_SEC``. ``attempts`` and ``interval`` stay in the
    signature so existing callers keep working; they do not shorten that walk.
    """
    why = _preflight(reader, writer, expected_account_id)
    if why:
        print(why)
        return 2
    open_now, _clock = session_open(reader)
    if not open_now:
        print(_closed_message())
        return 2
    underlying = MC.WHEEL_UNDERLYING
    quote = _quote(md, underlying)
    try:
        bid, ask = float(quote["bp"]), float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        print(f"Refused: no two-sided quote for {underlying}. Nothing was sent.")
        return 2
    if ask < MC.STOCK_MIN_PRICE or ask > MC.STOCK_MAX_PRICE:
        print(f"Refused: {underlying} ask {ask:.2f} is outside the mechanics band. Nothing was sent.")
        return 2
    cash = _cash(reader)
    if cash is None:
        print("Refused: mechanics cash is unreadable. Nothing was sent.")
        return 2
    today = _session_date(reader, journal)
    # Cap the chain at the far strike so a short page of high strikes cannot hide it.
    far_cap = min(MC.WHEEL_MAX_STRIKE, float(bid) * MC.FAR_OTM_FRACTION)
    contracts, snaps = _listed(reader, md, underlying, today, "put", MC.SELL_BUYBACK_MAX_DTE,
                               strike_lte=far_cap)
    chosen, counts = _choose_put(contracts, snaps, underlying=underlying, today=today, spot_bid=bid,
                                 spot_ask=ask, cash=cash, allowance=_allowance(cfg), mode="far")
    if chosen is None:
        print("Refused: no far-OTM weekly put whose strike*100 plus allowance fits mechanics cash. "
              f"Nothing was sent. {_filter_summary(counts)}")
        return 2
    ids = []
    rows = []
    ledger = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=writer, ledger=ledger)
    occ = chosen["symbol"]
    flatten = {"occ": None, "cycle_id": None}
    try:
        _sell_and_buyback(cfg, journal, om, ledger, reader, md, ids, rows, flatten, chosen, attempts, interval, sleep)
    except BrokerError as exc:
        rows.append(_row("broker_error", "FAIL", "BROKER", None, type(exc).__name__))
    finally:
        if flatten["occ"] and option_qty(reader.positions(), flatten["occ"]) > 0:
            _buy_to_close(journal, om, reader, md, ids, flatten["occ"], attempts, interval, sleep)
            _sync_wheel(journal, om, ledger, reader)
        for item in ids:
            _cancel_open(om, reader, item["id"], item.get("cid"))
    state = broker_snapshot(reader, [underlying, occ])
    short = option_qty(reader.positions(), occ)
    flat = short == 0 and not [
        order for order in state["open_orders"] if order.get("symbol") == occ]
    passed = bool(rows) and all(row["result"] == "PASS" for row in rows) and flat
    alert = ""
    if short > 0:
        alert = (f"SHORT PUT STILL OPEN: {occ} qty {short:g}. "
                 "Buyback and cleanup did not flatten this short put.")
    code = _finish(rows, state, passed, alert)
    if not passed:
        return code
    evidence = f"wheel_sell_buyback run {journal.run_id} broker fill and buyback {occ}"
    if not _record(root, "wheel-sell-buyback", expected_account_id, cfg.config_hash, evidence, record):
        print("RESULT: FAIL")
        return 2
    return 0


def _distinct_prices(prices):
    """Drop non-positive duplicates, keeping order. At most the step cap."""
    out = []
    for px in prices:
        px = round(float(px), 2)
        if px <= 0:
            continue
        if out and abs(out[-1] - px) < 1e-9:
            continue
        out.append(px)
    if len(out) <= MC.PRICE_WALK_MAX_STEPS:
        return out
    # Keep the quote and the floor or cap if a ladder ever grows.
    return [out[0], out[-1]]


def _sell_price_walk(bid):
    """Limits for a sell the indicative bid did not get filled.

    The first price is the bid tick-rounded down. The second, when it sits
    strictly between that price and the floor, is half of it, also
    tick-rounded down. The last is ``MC.SELL_PUT_FLOOR`` (the minimum tick),
    which the nickel grid would round away.

    Args:
        bid: Indicative bid per share. Not an OPRA quote.

    Returns:
        Highest limit first, at most ``MC.PRICE_WALK_MAX_STEPS`` prices.
    """
    floor = round(float(MC.SELL_PUT_FLOOR), 2)
    first = W.tick_round_down(float(bid))
    if first < floor:
        first = floor
    prices = [first]
    half = W.tick_round_down(first / 2.0)
    if floor + 1e-9 < half < first - 1e-9:
        prices.append(half)
    if abs(prices[-1] - floor) >= 1e-9:
        prices.append(floor)
    return _distinct_prices(prices)


def _buy_price_walk(ask):
    """Limits for a buy the indicative ask did not get filled.

    Starts at the ask tick-rounded up and doubles once, then stops at
    ``MC.BUYBACK_MAX_PRICE``. A quote already above the cap is a single
    order at the cap. No price is above the cap.

    Args:
        ask: Indicative ask per share. Not an OPRA quote.

    Returns:
        Lowest limit first, at most ``MC.PRICE_WALK_MAX_STEPS`` prices.
    """
    cap = round(float(MC.BUYBACK_MAX_PRICE), 2)
    start = W.tick_round_up(float(ask))
    if start > cap or start <= 0:
        start = cap
    prices = [start]
    doubled = min(cap, W.tick_round_up(start * 2.0))
    if start + 1e-9 < doubled < cap - 1e-9:
        prices.append(doubled)
    if abs(prices[-1] - cap) >= 1e-9:
        prices.append(cap)
    return _distinct_prices(prices)


def _clamp_walk_price(side, limit):
    """Keep a sell at or above the floor and a buy at or below the cap."""
    if side == "buy":
        return min(round(float(limit), 2), round(float(MC.BUYBACK_MAX_PRICE), 2))
    return max(round(float(limit), 2), round(float(MC.SELL_PUT_FLOOR), 2))


def _format_walk(label, prices):
    body = " -> ".join(f"{px:.2f}" for px in prices) if prices else "(none)"
    return f"price walk {label}: {body}"


def _walk_evidence(walk, ok):
    """Failure rows name the last limit, the broker status, and the reprices."""
    sent = walk["sent"]
    order = walk["order"] or {}
    place = walk["place"] or {}
    status = order.get("status") or place.get("status") or "none"
    reprices = max(0, len(sent) - 1)
    limit_txt = f"{sent[-1]:.2f}" if sent else "none"
    detail = f"last limit {limit_txt}; broker status {status}; reprices {reprices}"
    if ok:
        return "BROKER", detail
    evidence = f"BROKER limit={limit_txt} status={status} reprices={reprices}"
    return evidence, detail


def _working_side(reader, symbol, side):
    for order in reader.orders("open") or []:
        if order.get("symbol") == symbol and order.get("side") == side:
            return True
    return False


def _fresh_order(reader, order_or_id):
    if isinstance(order_or_id, dict):
        order_or_id = order_or_id.get("id")
    if not order_or_id:
        return None
    return reader.order(order_or_id)


def _filled_qty(order):
    try:
        return float((order or {}).get("filled_qty") or 0)
    except (TypeError, ValueError):
        return 0.0


def _position_shows_fill(reader, symbol, side):
    """True when another order on this side would double the fill.

    A sell that already opened the short, or a buy that already flattened
    it, must not be replaced. Call this only after this walk has submitted.
    """
    qty = option_qty(reader.positions(), symbol)
    if side == "sell":
        return qty > 0
    return qty == 0


def _fill_visible(reader, order, symbol, side):
    if (order or {}).get("status") == "filled" or _filled_qty(order) > 0:
        return True
    return _position_shows_fill(reader, symbol, side)


def _reservation_active(journal, client_order_id):
    if not client_order_id:
        return False
    row = journal.db.execute(
        "SELECT 1 FROM reservations WHERE client_order_id=? AND status='ACTIVE' LIMIT 1",
        (client_order_id,)).fetchone()
    return row is not None


def _safe_reconcile(om, reader):
    try:
        om.reconcile(reader)
    except BrokerError:
        return


def _cancel_for_replace(om, reader, journal, order, client_order_id, symbol, side):
    """Cancel one resting order and say whether a replace is safe.

    Re-reads the order and the position after the cancel. A cancel that
    races a fill returns ``filled`` and must not be replaced. A sell replace
    also waits until that order's reservation is released, so the next
    submit cannot reserve the collateral twice. The writer has cancel and
    submit, not replace, so this is cancel-then-new.

    Returns:
        ``("filled"|"clear"|"unconfirmed"|"busy"|"reserved"|"missing", order)``.
    """
    order = _fresh_order(reader, order) or order
    if _fill_visible(reader, order, symbol, side):
        _safe_reconcile(om, reader)
        return "filled", _fresh_order(reader, order) or order
    order_id = (order or {}).get("id")
    if not order_id:
        return "missing", order
    confirmed = None
    try:
        confirmed = _cancel_open(om, reader, order_id, client_order_id)
    except BrokerError:
        # The fill can win the race and make cancel raise. The re-read below
        # decides; a second sell or buy is not submitted from this branch.
        confirmed = None
    order = _fresh_order(reader, order_id) or confirmed or order
    if _fill_visible(reader, order, symbol, side):
        _safe_reconcile(om, reader)
        return "filled", _fresh_order(reader, order) or order
    status = (order or {}).get("status")
    if status not in ("canceled", "expired", "rejected"):
        return "unconfirmed", order
    _safe_reconcile(om, reader)
    order = _fresh_order(reader, order_id) or order
    if _fill_visible(reader, order, symbol, side):
        return "filled", order
    if _working_side(reader, symbol, side):
        return "busy", order
    if side == "sell" and _reservation_active(journal, client_order_id):
        return "reserved", order
    return "clear", order


def _walk_result(sent, order, place, settled):
    return {"sent": list(sent), "order": order, "place": place, "settled": settled}


def _walk_option(journal, om, reader, ids, *, symbol, side, intent, purpose, signal_key,
                 prices, reservations, cash_check, label, sleep):
    """Submit one limit at a time, walking ``prices`` until one fills.

    Never more than one order on ``side`` is working. Each unfilled limit
    is cancel-confirmed before the next price. Polls use the mechanics
    constants, not the caller's shorter budget.
    """
    sent = []
    last_order = None
    last_place = None
    if side == "sell" and option_qty(reader.positions(), symbol) > 0:
        print(_format_walk(label, sent))
        return _walk_result(sent, None, None, False)
    if side == "buy" and option_qty(reader.positions(), symbol) == 0:
        print(_format_walk(label, sent))
        return _walk_result(sent, None, None, True)

    for index, raw in enumerate(prices):
        limit = _clamp_walk_price(side, raw)
        if sent and abs(sent[-1] - limit) < 1e-9:
            continue
        if _working_side(reader, symbol, side):
            break
        prev_cid = (last_place or {}).get("client_order_id")
        if side == "sell" and prev_cid and _reservation_active(journal, prev_cid):
            break
        if sent and _fill_visible(reader, last_order, symbol, side):
            print(_format_walk(label, sent))
            return _walk_result(sent, last_order, last_place, True)
        print(f"price walk {label} step {len(sent) + 1}/{len(prices)}: limit {limit:.2f}")
        body = W.option_order_body(symbol, side, limit, intent, MC.WHEEL_CONTRACTS)
        last_place = om.place(
            strategy="WHEEL", symbol=symbol, purpose=purpose, signal_key=signal_key,
            body=body, reservations=reservations, cash_check=cash_check, attempt=index)
        sent.append(limit)
        if last_place.get("status") in ("BLOCKED", "DUPLICATE", "rejected", "DRY_RUN"):
            break
        order_id = (last_place.get("order") or {}).get("id")
        if not order_id:
            break
        _remember(ids, last_place)
        last_order = poll_order(
            reader, order_id, {"filled"}, MC.PRICE_WALK_ATTEMPTS, MC.PRICE_WALK_INTERVAL_SEC, sleep)
        if _fill_visible(reader, last_order, symbol, side):
            last_order = _fresh_order(reader, last_order) or last_order
            print(_format_walk(label, sent))
            return _walk_result(sent, last_order, last_place, True)
        kind, last_order = _cancel_for_replace(
            om, reader, journal, last_order or last_place.get("order"),
            last_place.get("client_order_id"), symbol, side)
        if kind == "filled":
            print(_format_walk(label, sent))
            return _walk_result(sent, last_order, last_place, True)
        if kind != "clear":
            break
    print(_format_walk(label, sent))
    return _walk_result(sent, last_order, last_place, False)


def _record_walk_row(rows, journal, step, symbol, walk, ok, fail_prefix, extra):
    evidence, detail = _walk_evidence(walk, ok)
    line = _format_walk(step, walk["sent"])
    note = line if ok else f"{fail_prefix}; {line}; {detail}"
    order = walk["order"]
    rows.append(_row(step, "PASS" if ok else "FAIL", evidence, order, note))
    place = walk["place"] or {}
    status = (order or {}).get("status") or place.get("status") or "none"
    payload = {
        "price_walk": [f"{px:.2f}" for px in walk["sent"]],
        "reprices": max(0, len(walk["sent"]) - 1),
        "broker_status": status,
        "last_limit": f"{walk['sent'][-1]:.2f}" if walk["sent"] else "none",
    }
    if extra:
        payload.update(extra)
    journal_step(journal, step, order, evidence, "PASS" if ok else "FAIL", symbol, payload)


def _sell_and_buyback(cfg, journal, om, ledger, reader, md, ids, rows, flatten, chosen, attempts, interval, sleep):
    """Sell the put, then buy it back, walking off the indicative quote.

    ``attempts`` and ``interval`` are unused. The walk polls
    ``MC.PRICE_WALK_ATTEMPTS`` times so a short caller budget cannot leave
    the indicative price working.
    """
    del attempts, interval
    occ = chosen["symbol"]
    need = chosen["need"]
    sell = _walk_option(
        journal, om, reader, ids, symbol=occ, side="sell", intent="sell_to_open",
        purpose="SELL_PUT_OPEN", signal_key=f"put:{journal.run_id}:{occ}",
        prices=_sell_price_walk(chosen["bid"]),
        reservations=[("WHEEL", "PUT_COLLATERAL", need, MC.WHEEL_UNDERLYING)],
        cash_check=lambda db, need=need: C._cash_ok(db, ledger, "WHEEL", need),
        label="sell_put", sleep=sleep)
    order = sell["order"]
    held = option_qty(reader.positions(), occ)
    ok = (order or {}).get("status") == "filled" and held > 0
    _record_walk_row(rows, journal, "sell_put", occ, sell, ok, "put was not filled",
                     {"strike": chosen["strike"]})
    if not ok:
        if held > 0:
            flatten["occ"] = occ
        return
    place = sell["place"] or {}
    cycle = _open_cycle(journal, MC.WHEEL_UNDERLYING, occ, chosen["strike"], chosen["expiry"],
                        place.get("client_order_id"), need, "PUT_SUBMITTED")
    flatten["occ"] = occ
    flatten["cycle_id"] = cycle["cycle_id"]
    _sync_wheel(journal, om, ledger, reader)
    cycle = _cycle(journal, cycle["cycle_id"])
    if cycle is None or cycle["state"] != "PUT_OPEN":
        rows.append(_row("sell_put_state", "FAIL", "BROKER", order, "runner did not reach PUT_OPEN"))
        return
    quote = _option_snapshot(md, occ)
    try:
        ask = float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        print("price walk buyback: (none)")
        rows.append(_row(
            "buyback", "FAIL", "BROKER limit=none status=none reprices=0", None,
            "no ask to buy the put back; price walk buyback: (none); "
            "last limit none; broker status none; reprices 0"))
        journal_step(journal, "buyback", None, "BROKER limit=none status=none reprices=0", "FAIL", occ, {
            "last_limit": "none", "broker_status": "none", "reprices": 0, "price_walk": [],
        })
        return
    buy = _walk_option(
        journal, om, reader, ids, symbol=occ, side="buy", intent="buy_to_close",
        purpose="BUY_TO_CLOSE", signal_key=f"buyback:{journal.run_id}:{occ}",
        prices=_buy_price_walk(ask), reservations=[], cash_check=None,
        label="buyback", sleep=sleep)
    seen = buy["order"]
    if (buy["place"] or {}).get("status") not in ("BLOCKED", "DUPLICATE", "rejected", "DRY_RUN"):
        if buy["place"] and cycle["state"] == "PUT_OPEN":
            C.set_state(journal, cycle, "BUYBACK_SUBMITTED", pending_cid=buy["place"].get("client_order_id"))
    _sync_wheel(journal, om, ledger, reader)
    flat = option_qty(reader.positions(), occ) == 0 and (seen or {}).get("status") == "filled"
    blocked = (buy["place"] or {}).get("status")
    if blocked in ("BLOCKED", "DUPLICATE", "rejected", "DRY_RUN"):
        prefix = f"buyback {blocked}"
    else:
        prefix = "buyback was not filled"
    _record_walk_row(rows, journal, "buyback", occ, buy, flat, prefix, None)
    if flat:
        flatten["occ"] = None


def _buy_to_close(journal, om, reader, md, ids, occ, attempts, interval, sleep):
    """Buy the short put back, walking the indicative ask up to the cap.

    ``attempts`` and ``interval`` are unused; the walk uses the mechanics
    poll window. A still-open short is left for the caller to report.
    """
    del attempts, interval
    if option_qty(reader.positions(), occ) <= 0:
        return None
    quote = _option_snapshot(md, occ)
    try:
        ask = float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        return None
    walk = _walk_option(
        journal, om, reader, ids, symbol=occ, side="buy", intent="buy_to_close",
        purpose="BUY_TO_CLOSE", signal_key=f"cleanup:{journal.run_id}:{occ}",
        prices=_buy_price_walk(ask), reservations=[], cash_check=None,
        label="buy_to_close", sleep=sleep)
    return walk["order"]


def run_wheel_expiry(cfg, journal, reader, writer, md, *, root, expected_account_id, action,
                     record=True, attempts=20, interval=1.0, sleep=time.sleep):
    """Multi-day assignment track. Only a full observed completion records wheel_lifecycle."""
    if action not in ("start", "reconcile"):
        print("mechanics-test refused: wheel-expiry action must be start or reconcile.")
        return 2
    why = _preflight(reader, writer, expected_account_id)
    if why:
        print(why)
        return 2
    if action == "start":
        return _expiry_start(cfg, journal, reader, writer, md, attempts, interval, sleep)
    return _expiry_reconcile(cfg, journal, reader, writer, md, root, expected_account_id,
                             record, attempts, interval, sleep)


def _expiry_start(cfg, journal, reader, writer, md, attempts, interval, sleep):
    track = journal.get(MC.TRACK_KEY) or {}
    if track and not track.get("completed") and not track.get("closed_without_assignment"):
        print("Refused: a wheel-expiry track is already open. Run --reconcile. Nothing was sent.")
        return 2
    open_now, _clock = session_open(reader)
    if not open_now:
        print(_closed_message())
        return 2
    underlying = MC.WHEEL_UNDERLYING
    if abs(equity_qty(reader.positions(), underlying)) > 0:
        print(f"Refused: {underlying} already has shares on the mechanics account. Nothing was sent.")
        return 2
    quote = _quote(md, underlying)
    try:
        bid, ask = float(quote["bp"]), float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        print(f"Refused: no two-sided quote for {underlying}. Nothing was sent.")
        return 2
    cash = _cash(reader)
    if cash is None:
        print("Refused: mechanics cash is unreadable. Nothing was sent.")
        return 2
    today = _session_date(reader, journal)
    contracts, snaps = _listed(
        reader, md, underlying, today, "put", MC.EXPIRY_MAX_DTE,
        strike_gte=float(ask) * MC.ITM_FRACTION, strike_lte=MC.WHEEL_MAX_STRIKE)
    chosen, counts = _choose_put(contracts, snaps, underlying=underlying, today=today, spot_bid=bid,
                                 spot_ask=ask, cash=cash, allowance=_allowance(cfg), mode="itm")
    if chosen is None:
        print("Refused: no near-expiry slightly-ITM put that fits mechanics cash. "
              f"Nothing was sent. {_filter_summary(counts)}")
        return 2
    ledger = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=writer, ledger=ledger)
    ids = []
    occ = chosen["symbol"]
    limit = W.tick_round_down(chosen["bid"])
    body = W.option_order_body(occ, "sell", limit, "sell_to_open", MC.WHEEL_CONTRACTS)
    result = _remember(ids, om.place(
        strategy="WHEEL", symbol=occ, purpose="SELL_PUT_OPEN", signal_key=f"expiry:{journal.run_id}:{occ}",
        body=body, reservations=[("WHEEL", "PUT_COLLATERAL", chosen["need"], underlying)],
        cash_check=lambda db, need=chosen["need"]: C._cash_ok(db, ledger, "WHEEL", need)))
    order = poll_order(reader, (result.get("order") or {}).get("id"), {"filled"}, attempts, interval, sleep)
    if (order or {}).get("status") != "filled":
        _cancel_open(om, reader, (result.get("order") or {}).get("id"), result.get("client_order_id"))
        if option_qty(reader.positions(), occ) > 0:
            _buy_to_close(journal, om, reader, md, ids, occ, attempts, interval, sleep)
        journal_step(journal, "wheel_expiry_start", order, "BROKER", "FAIL", occ)
        print("RESULT: FAIL")
        _print_state(broker_snapshot(reader, [underlying, occ]))
        print("wheel_lifecycle NOT recorded: the put did not fill.")
        return 2
    cycle = _open_cycle(journal, underlying, occ, chosen["strike"], chosen["expiry"],
                        result.get("client_order_id"), chosen["need"], "PUT_SUBMITTED")
    _sync_wheel(journal, om, ledger, reader)
    cycle = _cycle(journal, cycle["cycle_id"])
    if cycle is None or cycle["state"] != "PUT_OPEN":
        _buy_to_close(journal, om, reader, md, ids, occ, attempts, interval, sleep)
        print("RESULT: FAIL")
        print("wheel_lifecycle NOT recorded: the runner did not reach PUT_OPEN.")
        return 2
    track = {
        "cycle_id": cycle["cycle_id"],
        "underlying": underlying,
        "put_symbol": occ,
        "put_order_id": order.get("id"),
        "strike": chosen["strike"],
        "expiry": chosen["expiry"],
        "put_evidence": None,
        "call_symbol": None,
        "call_evidence": None,
        "call_outcome": None,
        "started_run_id": journal.run_id,
        "completed": False,
        "closed_without_assignment": False,
    }
    journal.set(MC.TRACK_KEY, track)
    journal_step(journal, "wheel_expiry_start", order, "BROKER", "PASS", occ, {
        "strike": chosen["strike"], "expiry": chosen["expiry"], "run_id": journal.run_id,
    })
    print(f"RESULT: STARTED put {occ} strike {chosen['strike']} expiry {chosen['expiry']}")
    print("wheel_lifecycle NOT recorded: assignment has not been observed. "
          "Run --suite wheel-expiry --reconcile on a later session.")
    _print_state(broker_snapshot(reader, [underlying, occ]))
    return 0


def _expiry_reconcile(cfg, journal, reader, writer, md, root, expected_account_id, record,
                      attempts, interval, sleep):
    track = journal.get(MC.TRACK_KEY)
    if not track:
        print("mechanics-test refused: no wheel-expiry track. Run --suite wheel-expiry --start first. "
              "Nothing was sent.")
        return 2
    if track.get("completed"):
        print("RESULT: PASS")
        print("wheel-expiry track is already complete.")
        return 0
    ledger = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=writer, ledger=ledger)
    cycle = _cycle(journal, track["cycle_id"])
    if cycle is None:
        print("mechanics-test refused: the wheel cycle for this track is missing.")
        return 2
    before = cycle["state"]
    _positions, activities = _sync_wheel(journal, om, ledger, reader)
    cycle = _cycle(journal, track["cycle_id"])
    underlying = track["underlying"]
    put_symbol = track["put_symbol"]

    if not track.get("call_symbol") and before in ("PUT_OPEN", "PUT_CLOSING", "PUT_PENDING", "ASSIGNMENT_PENDING"):
        if cycle["state"] == "PUT_OPEN":
            journal_step(journal, "wheel_expiry_reconcile", None, "BROKER", "WAITING", put_symbol,
                         {"state": "PUT_OPEN"})
            print("RESULT: WAITING")
            print(f"Put {put_symbol} is still open. Nothing was recorded.")
            return 0
        if cycle["state"] == "ASSIGNMENT_PENDING":
            track["put_evidence"] = MC.MOCK_EVIDENCE
            journal.set(MC.TRACK_KEY, track)
            journal_step(journal, "wheel_expiry_reconcile", None, MC.MOCK_EVIDENCE, "MOCK", put_symbol, {
                "note": "option gone without OPASN/OPXRC/OPEXP",
            })
            print("RESULT: MOCK")
            print("wheel_lifecycle NOT recorded: evidence is MOCK. "
                  "Mock state transitions prove the code path only.")
            return 0
        if cycle["state"] == "CLOSED":
            track["put_evidence"] = "OPEXP"
            track["closed_without_assignment"] = True
            journal.set(MC.TRACK_KEY, track)
            journal_step(journal, "wheel_expiry_reconcile", None, "OPEXP", "CLOSED", put_symbol)
            print("RESULT: CLOSED")
            print("wheel_lifecycle NOT recorded: the put expired or closed without an observed assignment.")
            return 0
        if cycle["state"] == "SHARES_HELD":
            label = _activity_label(put_symbol, activities)
            if label != MC.OBSERVED_ASSIGNMENT:
                track["put_evidence"] = MC.MOCK_EVIDENCE
                journal.set(MC.TRACK_KEY, track)
                journal_step(journal, "wheel_expiry_reconcile", None, MC.MOCK_EVIDENCE, "MOCK", put_symbol)
                print("RESULT: MOCK")
                print("wheel_lifecycle NOT recorded: evidence is MOCK.")
                return 0
            track["put_evidence"] = MC.OBSERVED_ASSIGNMENT
            journal.set(MC.TRACK_KEY, track)
            journal_step(journal, "wheel_expiry_assignment", None, MC.OBSERVED_ASSIGNMENT, "PASS", underlying, {
                "shares": equity_qty(reader.positions(), underlying),
            })

    if track.get("put_evidence") != MC.OBSERVED_ASSIGNMENT:
        print("RESULT: WAITING")
        print("wheel_lifecycle NOT recorded.")
        return 0

    cycle = _cycle(journal, track["cycle_id"])
    if cycle["state"] == "SHARES_HELD" and not track.get("call_symbol"):
        return _sell_covered_call(cfg, journal, om, ledger, reader, md, track, attempts, interval, sleep)

    if track.get("call_symbol"):
        return _reconcile_call(cfg, journal, om, ledger, reader, md, root, expected_account_id, track,
                               before, record, attempts, interval, sleep)
    print("RESULT: WAITING")
    print(f"Wheel state {cycle['state']}. wheel_lifecycle NOT recorded.")
    return 0


def _sell_covered_call(cfg, journal, om, ledger, reader, md, track, attempts, interval, sleep):
    underlying = track["underlying"]
    shares = equity_qty(reader.positions(), underlying)
    if shares < 100:
        print("RESULT: WAITING")
        print("Assignment is observed but 100 shares are not visible yet. Nothing was recorded.")
        return 0
    open_now, _clock = session_open(reader)
    if not open_now:
        print("RESULT: WAITING")
        print("The covered call waits for regular US market hours "
              f"({MC.NY_HOURS}; currently {MC.SYDNEY_HOURS}).")
        print("wheel_lifecycle NOT recorded: the covered call has not been sold.")
        return 0
    quote = _quote(md, underlying)
    try:
        ask = float(quote["ap"])
    except (KeyError, TypeError, ValueError):
        print("RESULT: WAITING")
        print("No quote for the covered call. wheel_lifecycle NOT recorded.")
        return 0
    today = _session_date(reader, journal)
    contracts, snaps = _listed(reader, md, underlying, today, "call", MC.SELL_BUYBACK_MAX_DTE)
    chosen = select_call(contracts, snaps, underlying=underlying, today=today, spot_ask=ask)
    if chosen is None:
        print("RESULT: WAITING")
        print("No covered call with a live bid. wheel_lifecycle NOT recorded.")
        return 0

    def check(db, sym=underlying):
        held = db.execute(
            "SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND kind='CALL_SHARES' "
            "AND symbol=?", (sym,)).fetchone()["a"]
        have = equity_qty(reader.positions(), sym)
        return (have - held >= 100, "share coverage: <100 free shares")

    attempt = int(track.get("call_attempt") or 0)
    limit = W.tick_round_down(chosen["bid"])
    body = W.option_order_body(chosen["symbol"], "sell", limit, "sell_to_open", MC.WHEEL_CONTRACTS)
    result = om.place(
        strategy="WHEEL", symbol=chosen["symbol"], purpose="SELL_CALL_OPEN",
        signal_key=f"{track['cycle_id']}:call:{attempt}", body=body,
        reservations=[("WHEEL", "CALL_SHARES", 100, underlying)], cash_check=check)
    if result.get("status") in ("BLOCKED", "rejected", "DRY_RUN"):
        track["call_attempt"] = attempt + 1
        journal.set(MC.TRACK_KEY, track)
        print("RESULT: WAITING")
        print(f"Covered call was not accepted ({result.get('status')}). wheel_lifecycle NOT recorded.")
        return 0
    if result.get("status") == "DUPLICATE":
        print("RESULT: WAITING")
        print("Covered call order already exists. wheel_lifecycle NOT recorded.")
        return 0
    cycle = _cycle(journal, track["cycle_id"])
    C.set_state(journal, cycle, "CALL_SUBMITTED", option_symbol=chosen["symbol"], strike=chosen["strike"],
                expiry=chosen["expiry"], pending_cid=result.get("client_order_id"))
    order = poll_order(reader, (result.get("order") or {}).get("id"), {"filled"} | _RESTING, attempts, interval, sleep)
    _sync_wheel(journal, om, ledger, reader)
    cycle = _cycle(journal, track["cycle_id"])
    reserved = om.reserved("WHEEL", kind="CALL_SHARES", symbol=underlying)
    track["call_symbol"] = chosen["symbol"]
    track["call_order_id"] = (order or {}).get("id")
    journal.set(MC.TRACK_KEY, track)
    evidence = "BROKER" if reserved >= 100 else MC.MOCK_EVIDENCE
    ok = reserved >= 100 and cycle["state"] in ("CALL_PENDING", "CALL_OPEN")
    journal_step(journal, "wheel_expiry_covered_call", order, evidence, "PASS" if ok else "FAIL",
                 chosen["symbol"], {"reserved_shares": reserved, "position_qty": shares})
    if not ok:
        print("RESULT: FAIL")
        print("100 shares were not reserved with the covered call. wheel_lifecycle NOT recorded.")
        _print_state(broker_snapshot(reader, [underlying, chosen["symbol"]]))
        return 2
    print("RESULT: WAITING")
    print(f"Covered call {chosen['symbol']} working. Reserved shares {reserved:g}. "
          "wheel_lifecycle NOT recorded until delivery or expiry is observed.")
    _print_state(broker_snapshot(reader, [underlying, chosen["symbol"]]))
    return 0


def _reconcile_call(cfg, journal, om, ledger, reader, md, root, expected_account_id, track, before,
                    record, attempts, interval, sleep):
    cycle = _cycle(journal, track["cycle_id"])
    activities = reader.activities() or []
    call = track["call_symbol"]
    label = _activity_label(call, activities)
    if cycle["state"] in ("CALL_PENDING",) or before == "CALL_PENDING":
        _sync_wheel(journal, om, ledger, reader)
        cycle = _cycle(journal, track["cycle_id"])
    if cycle["state"] in ("CALL_OPEN", "CALL_CLOSING", "CALL_PENDING"):
        journal_step(journal, "wheel_expiry_call_open", None, "BROKER", "WAITING", call, {"state": cycle["state"]})
        print("RESULT: WAITING")
        print(f"Covered call state {cycle['state']}. Delivery or expiry is not observed yet. "
              "wheel_lifecycle NOT recorded.")
        return 0
    unexplained = label == MC.MOCK_EVIDENCE and cycle["state"] != "CLOSED"
    if cycle["state"] == "CALL_ASSIGNMENT_PENDING" or unexplained:
        track["call_evidence"] = MC.MOCK_EVIDENCE
        journal.set(MC.TRACK_KEY, track)
        journal_step(journal, "wheel_expiry_call", None, MC.MOCK_EVIDENCE, "MOCK", call)
        print("RESULT: MOCK")
        print("wheel_lifecycle NOT recorded: evidence is MOCK.")
        return 0
    outcome = None
    if cycle["state"] == "CLOSED" and label == MC.OBSERVED_ASSIGNMENT:
        outcome = "OPASN"
        track["call_evidence"] = MC.OBSERVED_ASSIGNMENT
    elif cycle["state"] == "SHARES_HELD" and label == "OPEXP":
        outcome = "OPEXP"
        track["call_evidence"] = "OPEXP"
    if outcome is None or track.get("put_evidence") != MC.OBSERVED_ASSIGNMENT:
        track["call_evidence"] = MC.MOCK_EVIDENCE
        journal.set(MC.TRACK_KEY, track)
        journal_step(journal, "wheel_expiry_call", None, MC.MOCK_EVIDENCE, "MOCK", call, {"state": cycle["state"]})
        print("RESULT: MOCK")
        print("wheel_lifecycle NOT recorded: evidence is MOCK.")
        return 0
    track["call_outcome"] = outcome
    track["completed"] = True
    journal.set(MC.TRACK_KEY, track)
    evidence = (f"{MC.OBSERVED_ASSIGNMENT} run {track.get('started_run_id')} "
                f"put {track['put_symbol']} assigned; 100 shares reserved; "
                f"covered call {call} {outcome}")
    journal_step(journal, "wheel_expiry_complete", None, MC.OBSERVED_ASSIGNMENT, "PASS", call, {
        "call_outcome": outcome, "evidence_text": evidence,
    })
    print("RESULT: PASS")
    print(f"Evidence {MC.OBSERVED_ASSIGNMENT}. Call outcome {outcome}.")
    _print_state(broker_snapshot(reader, [track["underlying"], call, track["put_symbol"]]))
    if not record:
        _print_command("wheel", evidence)
        print("wheel_lifecycle NOT recorded: --record was not set.")
        return 0
    if not _record(root, "wheel", expected_account_id, cfg.config_hash, evidence, True):
        print("RESULT: FAIL")
        return 2
    return 0
