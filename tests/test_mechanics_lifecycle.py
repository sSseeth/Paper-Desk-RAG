"""Offline mechanics lifecycle suites, gate semantics, and series separation."""
import json
import os
import shutil
from datetime import date, datetime, timezone

import desk
from conftest import ROOT, ReaderView
from sakibdesk import cycle as C
from sakibdesk import lifecycle_gate as LG
from sakibdesk import mechanics_config as MC
from sakibdesk import mechanics_test as MT
from sakibdesk import reports as RP
from sakibdesk import stock as S
from sakibdesk import wheel as W
from sakibdesk.ledger import Ledger
from sakibdesk.orders import OrderManager
from sakibdesk.alpaca import BrokerError
from sakibdesk.config import Config
from sakibdesk.journal import Journal

WHEN = datetime(2026, 11, 16, 15, 0, tzinfo=timezone.utc)
EXP = "2026-11-20"


def _occ(root, exp, cp, strike):
    ymd = date.fromisoformat(exp).strftime("%y%m%d")
    return f"{root}{ymd}{cp}{int(round(strike * 1000)):08d}"


def _contract(root, exp, cp, strike):
    return {
        "symbol": _occ(root, exp, cp, strike),
        "expiration_date": exp,
        "strike_price": f"{strike:.1f}" if strike != int(strike) else str(int(strike)),
        "type": "put" if cp == "P" else "call",
        "multiplier": "100",
        "size": "100",
        "style": "american",
        "root_symbol": root,
        "underlying_symbol": root,
        "tradable": True,
    }


class MechFake:
    """Paper stand-in. Marketable stock limits fill; far limits rest; options fill."""

    def __init__(self, clock):
        self.clock_at = clock
        self.book = {}
        self.by_cid = {}
        self.n = 0
        self.positions_list = []
        self.activities_list = []
        self.contracts = []
        self.snapshots = {}
        self.quotes = {MC.STOCK_SYMBOL: {"bp": 11.0, "ap": 11.0, "t": "2026-11-16T15:00:00Z"}}
        self.cancels = []
        self.submits = []
        self.acct = {"id": "MECH-1", "account_number": "PA-MECH", "status": "ACTIVE",
                     "cash": "100000", "equity": "100000", "trading_blocked": False}
        self.is_open = True
        self.fill_marketable = True
        self.partial_probe = False
        self.fail_oco = False
        self.fail_sells = False
        self.confirm_cancels = True
        self.bars = {}

    def _working_sell(self, sym):
        open_status = {"new", "accepted", "pending_new", "partially_filled", "held"}
        for order in self.book.values():
            if order.get("symbol") == sym and order.get("side") == "sell" and order.get("status") in open_status:
                return True
        return False

    def account(self):
        return dict(self.acct)

    def clock(self):
        stamp = self.clock_at().astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        return {"timestamp": stamp, "is_open": self.is_open,
                "next_open": "2026-11-17T14:30:00Z", "next_close": "2026-11-16T21:00:00Z"}

    def positions(self):
        return [dict(p) for p in self.positions_list]

    def orders(self, status="open"):
        open_status = {"new", "accepted", "pending_new", "partially_filled", "held"}
        if status == "open":
            return [o for o in self.book.values() if o["status"] in open_status]
        return list(self.book.values())

    def order(self, oid):
        if oid in self.book:
            return self.book[oid]
        for order in self.book.values():
            for leg in order.get("legs") or []:
                if leg["id"] == oid:
                    return leg
        return None

    def order_by_client_id(self, cid):
        return self.by_cid.get(cid)

    def submit(self, body):
        self.submits.append(dict(body))
        if body["client_order_id"] in self.by_cid:
            raise BrokerError("duplicate client_order_id")
        if self.fail_oco and body.get("order_class") == "oco":
            raise BrokerError("oco rejected")
        self.n += 1
        oid = f"OID-{self.n}"
        limit = float(body.get("limit_price") or 0)
        qty = float(body["qty"])
        side = body["side"]
        sym = body["symbol"]
        status, filled, avg, legs = "accepted", "0", None, None
        if body.get("order_class") == "oco":
            # Alpaca OCO: parent is the take-profit limit; the only leg is the stop, held.
            status = "new"
            legs = [{
                "id": oid + "-sl", "type": "stop", "side": "sell", "qty": body["qty"],
                "status": "held", "filled_qty": "0", "symbol": sym, "order_class": "oco",
            }]
        elif body.get("order_class") == "bracket":
            # Bracket: parent is the entry. Both exits are legs, held until the entry fills.
            legs = [
                {"id": oid + "-tp", "type": "limit", "side": "sell", "qty": body["qty"],
                 "status": "held", "filled_qty": "0", "symbol": sym},
                {"id": oid + "-sl", "type": "stop", "side": "sell", "qty": body["qty"],
                 "status": "held", "filled_qty": "0", "symbol": sym},
            ]
        elif side == "sell" and len(sym) <= 6 and self._working_sell(sym):
            # Shares are held_for_orders while the OCO is accepted. The exit must cancel first.
            status = "rejected"
        elif len(sym) > 10 and self.fill_marketable:
            status, filled, avg = "filled", str(int(qty)), limit
            self._apply_fill(sym, side, qty)
        elif len(sym) <= 6 and not (self.fail_sells and side == "sell"):
            quote = self.quotes.get(sym) or {}
            bid = float(quote.get("bp") or 0)
            ask = float(quote.get("ap") or 0)
            if side == "buy" and ask and limit + 1e-9 >= ask:
                status, filled, avg = "filled", str(int(qty)), ask
                self._apply_fill(sym, side, qty)
            elif side == "sell" and bid and limit <= bid + 1e-9:
                status, filled, avg = "filled", str(int(qty)), bid
                self._apply_fill(sym, side, qty)
            elif (self.partial_probe and side == "buy" and qty >= 2 and bid and limit < bid * 0.75):
                status, filled, avg = "partially_filled", "1", limit
                self._apply_fill(sym, side, 1)
        order = {
            "id": oid, "client_order_id": body["client_order_id"], "status": status,
            "filled_qty": filled, "filled_avg_price": None if avg is None else str(avg),
            "side": side, "symbol": sym, "qty": body["qty"], "type": body.get("type"),
            "limit_price": body.get("limit_price"), "submitted_at": self.clock()["timestamp"],
            "asset_class": "us_option" if len(sym) > 10 else "us_equity",
            "legs": legs, "order_class": body.get("order_class"),
        }
        self.book[oid] = order
        self.by_cid[body["client_order_id"]] = order
        return dict(order)

    def _apply_fill(self, sym, side, qty):
        asset = "us_option" if len(sym) > 10 else "us_equity"
        current = next((p for p in self.positions_list if p["symbol"] == sym), None)
        if asset == "us_equity":
            have = float(current["qty"]) if current else 0.0
            new = have + (qty if side == "buy" else -qty)
            if current and abs(new) < 1e-9:
                self.positions_list.remove(current)
            elif current:
                current["qty"] = str(new)
            elif new > 0:
                self.positions_list.append({"symbol": sym, "qty": str(new), "side": "long", "asset_class": asset})
            return
        have = float(current["qty"]) if current else 0.0
        new = have + (qty if side == "sell" else -qty)
        if current and new <= 1e-9:
            self.positions_list.remove(current)
        elif current:
            current["qty"] = str(new)
            current["side"] = "short"
        elif new > 0:
            self.positions_list.append({"symbol": sym, "qty": str(new), "side": "short", "asset_class": asset})

    def cancel(self, oid):
        self.cancels.append(oid)
        if not self.confirm_cancels:
            return
        order = self.book.get(oid)
        if order and order["status"] != "filled":
            order["status"] = "canceled"
            for leg in order.get("legs") or []:
                if leg["status"] != "filled":
                    leg["status"] = "canceled"

    def latest_quotes(self, symbols):
        return {sym: self.quotes[sym] for sym in symbols if sym in self.quotes}

    def option_snapshots(self, symbols):
        return {sym: self.snapshots[sym] for sym in symbols if sym in self.snapshots}

    def option_contracts(self, underlying, start, end, kind, strike_lte=None, strike_gte=None):
        rows = [c for c in self.contracts
                if c["underlying_symbol"] == underlying and c["type"] == kind and start <= c["expiration_date"] <= end]
        if strike_lte is not None:
            rows = [c for c in rows if float(c["strike_price"]) <= float(strike_lte) + 1e-9]
        if strike_gte is not None:
            rows = [c for c in rows if float(c["strike_price"]) + 1e-9 >= float(strike_gte)]
        return rows

    def activities(self, *args, **kwargs):
        return list(self.activities_list)

    def daily_bars(self, symbols, start):
        return {sym: self.bars.get(sym, []) for sym in symbols}

    def asset(self, sym):
        return {"symbol": sym, "tradable": True}


