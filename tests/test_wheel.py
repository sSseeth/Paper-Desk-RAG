"""Wheel state sequence, invalid transition, delayed assignment, contract screen, management."""
import json, pytest
from conftest import make_calendar
from sakibdesk import wheel as W, cycle as C
from sakibdesk.orders import OrderManager
from sakibdesk.ledger import Ledger

P = json.load(open(__import__("conftest").ROOT + "/policy.json"))["wheel"]


def test_full_wheel_sequence():
    seq = ["PUT_SUBMITTED", "PUT_FILLED", "ASSIGNMENT_CONFIRMED", "CALL_SUBMITTED", "CALL_FILLED",
           "BUYBACK_SUBMITTED", "BUYBACK_FILLED", "CALL_SUBMITTED", "CALL_FILLED", "ASSIGNMENT_CONFIRMED"]
    s, path = "CASH", ["CASH"]
    for e in seq:
        s = W.transition(s, e); path.append(s)
    assert path == ["CASH", "PUT_PENDING", "PUT_OPEN", "SHARES_HELD", "CALL_PENDING", "CALL_OPEN", "CALL_CLOSING",
                    "SHARES_HELD", "CALL_PENDING", "CALL_OPEN", "CLOSED"]


@pytest.mark.parametrize("st,ev", [("CASH", "CALL_SUBMITTED"), ("PUT_OPEN", "CALL_SUBMITTED"),
                                   ("SHARES_HELD", "PUT_SUBMITTED"), ("CALL_OPEN", "UNWIND_SUBMITTED")])
def test_invalid_transitions_raise(st, ev):
    with pytest.raises(W.InvalidTransition):
        W.transition(st, ev)


def test_assignment_never_inferred_from_price():
    assert W.classify_option_outcome("MU261204P00150000", True, [], []) == "OPTION_POSITION_GONE_UNEXPLAINED"
    assert W.classify_option_outcome("X", True, [], [{"symbol": "X", "activity_type": "OPASN"}]) == "ASSIGNMENT_CONFIRMED"
    assert W.classify_option_outcome("X", True, [], [{"symbol": "X", "activity_type": "OPEXP"}]) == "EXPIRED_CONFIRMED"


def _cycle(journal, state, **d):
    data = {"option_symbol": "MU261204P00150000", "strike": 150.0, "expiry": "2026-12-04", "open_premium": 3.0}
    data.update(d)
    journal.db.execute("INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)", ("C1", "MU", state, json.dumps(data), "", ""))


def test_delayed_assignment_keeps_reserve_until_activity(cfg, journal, broker):
    led = Ledger(journal, cfg.policy)
    om = OrderManager(journal, cfg, writer=broker, ledger=led)
    _cycle(journal, "PUT_OPEN")
    journal.db.execute("INSERT INTO reservations VALUES('r','WHEEL','PUT_COLLATERAL','MU',15002,'cid','ACTIVE','','')")
    s = {"actions": []}
    C.reconcile_wheel(journal, om, led, [], [], s)                       # day 1: option gone, no activity
    assert C.cycles(journal)[0]["state"] == "ASSIGNMENT_PENDING" and om.reserved("WHEEL") == 15002
    C.reconcile_wheel(journal, om, led, [], [], s)                       # still nothing
    assert C.cycles(journal)[0]["state"] == "ASSIGNMENT_PENDING"
    acts = [{"symbol": "MU261204P00150000", "activity_type": "OPASN"}]
    pos = [{"symbol": "MU", "qty": "100", "asset_class": "us_equity"}]
    C.reconcile_wheel(journal, om, led, pos, acts, s)                    # day 2: activity arrives
    c = C.cycles(journal)[0]
    assert c["state"] == "SHARES_HELD" and c["data"]["acquisition_price"] == 150.0
    assert om.reserved("WHEEL") == 0 and led.cash("WHEEL") == 80000 - 15000
    C.reconcile_wheel(journal, om, led, pos, acts, s)                    # idempotent
    assert led.cash("WHEEL") == 65000


