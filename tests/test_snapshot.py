"""Read-only SIZER snapshot: no writes, buying power never used as cash, sizing base = min(initial, equity)."""
import importlib.util, os, sys

spec = importlib.util.spec_from_file_location("desk", os.path.join(os.path.dirname(__file__), "..", "desk.py"))
desk = importlib.util.module_from_spec(spec); spec.loader.exec_module(desk)


def test_snapshot_read_only_and_bases(cfg, journal, broker):
    broker.acct.update({"account_number": "PA3TEST", "cash": "100000", "equity": "98000",
                        "buying_power": "400000", "multiplier": "4"})
    s = desk.build_snapshot(cfg, journal, broker, "PA3TEST", "2026-11-12T00:00:00Z")
    assert s["read_only"] and broker.submits == [] and broker.cancels == []
    assert s["account"]["account_matches_expected"] is True
    assert s["sizing_base"] == 98000.0
    assert s["ledgers"]["STOCK"]["ledger_cash"] == 20000.0 and s["ledgers"]["WHEEL"]["ledger_cash"] == 80000.0
    assert s["ledger_sum_vs_broker_cash"] == 0.0
    assert s["account"]["buying_power_info_only_not_used"] == 400000.0
    assert "400000" not in str(s["ledgers"]) and s["ledgers"]["WHEEL"]["free_cash"] == 80000.0
    assert s["put_reserve_rule"].endswith("$3.00 per contract")


def test_snapshot_sizing_base_capped_at_initial(cfg, journal, broker):
    broker.acct.update({"cash": "101000", "equity": "105000"})
    s = desk.build_snapshot(cfg, journal, broker, "ACC-1", "t")
    assert s["sizing_base"] == 100000.0 and s["account"]["account_matches_expected"] is True


def test_snapshot_flags_wrong_account(cfg, journal, broker):
    s = desk.build_snapshot(cfg, journal, broker, "PA3OTHER", "t")
    assert s["account"]["account_matches_expected"] is False


# ---- option positions, assigned lots, covered-call coverage (Sizer) ----
import json  # noqa: E402
from conftest import ReaderView  # noqa: E402
from sakibdesk import wheel as W  # noqa: E402


def _cyc(journal, cid, sym, state, **d):
    journal.db.execute("INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)", (cid, sym, state, json.dumps(d), cid, cid))


def _opt(sym, qty, side, avg, px):
    return {"symbol": sym, "asset_class": "us_option", "qty": str(qty), "side": side, "avg_entry_price": str(avg),
            "current_price": str(px), "market_value": str(float(qty) * px * 100), "cost_basis": str(float(qty) * avg * 100),
            "unrealized_pl": "0"}


def test_parse_occ():
    assert W.parse_occ("MU261204P00150000") == {"underlying": "MU", "type": "put", "expiry": "2026-12-04", "strike": 150.0}
    assert W.parse_occ("NVDA261218C00187500")["strike"] == 187.5
    assert W.parse_occ("MU") is None and W.parse_occ("MU261340P00150000") is None