def _clock():
    class Clock:
        def __call__(self):
            return WHEN
    return Clock()


def _journal(tmp_path, cfg, clock):
    path = tmp_path / "data" / "mechanics" / "journal.sqlite3"
    return Journal(str(path), cfg, clock=clock)


def _run_kw(fake, clock):
    return {"attempts": 3, "interval": 0, "sleep": lambda _s: None}


def _quote_for(sym, bid, ask):
    return {"latestQuote": {"bp": bid, "ap": ask, "t": "2026-11-16T15:00:00Z"}, "greeks": {"delta": -0.2}}


def _arm_wheel(fake):
    puts = [_contract("F", EXP, "P", strike) for strike in (8, 12)]
    calls = [_contract("F", EXP, "C", 12)]
    fake.contracts = puts + calls
    for contract in fake.contracts:
        fake.snapshots[contract["symbol"]] = _quote_for(contract["symbol"], 0.40, 0.50)


def test_config_hash_unchanged_and_caps_are_mechanics_only():
    assert Config(ROOT).config_hash == "2df03f83726ac139"
    assert MC.STOCK_SYMBOL == "F" and MC.WHEEL_UNDERLYING == "F"
    assert MC.STOCK_ENTRY_QTY == 1 and MC.STOCK_PARTIAL_QTY == 2
    assert MC.STOCK_MAX_NOTIONAL == 60
    assert MC.WHEEL_MAX_STRIKE == 30
    assert MC.FAR_OTM_FRACTION == 0.90
    # The indicative walk is mechanics-only. It is not part of the config hash.
    assert MC.SELL_PUT_FLOOR == 0.01
    assert MC.MIN_SELLABLE_BID == 0.05
    assert MC.BUYBACK_MAX_PRICE == 0.25
    assert MC.BUYBACK_EXTRA_OVER_SOLD == 0.25
    assert MC.BUYBACK_ABS_MAX == 1.00
    assert MC.PRICE_WALK_ATTEMPTS == 30
    assert MC.PRICE_WALK_INTERVAL_SEC == 1.0
    assert MC.PRICE_WALK_MAX_STEPS == 3
    walk_defaults = MT.run_wheel_sell_buyback.__kwdefaults__
    assert walk_defaults["attempts"] == MC.PRICE_WALK_ATTEMPTS
    assert walk_defaults["interval"] == MC.PRICE_WALK_INTERVAL_SEC
    text = open(os.path.join(ROOT, "policy.json"), encoding="utf-8").read()
    assert "MECH_" not in text and MC.STOCK_SYMBOL not in json.load(open(os.path.join(ROOT, "policy.json")))["wheel"]["universe"]


def test_select_put_fits_cash_and_distance():
    today = date(2026, 11, 16)
    puts = [_contract("F", EXP, "P", strike) for strike in (8, 12, 40)]
    snaps = {c["symbol"]: _quote_for(c["symbol"], 0.40, 0.50) for c in puts}
    far = MT.select_put(puts, snaps, underlying="F", today=today, spot_bid=11, spot_ask=11,
                        cash=100000, allowance=3, mode="far")
    assert far["strike"] == 8 and far["need"] == 803
    itm = MT.select_put(puts, snaps, underlying="F", today=today, spot_bid=11, spot_ask=11,
                        cash=100000, allowance=3, mode="itm")
    assert itm["strike"] == 12
    tight = MT.select_put(puts, snaps, underlying="F", today=today, spot_bid=11, spot_ask=11,
                          cash=500, allowance=3, mode="far")
    assert tight is None
    # 9 is at or below 90% of 11 and is sellable, so far wins over 10.5.
    # 10.5 is above 90% and still below the spot bid, so it stays in the
    # chain for the fallback and is counted.
    relaxed = [
        _contract("F", EXP, "P", 8),
        _contract("F", EXP, "P", 9),
        _contract("F", EXP, "P", 10.5),
    ]
    relaxed_snaps = {
        relaxed[0]["symbol"]: _quote_for("x", 0, 0.05),
        relaxed[1]["symbol"]: _quote_for("x", 0.20, 0.30),
        relaxed[2]["symbol"]: _quote_for("x", 0.40, 0.50),
    }
    nearer = MT.select_put(relaxed, relaxed_snaps, underlying="F", today=today, spot_bid=11, spot_ask=11,
                           cash=100000, allowance=3, mode="far")
    assert nearer["strike"] == 9 and nearer["rule"] == "far"
    _none, counts = MT._choose_put(relaxed, relaxed_snaps, underlying="F", today=today, spot_bid=11,
                                   spot_ask=11, cash=100000, allowance=3, mode="far")
    assert counts == {"listed": 3, "dte": 3, "strike": 3, "bid": 2, "sellable": 2}
    empty, empty_counts = MT._choose_put(
        [_contract("F", "2026-11-16", "P", 8), _contract("F", EXP, "P", 8), _contract("F", EXP, "P", 40)],
        {}, underlying="F", today=today, spot_bid=11, spot_ask=11, cash=100000, allowance=3, mode="far")
    assert empty is None and empty_counts == {"listed": 3, "dte": 2, "strike": 1, "bid": 0, "sellable": 0}
    call = MT.select_call(
        [_contract("F", EXP, "C", 12), _contract("F", EXP, "C", 11)],
        {_contract("F", EXP, "C", 12)["symbol"]: _quote_for("x", 0.3, 0.4),
         _contract("F", EXP, "C", 11)["symbol"]: _quote_for("x", 0.3, 0.4)},
        underlying="F", today=today, spot_ask=11)
    assert call["strike"] == 12