def test_screen_rules_and_ranking():
    cal = make_calendar("2026-09-01", "2027-01-31")
    def con(sym, exp, k, delta, bid, ask, oi=1000, **kw):
        c = {"symbol": sym, "expiration_date": exp, "strike_price": str(k), "type": "put", "multiplier": "100",
             "size": "100", "style": "american", "root_symbol": "MU", "underlying_symbol": "MU", "tradable": True,
             "open_interest": str(oi), "open_interest_date": "2026-11-09"}
        c.update(kw)
        return c, {"latestQuote": {"bp": bid, "ap": ask, "t": "2026-11-10T14:59:50Z"}, "greeks": {"delta": delta}}
    pairs = [con("A", "2026-12-11", 90, -0.21, 1.00, 1.05),        # weekly
             con("B", "2026-12-18", 90, -0.25, 1.00, 1.05),        # monthly (3rd Friday) -> preferred
             con("C", "2026-12-18", 90, -0.20, 1.00, 1.50),        # spread too wide
             con("D", "2026-12-18", 90, -0.20, 1.00, 1.05, oi=10), # OI
             con("E", "2026-12-18", 99, -0.20, 1.00, 1.05),        # above 95% mid ceiling
             con("F", "2026-12-18", 90, -0.20, 1.00, 1.05, multiplier="10")]
    contracts, snaps = [p[0] for p in pairs], {p[0]["symbol"]: p[1] for p in pairs}
    from datetime import datetime, timezone
    best, rej = W.screen_contracts("put", "MU", contracts, snaps, spot_mid=100, spot_bid=99.9, spot_ask=100.1, sma50=98,
                                   today="2026-11-10", now=datetime(2026, 11, 10, 15, 0, tzinfo=timezone.utc),
                                   calendar=cal, earnings_earliest="2027-01-20", latest_release="2026-09-20", p=P)
    assert best["symbol"] == "B"
    r = dict(rej)
    assert {"C", "D", "E", "F"} <= set(r)
    assert W.screen_contracts("put", "MU", contracts, snaps, spot_mid=100, spot_bid=99.9, spot_ask=100.1, sma50=98,
                              today="2026-11-10", now=None, calendar=cal, earnings_earliest="UNKNOWN",
                              latest_release="2026-09-20", p=P)[0] is None


def test_earnings_expiry_and_post_release_exclusion():
    cal = make_calendar("2026-09-01", "2027-01-31")
    kw = dict(spot_mid=100, spot_bid=99.9, spot_ask=100.1, sma50=98, now=None, calendar=cal, p=P)
    # expiry on/after earnings
    c = {"symbol": "B", "expiration_date": "2026-12-18", "strike_price": "90", "type": "put", "multiplier": "100",
         "style": "american", "root_symbol": "MU", "underlying_symbol": "MU", "tradable": True}
    best, rej = W.screen_contracts("put", "MU", [c], {}, today="2026-11-10", earnings_earliest="2026-12-17",
                                   latest_release="2026-09-20", **kw)
    assert best is None and dict(rej)["B"] == "expiry on/after next earnings"
    # within 2 sessions after release
    best, rej = W.screen_contracts("put", "MU", [c], {}, today="2026-10-28", earnings_earliest="2027-01-20",
                                   latest_release="2026-10-26", **kw)
    assert rej == [("*", "within 2 sessions after earnings release")]


def test_regular_monthly_holiday_thursday():
    assert W.is_regular_monthly("2026-12-18", set())
    assert not W.is_regular_monthly("2026-12-11", set())
    assert W.is_regular_monthly("2025-04-17", {"2025-04-16"})           # Fri 18 Apr 2025 Good Friday (3rd Fri)
    assert not W.is_regular_monthly("2025-04-17", {"2025-04-18"})


def test_collateral_and_ticks():
    assert W.put_collateral(150, P) == 15003.0  # strike*100 + $3 allowance (page 19 overlay; Skeptic/Sizer 2026-11-12)
    assert W.tick_round_down(2.37) == 2.35 and W.tick_round_up(3.41) == 3.5


def test_management_actions():
    cal = make_calendar("2026-09-01", "2027-01-31")
    base = dict(now_session_date="2026-11-27", calendar=cal, opt_bid=1.0, opt_ask=1.4, earnings_earliest="2027-01-20",
                dividend=None, distress=False, p=P)
    put = {"state": "PUT_OPEN", "data": {"strike": 150.0, "expiry": "2026-12-04", "open_premium": 3.0}}
    a = W.management_actions(put, stock_mid=119, **base)
    assert ("BUY_TO_CLOSE", "50% premium captured (ask <= 50% of opening premium)") in a
    assert ("BUY_TO_CLOSE", "stock 20% below put strike") in a
    assert ("NOTIFY", "7 days to expiry") in a
    call = {"state": "CALL_OPEN", "data": {"strike": 160.0, "expiry": "2026-12-18", "open_premium": 3.0,
                                           "acquisition_price": 150.0}}
    a = W.management_actions(call, stock_mid=119, **base)
    assert ("BUY_TO_CLOSE", "20% below acquisition: unwind") in a and not any(x[0] == "SELL_SHARES" for x in a)
    held = {"state": "SHARES_HELD", "data": {"acquisition_price": 150.0}}
    assert ("BLOCK_ADDITIONS", "10% below acquisition") in W.management_actions(held, stock_mid=134, **base)
    base["earnings_earliest"] = "UNKNOWN"
    assert ("FLAG", "earnings date unknown while short option open") in W.management_actions(put, stock_mid=149, **base)
