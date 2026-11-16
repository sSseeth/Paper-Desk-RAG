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
from sakibdesk import wheel as W
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
        self.bars = {}

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
            legs = [
                {"id": oid + "-tp", "type": "limit", "qty": body["qty"], "status": "new",
                 "filled_qty": "0", "symbol": sym, "side": "sell"},
                {"id": oid + "-sl", "type": "stop", "qty": body["qty"], "status": "new",
                 "filled_qty": "0", "symbol": sym, "side": "sell"},
            ]
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

    def option_contracts(self, underlying, start, end, kind):
        return [c for c in self.contracts
                if c["underlying_symbol"] == underlying and c["type"] == kind and start <= c["expiration_date"] <= end]

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
    assert option_flat(fake, occ)
    gate = json.loads(open(LG.gate_path(str(tmp_path)), encoding="utf-8").read())
    assert "wheel_sell_buyback" in gate
    assert journal.run_id in gate["wheel_sell_buyback"]["evidence"]
    assert "wheel_lifecycle" not in gate


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