def test_stock_suite_pass_marks_partial_mock_and_can_record(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    main = Journal(str(tmp_path / "data" / "journal.sqlite3"), cfg, clock=clock)
    main.event("CYCLE_START", {"mode": "DRY_RUN"})
    before = [row["kind"] for row in main.events()]
    fake = MechFake(clock)
    code = MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 0
    assert "RESULT: PASS" in out
    assert "partial_fill_protection" in out and "MOCK" in out
    assert "py desk.py lifecycle-record --account mechanics --check stock --evidence" in out
    assert journal.run_id in out
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "stock_lifecycle" in gate
    assert journal.run_id in gate["stock_lifecycle"]["evidence"]
    assert "MOCK" in gate["stock_lifecycle"]["evidence"]
    assert "MECH-1" not in open(LG.gate_path(str(tmp_path)), encoding="utf-8").read()
    steps = list(journal.events("MECHANICS_STEP"))
    assert {json.loads(row["payload"])["step"] for row in steps} >= {
        "limit_cancel", "partial_fill_protection", "marketable_buy",
        "protect_stop_target", "protect_cancel_replace", "strategy_exit"}
    limit = json.loads(next(row["payload"] for row in steps
                             if json.loads(row["payload"])["step"] == "limit_cancel"))
    assert limit["broker_order_id"] and limit["observed_status"] == "canceled"
    assert limit["observed_at_utc"] and limit["submitted_at"]
    assert fake.acct["id"] not in json.dumps(limit)
    assert [row["kind"] for row in main.events()] == before
    assert journal.db.execute("SELECT COUNT(*) n FROM equity_marks").fetchone()["n"] == 0
    assert main.db.execute("SELECT COUNT(*) n FROM equity_marks").fetchone()["n"] == 0
    assert "spy_benchmarks" not in open(MT.__file__, encoding="utf-8").read()
    ocos = [order for order in fake.book.values() if order.get("order_class") == "oco"]
    assert len(ocos) == 2
    for order in ocos:
        assert order["type"] == "limit" and order["side"] == "sell"
        assert [leg["type"] for leg in order["legs"]] == ["stop"]
        assert order["legs"][0]["side"] == "sell"


def test_legs_live_accepts_alpaca_oco_parent_and_bracket_legs():
    """Fake broker returns Alpaca's OCO shape and a bracket shape."""
    fake = MechFake(_clock())
    oco_body = C.protection_oco_body("F", 1, {"limit_price": "12.00"}, {"stop_price": "10.00"})
    oco_body["client_order_id"] = "oco-shape"
    parent = fake.submit(oco_body)
    stored = fake.order(parent["id"])
    assert stored["status"] == "new" and stored["type"] == "limit" and stored["side"] == "sell"
    assert stored["order_class"] == "oco"
    assert [leg["type"] for leg in stored["legs"]] == ["stop"]
    assert stored["legs"][0]["status"] == "held"
    assert MT._legs_live(stored)
    # The old check looked only inside legs, so this live OCO would fail.
    assert MT._legs_live({"id": "x", "type": "limit", "side": "buy", "status": "filled",
                          "legs": stored["legs"]}) is False
    bracket_body, _stop, _target = S.bracket_body("F", 1, 11.0, 0.5, {"target_r_mult": 3})
    bracket_body["client_order_id"] = "bracket-shape"
    bracket = fake.order(fake.submit(bracket_body)["id"])
    assert bracket["order_class"] == "bracket" and bracket["side"] == "buy"
    assert {leg["type"] for leg in bracket["legs"]} == {"limit", "stop"}
    assert all(leg["status"] == "held" and leg["side"] == "sell" for leg in bracket["legs"])
    assert MT._legs_live(bracket)
    assert MT._legs_live({"legs": [bracket["legs"][0]]}) is False


def test_strategy_exit_does_not_sell_until_oco_cancel_is_confirmed(tmp_path):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    fake.confirm_cancels = False
    fake.positions_list = [{"symbol": "F", "qty": "1", "side": "long", "asset_class": "us_equity"}]
    body = C.protection_oco_body("F", 1, {"limit_price": "12.00"}, {"stop_price": "10.00"})
    body["client_order_id"] = "oco-live"
    parent = fake.submit(body)
    om = OrderManager(journal, cfg, writer=fake, ledger=Ledger(journal, cfg.policy))
    rows = []
    MT._step_exit(cfg, journal, om, fake, fake, [{"id": parent["id"], "cid": body["client_order_id"]}],
                  rows, "F", 1, 0, lambda _s: None)
    assert rows[-1]["result"] == "FAIL"
    assert "held_for_orders" in rows[-1]["note"]
    assert [item for item in fake.submits if item.get("position_intent") == "sell_to_close"] == []
    assert fake.order(parent["id"])["status"] == "new"


def test_stock_partial_fill_is_broker_evidence_when_the_broker_partials(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    fake.partial_probe = True
    code = MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=False, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 0, out
    partial = next(json.loads(row["payload"]) for row in journal.events("MECHANICS_STEP")
                   if json.loads(row["payload"])["step"] == "partial_fill_protection")
    assert partial["evidence"] == "BROKER"
    assert partial["observed_status"] == "partially_filled"
    assert partial["duplicate_blocked"] is True
    cids = [body["client_order_id"] for body in fake.submits if body.get("qty") == "2"]
    assert len(cids) == 1


def test_stock_refuses_when_closed_or_position_exists(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    fake.is_open = False
    assert MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        **_run_kw(fake, clock)) == 2
    out = capsys.readouterr().out
    assert "regular US market hours" in out and "Sydney" in out
    assert fake.submits == []
    fake.is_open = True
    fake.positions_list = [{"symbol": "F", "qty": "1", "side": "long", "asset_class": "us_equity"}]
    assert MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        **_run_kw(fake, clock)) == 2
    assert fake.submits == []


def test_stock_cleanup_flattens_when_protection_is_rejected(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    fake.fail_oco = True
    code = MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 2
    assert "RESULT: FAIL" in out
    assert "protect_stop_target" in out
    state_line = out.split("final broker state:", 1)[1]
    state = json.loads(state_line.split("RESULT:", 1)[0])
    assert state["equity_qty"]["F"] == 0
    assert state["open_orders"] == []


def test_stock_reports_open_position_when_sells_will_not_fill(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    fake.fail_sells = True
    code = MT.run_stock_suite(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 2 and "RESULT: FAIL" in out
    state = json.loads(out.split("final broker state:", 1)[1].split("RESULT:", 1)[0])
    assert state["equity_qty"]["F"] == 1
    assert state["open_orders"] == []


def test_wheel_sell_buyback_records_on_pass(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 0, out
    assert "RESULT: PASS" in out
    occ = _occ("F", EXP, "P", 8)
    assert f"chosen far put {occ} strike 8.00 bid 0.40 (72.7% of spot 11.00)" in out
    assert option_flat(fake, occ)
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "wheel_sell_buyback" in gate
    assert journal.run_id in gate["wheel_sell_buyback"]["evidence"]
    assert "wheel_lifecycle" not in gate


def test_wheel_sell_buyback_refusal_prints_filter_counts(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    for snap in fake.snapshots.values():
        snap["latestQuote"]["bp"] = 0
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 2
    assert "no far-OTM weekly put" in out
    assert "contracts listed 1, after DTE filter 1, after strike filter 1, with positive bid 0" in out
    assert "sellable bid (>= 1 tick after rounding) 0" in out
    assert fake.submits == []


def _puts(exp, quotes):
    """Put contracts and snapshots. ``quotes`` maps strike to (bid, ask)."""
    contracts = [_contract("F", exp, "P", strike) for strike in quotes]
    snaps = {}
    for contract in contracts:
        strike = float(contract["strike_price"])
        bid, ask = quotes[strike]
        snaps[contract["symbol"]] = _quote_for(contract["symbol"], bid, ask)
    return contracts, snaps


def _choose(contracts, snaps, *, spot=11, cash=100000, mode="far", today=None):
    today = today or date(2026, 11, 16)
    return MT._choose_put(
        contracts, snaps, underlying="F", today=today, spot_bid=spot, spot_ask=spot,
        cash=cash, allowance=3, mode=mode)


def test_subnickel_far_bid_falls_back_to_the_lowest_nearer_otm():
    quotes = {8: (0.04, 0.10), 9: (0.049, 0.10), 10: (0.20, 0.30), 10.5: (0.60, 0.70)}
    contracts, snaps = _puts(EXP, quotes)
    chosen, counts = _choose(contracts, snaps)
    assert chosen["strike"] == 10 and chosen["rule"] == "fallback"
    assert chosen["bid"] == 0.20
    assert counts["bid"] == 4 and counts["sellable"] == 2
    assert MT._put_choice_line(chosen, 11) == (
        f"chosen fallback put {chosen['symbol']} strike 10.00 bid 0.20 (90.9% of spot 11.00)")
    # A bid that rounds down to one nickel is sellable, so the far strike wins.
    quotes[9] = (0.09, 0.15)
    contracts, snaps = _puts(EXP, quotes)
    chosen, _counts = _choose(contracts, snaps)
    assert chosen["strike"] == 9 and chosen["rule"] == "far"


def test_sellable_far_strike_is_preferred_over_a_richer_nearer_put():
    quotes = {8: (0.10, 0.20), 9: (0.15, 0.25), 10: (0.80, 0.90)}
    chosen, counts = _choose(*_puts(EXP, quotes))
    assert chosen["strike"] == 9 and chosen["rule"] == "far"
    assert chosen["bid"] == 0.15
    assert counts["sellable"] == 3
    assert "81.8% of spot" in MT._put_choice_line(chosen, 11)


def test_nothing_sellable_refuses_and_separates_positive_from_sellable():
    quotes = {8: (0.04, 0.10), 9: (0.02, 0.10), 10: (0.03, 0.10)}
    chosen, counts = _choose(*_puts(EXP, quotes))
    assert chosen is None
    assert counts == {"listed": 3, "dte": 3, "strike": 3, "bid": 3, "sellable": 0}
    line = MT._filter_summary(counts)
    assert "with positive bid 3, sellable bid (>= 1 tick after rounding) 0" in line


def test_far_mode_never_chooses_a_strike_at_or_above_spot():
    quotes = {8: (0.01, 0.05), 11: (1.00, 1.10), 12: (1.20, 1.30)}
    chosen, counts = _choose(*_puts(EXP, quotes))
    assert chosen is None
    assert counts["strike"] == 1 and counts["bid"] == 1 and counts["sellable"] == 0
    quotes = {10.5: (0.40, 0.50), 11: (1.00, 1.10)}
    chosen, counts = _choose(*_puts(EXP, quotes))
    assert chosen["strike"] == 10.5 and chosen["rule"] == "fallback"
    assert chosen["strike"] < 11
    assert counts["strike"] == 1 and counts["sellable"] == 1


def test_far_mode_uses_the_next_expiry_only_when_the_near_one_has_no_sellable_bid():
    today = date(2026, 11, 16)
    near, later, too_far = "2026-11-20", "2026-11-27", "2026-12-18"
    quotes = {
        (near, 9): (0.04, 0.10),
        (later, 8): (0.25, 0.35),
        (too_far, 8): (0.90, 1.00),
    }
    contracts, snaps = _puts_by_expiry(quotes)
    chosen, counts = _choose(contracts, snaps, today=today)
    assert chosen["expiry"] == later and chosen["strike"] == 8 and chosen["rule"] == "far"
    assert counts["dte"] == 2 and counts["sellable"] == 1
    # A sellable fallback on the near expiry stops the search.
    quotes[(near, 10)] = (0.30, 0.40)
    contracts, snaps = _puts_by_expiry(quotes)
    chosen, _counts = _choose(contracts, snaps, today=today)
    assert chosen["expiry"] == near and chosen["strike"] == 10 and chosen["rule"] == "fallback"


def test_fallback_still_respects_cash_and_the_strike_cap():
    quotes = {8: (0.04, 0.10), 10: (0.40, 0.50), 40: (1.00, 1.10)}
    chosen, counts = _choose(*_puts(EXP, quotes), cash=900)
    assert chosen is None
    assert counts["strike"] == 1 and counts["sellable"] == 0
    quotes[8] = (0.20, 0.30)
    chosen, counts = _choose(*_puts(EXP, quotes), cash=1000)
    assert chosen["strike"] == 8 and chosen["rule"] == "far"
    assert counts["sellable"] == 1


def _puts_by_expiry(quotes):
    """``quotes`` maps (expiry, strike) to (bid, ask)."""
    contracts = []
    snaps = {}
    for (exp, strike), (bid, ask) in quotes.items():
        contract = _contract("F", exp, "P", strike)
        contracts.append(contract)
        snaps[contract["symbol"]] = _quote_for(contract["symbol"], bid, ask)
    return contracts, snaps


class WalkBroker(MechFake):
    """Options rest until the limit reaches the price this broker will trade.

    A sell fills at or below ``sell_fill_at``. A buy fills at or above
    ``buy_fill_at``. ``None`` never fills that side. A cancel can fill the
    resting sell first, which is the race a replace must not double.
    """

    def __init__(self, clock):
        super().__init__(clock)
        self.sell_fill_at = None
        self.buy_fill_at = None
        self.race_sell_cancel = False
        self.race_raises = False
        self._raced = False
        self.journal = None
        self.collateral_at_submit = []
        self.working_at_submit = []
        self.max_option_qty = 0.0

    def _should_fill(self, body):
        symbol = body.get("symbol") or ""
        if len(symbol) <= 10:
            return True
        try:
            limit = float(body.get("limit_price") or 0)
        except (TypeError, ValueError):
            return False
        side = body.get("side")
        if side == "sell":
            return self.sell_fill_at is not None and limit <= self.sell_fill_at + 1e-9
        if side == "buy":
            return self.buy_fill_at is not None and limit + 1e-9 >= self.buy_fill_at
        return False

    def _note(self, body):
        symbol = body.get("symbol")
        side = body.get("side")
        working = 0
        for order in self.orders("open"):
            if order.get("symbol") == symbol and order.get("side") == side:
                working += 1
        self.working_at_submit.append(working)
        if self.journal is None or body.get("position_intent") != "sell_to_open":
            return
        row = self.journal.db.execute(
            "SELECT COUNT(*) n FROM reservations WHERE status='ACTIVE' AND kind='PUT_COLLATERAL'"
        ).fetchone()
        self.collateral_at_submit.append(row["n"])

    def submit(self, body):
        self._note(body)
        self.fill_marketable = self._should_fill(body)
        order = super().submit(body)
        if len(body.get("symbol") or "") > 10 and order.get("status") != "filled":
            stored = self.book[order["id"]]
            stored["status"] = "new"
            return dict(stored)
        return order

    def _apply_fill(self, sym, side, qty):
        super()._apply_fill(sym, side, qty)
        if len(sym) > 10:
            self.max_option_qty = max(self.max_option_qty, MT.option_qty(self.positions(), sym))

    def cancel(self, oid):
        order = self.book.get(oid)
        resting = {"new", "accepted", "pending_new", "partially_filled", "held"}
        if (self.race_sell_cancel and not self._raced and order and order.get("side") == "sell"
                and order.get("status") in resting):
            self._raced = True
            order["status"] = "filled"
            order["filled_qty"] = order["qty"]
            order["filled_avg_price"] = order.get("limit_price")
            self._apply_fill(order["symbol"], "sell", float(order["qty"]))
            self.cancels.append(oid)
            if self.race_raises:
                raise BrokerError("cancel raced a fill")
            return
        super().cancel(oid)


def _run_walk(tmp_path, fake, clock, sleep):
    cfg = Config(ROOT)
    journal = _journal(tmp_path, cfg, clock)
    fake.journal = journal
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, attempts=1, interval=0, sleep=sleep)
    return journal, code


def _limits(fake, intent):
    return [body["limit_price"] for body in fake.submits if body.get("position_intent") == intent]


def _active_collateral(journal):
    return journal.db.execute(
        "SELECT COUNT(*) n FROM reservations WHERE status='ACTIVE' AND kind='PUT_COLLATERAL'"
    ).fetchone()["n"]


def _mech_step(journal, step):
    for row in journal.events("MECHANICS_STEP"):
        payload = json.loads(row["payload"])
        if payload.get("step") == step:
            return payload
    return None


def test_price_walk_bounds_and_config_hash():
    assert Config(ROOT).config_hash == "2df03f83726ac139"
    assert MT._sell_price_walk(0.40) == [0.40, 0.20, 0.01]
    assert MT._sell_price_walk(0.05) == [0.05, 0.01]
    assert MT._buy_price_walk(0.05) == [0.05, 0.10, 0.25]
    assert MT._buy_price_walk(0.20) == [0.20, 0.25]
    assert MT._buy_price_walk(0.50) == [0.25]
    assert MT._buy_price_walk(1.0) == [0.25]
    for ask in (0.01, 0.04, 0.05, 0.11, 0.2, 0.25, 0.3, 0.5, 2.0):
        steps = MT._buy_price_walk(ask)
        assert 1 <= len(steps) <= MC.PRICE_WALK_MAX_STEPS
        assert steps[-1] <= MC.BUYBACK_MAX_PRICE
        assert max(steps) <= MC.BUYBACK_MAX_PRICE
    for bid in (0.02, 0.05, 0.11, 0.4, 1.0):
        steps = MT._sell_price_walk(bid)
        assert steps
        assert steps[-1] == MC.SELL_PUT_FLOOR
        assert len(steps) <= MC.PRICE_WALK_MAX_STEPS
        assert steps == sorted(steps, reverse=True)


def test_sell_walk_unfilled_at_bid_fills_at_floor(tmp_path, capsys):
    clock = _clock()
    fake = WalkBroker(clock)
    _arm_wheel(fake)
    fake.sell_fill_at = MC.SELL_PUT_FLOOR
    fake.buy_fill_at = 0.0
    slept = []
    journal, code = _run_walk(tmp_path, fake, clock, slept.append)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "RESULT: PASS" in out
    assert "price walk sell_put: 0.40 -> 0.20 -> 0.01" in out
    assert _limits(fake, "sell_to_open") == ["0.40", "0.20", "0.01"]
    assert fake.collateral_at_submit == [1, 1, 1]
    assert fake.working_at_submit == [0, 0, 0, 0]
    assert fake.max_option_qty == 1
    assert option_flat(fake, _occ("F", EXP, "P", 8))
    assert _active_collateral(journal) == 0
    assert slept == [MC.PRICE_WALK_INTERVAL_SEC] * ((MC.PRICE_WALK_ATTEMPTS - 1) * 2)
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "wheel_sell_buyback" in gate
    assert gate["wheel_sell_buyback"]["config_hash"] == "2df03f83726ac139"


def test_sell_walk_never_fills_cancels_and_stays_flat(tmp_path, capsys):
    clock = _clock()
    fake = WalkBroker(clock)
    _arm_wheel(fake)
    slept = []
    journal, code = _run_walk(tmp_path, fake, clock, slept.append)
    out = capsys.readouterr().out
    assert code == 2
    assert "RESULT: FAIL" in out
    assert "price walk sell_put: 0.40 -> 0.20 -> 0.01" in out
    assert "limit=0.01" in out and "status=canceled" in out and "reprices=2" in out
    step = _mech_step(journal, "sell_put")
    assert step["result"] == "FAIL"
    assert step["evidence"] == "BROKER limit=0.01 status=canceled reprices=2"
    assert step["last_limit"] == "0.01"
    assert step["broker_status"] == "canceled"
    assert step["reprices"] == 2
    assert step["price_walk"] == ["0.40", "0.20", "0.01"]
    assert _limits(fake, "sell_to_open") == ["0.40", "0.20", "0.01"]
    assert _limits(fake, "buy_to_close") == []
    assert fake.collateral_at_submit == [1, 1, 1]
    assert all(n == 0 for n in fake.working_at_submit)
    sells = [order for order in fake.book.values() if order.get("side") == "sell"]
    assert sells and all(order["status"] == "canceled" for order in sells)
    assert fake.positions_list == []
    assert fake.max_option_qty == 0
    state = json.loads(out.split("final broker state:", 1)[1].split("RESULT:", 1)[0])
    assert state["open_orders"] == []
    assert state["positions"] == []
    assert _active_collateral(journal) == 0
    assert not os.path.exists(LG.gate_path(str(tmp_path)))
    assert slept == [MC.PRICE_WALK_INTERVAL_SEC] * ((MC.PRICE_WALK_ATTEMPTS - 1) * 3)


def test_sell_cancel_racing_a_fill_does_not_double_sell(tmp_path, capsys):
    clock = _clock()
    fake = WalkBroker(clock)
    _arm_wheel(fake)
    fake.race_sell_cancel = True
    fake.race_raises = True
    fake.buy_fill_at = 0.0
    journal, code = _run_walk(tmp_path, fake, clock, lambda _s: None)
    out = capsys.readouterr().out
    assert code == 0, out
    assert _limits(fake, "sell_to_open") == ["0.40"]
    assert len({body["client_order_id"] for body in fake.submits
                if body.get("position_intent") == "sell_to_open"}) == 1
    assert fake.collateral_at_submit == [1]
    assert all(n == 0 for n in fake.working_at_submit)
    assert fake.max_option_qty == 1
    assert option_flat(fake, _occ("F", EXP, "P", 8))
    assert _active_collateral(journal) == 0
    assert "0.40 -> 0.20" not in out
    assert "price walk sell_put: 0.40" in out


def test_buyback_walk_stops_at_cap(tmp_path, capsys):
    clock = _clock()
    fake = WalkBroker(clock)
    _arm_wheel(fake)
    occ = _occ("F", EXP, "P", 8)
    fake.snapshots[occ]["latestQuote"]["bp"] = 0.40
    fake.snapshots[occ]["latestQuote"]["ap"] = 0.05
    fake.sell_fill_at = 100.0
    slept = []
    journal, code = _run_walk(tmp_path, fake, clock, slept.append)
    out = capsys.readouterr().out
    assert code == 2, out
    assert "RESULT: FAIL" in out
    # The sell filled at 0.40, so the cap is 0.40 + 0.25, still under the absolute max.
    cap = MT._buyback_cap(0.40)
    assert cap == 0.65
    ladder = f"0.05 -> 0.10 -> {cap:.2f}"
    assert f"price walk buyback: {ladder}" in out
    assert f"price walk buy_to_close: {ladder}" in out
    buys = [float(px) for px in _limits(fake, "buy_to_close")]
    assert buys == [0.05, 0.10, cap, 0.05, 0.10, cap]
    assert max(buys) == cap
    assert max(buys) <= MC.BUYBACK_ABS_MAX
    step = _mech_step(journal, "buyback")
    assert step["result"] == "FAIL"
    assert step["evidence"] == f"BROKER limit={cap:.2f} status=canceled reprices=2"
    assert "SHORT PUT STILL OPEN" in out
    assert occ in out
    assert all(n == 0 for n in fake.working_at_submit)
    assert fake.max_option_qty == 1
    assert MT.option_qty(fake.positions(), occ) == 1
    assert _active_collateral(journal) == 1
    state = json.loads(out.split("final broker state:", 1)[1].split("SHORT PUT", 1)[0])
    assert state["open_orders"] == []
    assert any(pos.get("symbol") == occ and pos.get("side") == "short" for pos in state["positions"])
    assert not os.path.exists(LG.gate_path(str(tmp_path)))
    assert slept == [MC.PRICE_WALK_INTERVAL_SEC] * ((MC.PRICE_WALK_ATTEMPTS - 1) * 6)


class ActivityFailureBroker(WalkBroker):
    """activities() answers 422. The first cancel leaves its order working."""

    def __init__(self, clock):
        super().__init__(clock)
        self.events = []
        self._cancel_n = 0

    def activities(self, *args, **kwargs):
        self.events.append("activities")
        raise BrokerError(
            'GET /v2/account/activities: HTTP 422 '
            '{"code":40010001,"message":"invalid activity type: OPXRC"}')

    def cancel(self, oid):
        self.events.append(("cancel", oid))
        self._cancel_n += 1
        if self._cancel_n == 1:
            raise BrokerError("cancel raced")
        super().cancel(oid)


def test_activities_422_during_cleanup_still_cancels_and_reports(tmp_path, capsys):
    clock = _clock()
    fake = ActivityFailureBroker(clock)
    _arm_wheel(fake)
    fake.sell_fill_at = 100.0
    fake.buy_fill_at = None
    _journal, code = _run_walk(tmp_path, fake, clock, lambda _s: None)
    captured = capsys.readouterr()
    out, err = captured.out, captured.err
    assert code == 2, out
    assert "Traceback" not in out and "Traceback" not in err
    assert "final broker state:" in out
    assert "RESULT: FAIL" in out
    assert "SHORT PUT STILL OPEN" in out
    last_act = max(i for i, event in enumerate(fake.events) if event == "activities")
    assert any(isinstance(event, tuple) and event[0] == "cancel" for event in fake.events[last_act + 1:])
    state = json.loads(out.split("final broker state:", 1)[1].split("SHORT PUT", 1)[0])
    assert state["open_orders"] == []
    occ = _occ("F", EXP, "P", 8)
    assert any(pos.get("symbol") == occ and pos.get("side") == "short" for pos in state["positions"])
    assert not os.path.exists(LG.gate_path(str(tmp_path)))


def test_leftover_short_put_recovers_without_a_new_sell(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = WalkBroker(clock)
    _arm_wheel(fake)
    occ = _occ("F", EXP, "P", 11)
    fake.positions_list.append({
        "symbol": occ, "qty": "1", "side": "short", "asset_class": "us_option",
    })
    fake.snapshots[occ] = _quote_for(occ, 0.10, 0.40)
    fake.buy_fill_at = 0.40
    stale = {
        "id": "STALE-1",
        "client_order_id": "stale-buy",
        "status": "new",
        "symbol": occ,
        "side": "buy",
        "qty": "1",
        "type": "limit",
        "limit_price": "0.15",
        "filled_qty": "0",
        "filled_avg_price": None,
        "submitted_at": "2026-11-16T15:00:00Z",
        "asset_class": "us_option",
    }
    fake.book[stale["id"]] = stale
    fake.by_cid[stale["client_order_id"]] = stale
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, attempts=1, interval=0, sleep=lambda _s: None)
    out = capsys.readouterr().out
    assert code == 2, out
    assert "A new put was not opened." in out
    assert "cancel-confirmed" in out
    assert "cap 1.00" in out
    assert "Nothing was recorded." in out
    assert "Re-run wheel-sell-buyback after this account is flat." in out
    assert "final broker state:" in out
    assert "RESULT: FAIL" in out
    assert "chosen far put" not in out
    sells = [body for body in fake.submits if body.get("position_intent") == "sell_to_open"]
    buys = [body for body in fake.submits if body.get("position_intent") == "buy_to_close"]
    assert sells == []
    assert buys and float(buys[0]["limit_price"]) == 0.40
    assert all(float(body["limit_price"]) <= MC.BUYBACK_ABS_MAX + 1e-9 for body in buys)
    assert fake.cancels[0] == "STALE-1"
    assert fake.order("STALE-1")["status"] == "canceled"
    assert option_flat(fake, occ)
    assert not os.path.exists(LG.gate_path(str(tmp_path)))
    assert journal.db.execute("SELECT COUNT(*) n FROM wheel_cycles").fetchone()["n"] == 0
    assert list(journal.events("MECHANICS_STEP")) == []
    assert Config(ROOT).config_hash == "2df03f83726ac139"


def test_buyback_cap_math_and_config_hash():
    assert Config(ROOT).config_hash == "2df03f83726ac139"
    assert MT._buyback_cap(None) == MC.BUYBACK_MAX_PRICE
    assert MT._buyback_cap(0) == MC.BUYBACK_MAX_PRICE
    assert MT._buyback_cap(-1) == MC.BUYBACK_MAX_PRICE
    assert MT._buyback_cap("nope") == MC.BUYBACK_MAX_PRICE
    assert MT._buyback_cap(0.01) == 0.26
    assert MT._buyback_cap(0.40) == 0.65
    assert MT._buyback_cap(0.55) == 0.80
    assert MT._buyback_cap(0.75) == 1.00
    assert MT._buyback_cap(0.90) == MC.BUYBACK_ABS_MAX
    assert MT._buy_price_walk(0.05) == [0.05, 0.10, 0.25]
    assert MT._buy_price_walk(0.05, 0.40) == [0.05, 0.10, 0.65]
    assert MT._buy_price_walk(0.05, 0.55) == [0.05, 0.10, 0.80]
    assert MT._buy_price_walk(0.05, 0.90) == [0.05, 0.10, 1.00]
    assert MT._buy_price_walk(0.90, 0.40) == [0.65]
    assert MT._clamp_walk_price("buy", 0.80, 0.65) == 0.65
    assert MT._clamp_walk_price("buy", 0.10, 0.65) == 0.10
    assert MT._clamp_walk_price("buy", 1.50, None) == MC.BUYBACK_MAX_PRICE
    for sold in (None, 0, 0.01, 0.05, 0.20, 0.40, 0.55, 0.75, 0.90, 2):
        cap = MT._buyback_cap(sold)
        assert MC.BUYBACK_MAX_PRICE - 1e-9 <= cap <= MC.BUYBACK_ABS_MAX + 1e-9
        for ask in (0.01, 0.05, 0.20, 0.50, 0.90, 1.50):
            steps = MT._buy_price_walk(ask, sold)
            assert 1 <= len(steps) <= MC.PRICE_WALK_MAX_STEPS
            assert steps == sorted(steps)
            assert max(steps) <= cap + 1e-9
    # One contract bought at the absolute cap, after a floor-priced sell, is under $100.
    loss = (MC.BUYBACK_ABS_MAX - MC.SELL_PUT_FLOOR) * 100 * MC.WHEEL_CONTRACTS
    assert loss < 100


class ReportedAvgBroker(WalkBroker):
    """Opening sell reports this filled average, which is not the limit."""

    def __init__(self, clock, avg):
        super().__init__(clock)
        self.reported_avg = f"{float(avg):.2f}"

    def submit(self, body):
        order = super().submit(body)
        stored = self.book.get(order["id"])
        if (stored and stored.get("side") == "sell" and stored.get("status") == "filled"
                and len(stored.get("symbol") or "") > 10):
            stored["filled_avg_price"] = self.reported_avg
            return dict(stored)
        return order


def test_buyback_cap_uses_the_broker_filled_average(tmp_path, capsys):
    clock = _clock()
    fake = ReportedAvgBroker(clock, 0.55)
    _arm_wheel(fake)
    occ = _occ("F", EXP, "P", 8)
    fake.snapshots[occ]["latestQuote"]["bp"] = 0.40
    fake.snapshots[occ]["latestQuote"]["ap"] = 0.05
    fake.sell_fill_at = 100.0
    _journal, code = _run_walk(tmp_path, fake, clock, lambda _s: None)
    out = capsys.readouterr().out
    assert code == 2, out
    # 0.55 + 0.25 = 0.80. The 0.40 limit would have capped the walk at 0.65.
    cap = MT._buyback_cap(0.55)
    assert cap == 0.80
    ladder = f"0.05 -> 0.10 -> {cap:.2f}"
    assert f"price walk buyback: {ladder}" in out
    assert f"price walk buy_to_close: {ladder}" in out
    buys = [float(px) for px in _limits(fake, "buy_to_close")]
    assert buys == [0.05, 0.10, cap, 0.05, 0.10, cap]
    assert max(buys) <= MC.BUYBACK_ABS_MAX
    assert all(n == 0 for n in fake.working_at_submit)
    assert fake.max_option_qty == 1


def test_suite_falls_back_and_lists_every_otm_strike(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    quotes = {8: (0.04, 0.10), 9: (0.03, 0.10), 10: (0.30, 0.40), 10.5: (0.70, 0.80), 11: (1.00, 1.10)}
    fake.contracts, fake.snapshots = _puts(EXP, quotes)
    seen = []
    orig = fake.option_contracts

    def wrapped(underlying, start, end, kind, strike_lte=None, strike_gte=None):
        seen.append(strike_lte)
        return orig(underlying, start, end, kind, strike_lte=strike_lte, strike_gte=strike_gte)

    fake.option_contracts = wrapped
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    occ = _occ("F", EXP, "P", 10)
    assert code == 0, out
    assert seen == [min(MC.WHEEL_MAX_STRIKE, 11.0)]
    assert f"chosen fallback put {occ} strike 10.00 bid 0.30 (90.9% of spot 11.00)" in out
    sold = [body["symbol"] for body in fake.submits if body.get("position_intent") == "sell_to_open"]
    assert sold == [occ]
    assert _occ("F", EXP, "P", 11) not in {body["symbol"] for body in fake.submits}
    assert option_flat(fake, occ)


def test_suite_refuses_a_chain_whose_bids_round_below_one_tick(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    quotes = {8: (0.04, 0.10), 9: (0.02, 0.10), 10: (0.0, 0.10), 11: (0.80, 0.90)}
    fake.contracts, fake.snapshots = _puts(EXP, quotes)
    code = MT.run_wheel_sell_buyback(
        cfg, journal, fake, fake, fake, root=str(tmp_path), expected_account_id="MECH-1",
        record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 2
    assert ("contracts listed 4, after DTE filter 4, after strike filter 3, "
            "with positive bid 2, sellable bid (>= 1 tick after rounding) 0") in out
    assert fake.submits == []


def test_wheel_expiry_start_refusal_prints_filter_counts(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    for snap in fake.snapshots.values():
        snap["latestQuote"]["bp"] = 0
    code = MT.run_wheel_expiry(
        cfg, journal, fake, fake, fake, action="start", root=str(tmp_path),
        expected_account_id="MECH-1", record=True, **_run_kw(fake, clock))
    out = capsys.readouterr().out
    assert code == 2
    assert "slightly-ITM" in out
    assert "contracts listed 1, after DTE filter 1, after strike filter 1, with positive bid 0" in out
    assert "sellable bid (>= 1 tick after rounding) 0" in out
    assert fake.submits == []


def option_flat(fake, occ):
    return MT.option_qty(fake.positions(), occ) == 0


def test_wheel_expiry_mock_does_not_record_lifecycle(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    kw = dict(root=str(tmp_path), expected_account_id="MECH-1", record=True, **_run_kw(fake, clock))
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="start", **kw) == 0
    put = _occ("F", EXP, "P", 12)
    assert any(p["symbol"] == put and p["side"] == "short" for p in fake.positions())
    assert not os.path.exists(LG.gate_path(str(tmp_path)))
    fake.positions_list = [p for p in fake.positions_list if p["symbol"] != put]
    out = capsys.readouterr().out
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="reconcile", **kw) == 0
    out = capsys.readouterr().out
    assert "MOCK" in out and "NOT recorded" in out
    assert not os.path.exists(LG.gate_path(str(tmp_path)))
    track = journal.get(MC.TRACK_KEY)
    assert track["put_evidence"] == "MOCK"


def test_wheel_expiry_observed_assignment_call_and_delivery(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    kw = dict(root=str(tmp_path), expected_account_id="MECH-1", record=True, **_run_kw(fake, clock))
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="start", **kw) == 0
    put = _occ("F", EXP, "P", 12)
    call = _occ("F", EXP, "C", 12)
    fake.positions_list = [p for p in fake.positions_list if p["symbol"] != put]
    fake.positions_list.append({"symbol": "F", "qty": "100", "side": "long", "asset_class": "us_equity"})
    fake.activities_list.append({"symbol": put, "activity_type": "OPASN"})
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="reconcile", **kw) == 0
    out = capsys.readouterr().out
    assert "Reserved shares 100" in out
    assert "wheel_lifecycle NOT recorded" in out
    assert MT.option_qty(fake.positions(), call) == 1
    reserved = journal.db.execute(
        "SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND kind='CALL_SHARES'"
    ).fetchone()["a"]
    assert reserved == 100
    fake.positions_list = [p for p in fake.positions_list if p["symbol"] != call]
    fake.positions_list = [p for p in fake.positions_list if p["symbol"] != "F"]
    fake.activities_list.append({"symbol": call, "activity_type": "OPASN"})
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="reconcile", **kw) == 0
    out = capsys.readouterr().out
    assert "RESULT: PASS" in out and "OBSERVED_PAPER_ASSIGNMENT" in out
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "OBSERVED_PAPER_ASSIGNMENT" in gate["wheel_lifecycle"]["evidence"]
    assert "MOCK" not in gate["wheel_lifecycle"]["evidence"]
    assert journal.get(MC.TRACK_KEY)["completed"] is True


def test_wheel_expiry_call_expiry_records_observed_assignment(tmp_path, capsys):
    cfg = Config(ROOT)
    clock = _clock()
    journal = _journal(tmp_path, cfg, clock)
    fake = MechFake(clock)
    _arm_wheel(fake)
    kw = dict(root=str(tmp_path), expected_account_id="MECH-1", record=True, **_run_kw(fake, clock))
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="start", **kw) == 0
    put = _occ("F", EXP, "P", 12)
    call = _occ("F", EXP, "C", 12)
    fake.positions_list = [{"symbol": "F", "qty": "100", "side": "long", "asset_class": "us_equity"}]
    fake.activities_list = [{"symbol": put, "activity_type": "OPASN"}]
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="reconcile", **kw) == 0
    fake.positions_list = [p for p in fake.positions_list if p["symbol"] != call]
    fake.activities_list.append({"symbol": call, "activity_type": "OPEXP"})
    assert MT.run_wheel_expiry(cfg, journal, fake, fake, fake, action="reconcile", **kw) == 0
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "OPEXP" in gate["wheel_lifecycle"]["evidence"]
    assert gate["wheel_lifecycle"]["evidence"].split()[0] == "OBSERVED_PAPER_ASSIGNMENT"


def test_main_report_drops_mechanics_events_and_labels_provisional(cfg, journal, tmp_path):
    journal.event("CYCLE_START", {})
    journal.event("MECHANICS_STEP", {"series": MC.SERIES, "step": "sell_put"})
    kept = RP.performance_events(journal)
    assert [row["kind"] for row in kept] == ["CYCLE_START"]
    html_path, csv_path = RP.write_local(str(tmp_path), cfg, journal, {"actions": []}, "s",
                                         directory=str(tmp_path / "reports"))
    text = open(csv_path, encoding="utf-8").read()
    assert "MECHANICS_STEP" not in text
    mech_html, mech_csv = RP.write_local(
        str(tmp_path), cfg, journal, {"wheel_lifecycle_label": "PROVISIONAL", "actions": []}, "m",
        directory=str(tmp_path / "reports" / "mechanics"))
    assert "MECHANICS_STEP" in open(mech_csv, encoding="utf-8").read()
    page = open(mech_html, encoding="utf-8").read()
    assert "Wheel lifecycle" in page and "PROVISIONAL" in page
    assert "PROVISIONAL" not in open(html_path, encoding="utf-8").read()


def test_entry_policy_blocks_wheel_and_labels_provisional(cfg, journal, root, broker, monkeypatch):
    called = []
    monkeypatch.setattr(C, "stock_entries", lambda *a, **k: called.append("stock"))
    monkeypatch.setattr(C, "wheel_entries", lambda *a, **k: called.append("wheel"))
    journal.set("healthy_streak", 2)
    blocked = LG.WheelDecision(False, False, "wheel_sell_buyback_missing", "BLOCKED")
    summary = C.run_cycle(cfg, journal, "AUTO_PAPER", C.Deps(
        reader=ReaderView(broker), writer=broker, md=broker, expected_account_id="ACC-1"), root,
        entry_policy=blocked)
    assert called == ["stock"]
    assert summary["wheel_lifecycle_label"] == "BLOCKED"
    assert summary["wheel"].startswith("WHEEL_ENTRIES_BLOCKED")
    assert journal.events("WHEEL_ENTRIES_BLOCKED")
    called.clear()
    provisional = LG.WheelDecision(True, True, "PROVISIONAL", "PROVISIONAL")
    summary = C.run_cycle(cfg, journal, "AUTO_PAPER", C.Deps(
        reader=ReaderView(broker), writer=broker, md=broker, expected_account_id="ACC-1"), root,
        entry_policy=provisional)
    assert called == ["stock", "wheel"]
    assert summary["wheel_lifecycle_label"] == "PROVISIONAL"
    assert journal.events("WHEEL_LIFECYCLE_PROVISIONAL")


def test_mock_failure_blocks_wheel_entries(monkeypatch, tmp_path):
    root = str(tmp_path)
    os.makedirs(os.path.join(root, "state"), exist_ok=True)
    # The decision only needs the gate file and the mock result.
    monkeypatch.setattr(W, "mock_paths_pass", lambda: (False, "broken"))
    path = LG.gate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({
            "stock_lifecycle": _record("stock"),
            "wheel_sell_buyback": _record("sell"),
        }, handle)
    decision = LG.wheel_entry_decision(root)
    assert decision.wheel_entries is False
    assert decision.reason == "wheel_mock_failed"


def _record(kind):
    return {
        "passed_at_utc": "2026-11-16T00:00:00Z",
        "mechanics_account_id_sha256_prefix": "0123456789abcdef",
        "runner_git_commit": "UNKNOWN",
        "config_hash": "2df03f83726ac139",
        "evidence": kind,
    }


def test_cli_refuses_main_closed_market_and_records_stock(monkeypatch, tmp_path, capsys):
    root = str(tmp_path)
    for name in ("policy.json", "watchlist.json"):
        shutil.copy(os.path.join(ROOT, name), tmp_path / name)
    shutil.copytree(os.path.join(ROOT, "schemas"), tmp_path / "schemas")
    monkeypatch.setattr(desk, "ROOT", root)
    monkeypatch.setattr(desk, "_cfg", lambda: Config(root))
    assert Config(root).config_hash == "2df03f83726ac139"

    def boom(*_a, **_k):
        raise AssertionError("main account client")

    monkeypatch.setattr(desk, "build_deps", boom)
    assert desk.main(["mechanics-test", "--suite", "stock"]) == 2
    assert "only runs with --account mechanics" in capsys.readouterr().out

    monkeypatch.setattr(desk, "doctor", lambda *a, **k: (False, []))
    assert desk.main(["mechanics-test", "--account", "mechanics", "--suite", "stock"]) == 2
    assert "doctor" in capsys.readouterr().out

    clock = _clock()
    fake = MechFake(clock)
    monkeypatch.setattr(desk, "doctor", lambda *a, **k: (True, []))

    def build(cfg, mode, account="main"):
        assert account == "mechanics" and mode == "AUTO_PAPER"
        return C.Deps(reader=fake, writer=fake, md=fake, expected_account_id="MECH-1")

    monkeypatch.setattr(desk, "build_deps", build)

    def secret(name):
        if name == "ALPACA_EXPECTED_ACCOUNT_ID":
            return "MAIN-EXPECTED"
        if name == "MECH_ALPACA_EXPECTED_ACCOUNT_ID":
            return "MECH-1"
        return "k"

    monkeypatch.setattr(desk, "_secret", secret)
    # The suite polls the fake immediately; pin the journal clock through Journal.
    real_journal = Journal

    def journal_factory(path, cfg, run_id=None, clock=None):
        return real_journal(path, cfg, run_id=run_id, clock=_clock())

    monkeypatch.setattr(desk, "Journal", journal_factory)
    monkeypatch.setattr(MT, "poll_order", _instant_poll)
    code = desk.main(["mechanics-test", "--account", "mechanics", "--suite", "stock", "--record"])
    out = capsys.readouterr().out
    assert code == 0, out
    gate = json.loads(open(LG.gate_path(root), encoding="utf-8").read())
    assert "stock_lifecycle" in gate
    assert not os.path.exists(os.path.join(root, "data", "journal.sqlite3"))


def _instant_poll(reader, order_id, accept, attempts, interval, sleep):
    last = reader.order(order_id) if order_id else None
    if (last or {}).get("status") in accept:
        return last
    return last


def test_cli_wheel_expiry_requires_a_phase(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(desk, "_cfg", lambda: Config(ROOT))
    monkeypatch.setattr(desk, "doctor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("doctor")))
    assert desk.main(["mechanics-test", "--account", "mechanics", "--suite", "wheel-expiry"]) == 2
    assert "--start" in capsys.readouterr().out