def test_snapshot_options_assigned_lot_and_covered_call(cfg, journal, broker):
    # AMD: put assigned at 150 -> SHARES_HELD lot, covered call open. NVDA: short put open. AVGO: call with no lot.
    _cyc(journal, "C1", "AMD", "CALL_OPEN", acquisition_price=150.0, option_symbol="AMD261218C00160000",
         strike=160.0, expiry="2026-12-18")
    _cyc(journal, "C2", "NVDA", "PUT_OPEN", option_symbol="NVDA261204P00170000", strike=170.0, expiry="2026-12-04")
    broker.positions_list = [
        {"symbol": "AMD", "asset_class": "us_equity", "qty": "100", "side": "long", "avg_entry_price": "150",
         "current_price": "140", "market_value": "14000"},
        _opt("AMD261218C00160000", -1, "short", 2.10, 1.50),
        _opt("NVDA261204P00170000", -1, "short", 3.00, 2.40),
        _opt("AVGO261218C00400000", -1, "short", 5.00, 4.00),
    ]
    reader = ReaderView(broker)
    s = desk.build_snapshot(cfg, journal, reader, "ACC-1", "t")
    assert broker.submits == [] and broker.cancels == []
    ops = {o["symbol"]: o for o in s["option_positions"]}
    amd = ops["AMD261218C00160000"]
    assert (amd["underlying"], amd["type"], amd["strike"], amd["expiry"], amd["contracts"]) == \
        ("AMD", "call", 160.0, "2026-12-18", -1)
    assert amd["avg_entry_price"] == 2.10 and amd["current_price"] == 1.50 and amd["market_value"] == -150.0
    assert amd["subledger"] == "WHEEL" and amd["wheel_cycle_id"] == "C1" and amd["wheel_state"] == "CALL_OPEN"
    assert ops["NVDA261204P00170000"]["type"] == "put" and ops["NVDA261204P00170000"]["wheel_cycle_id"] == "C2"
    assert ops["AVGO261218C00400000"]["subledger"] == "UNATTRIBUTED"
    assert "AMD" not in ops   # equities are not option positions
    (lot,) = s["assigned_lots"]
    assert lot["underlying"] == "AMD" and lot["shares"] == 100
    assert lot["acquisition_value"] == 15000.0 and lot["market_value"] == 14000.0
    assert lot["allocation_value"] == 15000.0            # max(cost, market)
    assert lot["shares_committed_to_open_calls"] == 100 and lot["free_shares"] == 0
    cov = {c["option_symbol"]: c for c in s["covered_call_coverage"]}
    assert cov["AMD261218C00160000"]["covered"] is True
    assert cov["AVGO261218C00400000"]["covered"] is False
    assert any("UNCOVERED CALL AVGO261218C00400000" in f for f in s["coverage_flags"])
    assert not any("AMD261218" in f for f in s["coverage_flags"])
    assert "strike x 100 x contracts" in s["assigned_lot_basis"]


def test_snapshot_lot_market_above_cost_and_missing_shares_flagged(cfg, journal, broker):
    _cyc(journal, "C1", "MU", "SHARES_HELD", acquisition_price=150.0)
    _cyc(journal, "C9", "TSM", "CLOSED", acquisition_price=200.0)        # closed cycles are not lots
    broker.positions_list = [{"symbol": "MU", "asset_class": "us_equity", "qty": "100", "current_price": "170"}]
    s = desk.build_snapshot(cfg, journal, ReaderView(broker), "ACC-1", "t")
    (lot,) = s["assigned_lots"]
    assert lot["market_value"] == 17000.0 and lot["allocation_value"] == 17000.0
    assert lot.get("free_shares", 100) == 100 and s["coverage_flags"] == []
    # a second covered call on the same 100 shares is flagged
    broker.positions_list += [_opt("MU261218C00180000", -2, "short", 2.0, 1.0)]
    s = desk.build_snapshot(cfg, journal, ReaderView(broker), "ACC-1", "t")
    (c,) = s["covered_call_coverage"]
    assert c["shares_required"] == 200 and c["covered"] is False
    assert s["assigned_lots"][0]["free_shares"] == -100
    # booked lot but shares not visible at the broker (or held by the STOCK subledger) is flagged
    broker.positions_list = []
    s = desk.build_snapshot(cfg, journal, ReaderView(broker), "ACC-1", "t")
    assert s["assigned_lots"][0]["market_value"] is None and s["assigned_lots"][0]["allocation_value"] is None
    assert any("wheel-available shares < 100" in f for f in s["coverage_flags"])
    assert any("market value UNKNOWN" in f for f in s["coverage_flags"])


def test_snapshot_uses_reader_only():
    import inspect
    src = inspect.getsource(desk.build_snapshot) + inspect.getsource(desk._option_positions) + \
        inspect.getsource(desk._assigned_lots_and_coverage)
    assert "PaperWriter" not in src and ".submit(" not in src and ".cancel(" not in src and "place(" not in src
