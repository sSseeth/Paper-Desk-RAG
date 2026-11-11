#!/usr/bin/env python3
"""SakibPaperDesk - Alpaca PAPER-ONLY runner (guide v2, 22 Oct 2026). No live mode exists."""
import argparse, json, os, subprocess, sys, uuid
from datetime import date

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
from sakibdesk import secrets_store as SS                     # noqa: E402
from sakibdesk.config import Config, ConfigError, PAPER_TRADING_HOST, DATA_HOST  # noqa: E402
from sakibdesk.journal import Journal, iso                    # noqa: E402
from sakibdesk.lock import ProcessLock, AlreadyRunning        # noqa: E402
from sakibdesk import research as R, reports as RP, cycle as C  # noqa: E402

DB = os.path.join(ROOT, "data", "journal.sqlite3")
LOCK = os.path.join(ROOT, "data", "desk.lock")


def _cfg():
    try:
        return Config(ROOT)
    except ConfigError as e:
        print(f"CONFIG INVALID: {e}"); sys.exit(3)


def _secret(name):
    return SS.get(name, ROOT)


def build_deps(cfg, mode):
    """DRY_RUN never constructs a PaperWriter (no object that can POST/DELETE orders)."""
    if SS.missing(ROOT):
        return C.Deps()
    from sakibdesk.alpaca import PaperReader, PaperWriter, MarketData
    kid, sec = _secret("ALPACA_PAPER_KEY_ID"), _secret("ALPACA_PAPER_SECRET")
    reader = PaperReader(kid, sec)
    writer = PaperWriter(kid, sec) if mode == "AUTO_PAPER" else None
    md = MarketData(kid, sec, cfg.feeds["stock_feed"], cfg.feeds["option_feed"])
    rb = R.ResearchBridge(None, _secret("GITHUB_RESEARCH_REPO"), _secret("GITHUB_READONLY_TOKEN"))
    return C.Deps(reader, writer, md, _secret("ALPACA_EXPECTED_ACCOUNT_ID"), rb)


def doctor(cfg, verbose=True, online=True):
    checks = []
    checks.append(("config_valid", True, f"config_hash={cfg.config_hash} strategy={cfg.strategy_version}"))
    checks.append(("paper_host_hardcoded", PAPER_TRADING_HOST == "https://paper-api.alpaca.markets", PAPER_TRADING_HOST))
    checks.append(("data_host", DATA_HOST == "https://data.alpaca.markets", DATA_HOST))
    checks.append(("feed_profile", True, f"{cfg.feed_profile} stock={cfg.feeds['stock_feed']} "
                                          f"options={cfg.feeds['option_feed']} label={cfg.feeds['option_quotes_label']}"))
    ok, why, actual = R.schema_status(ROOT, cfg.policy)
    checks.append(("research_schema_signed", ok, f"{why}; file sha256={actual}; signed="
                                                 f"{cfg.policy['research']['signed_schema_sha256'] or '(empty)'}"))
    miss = SS.missing(ROOT)
    checks.append(("secrets_present", not miss, "missing: " + ", ".join(miss) if miss else "all present"))
    if not miss and online:
        try:
            deps = build_deps(cfg, "DRY_RUN")
            a = deps.reader.account()
            checks.append(("paper_account_id", C.account_matches(a, deps.expected_account_id),
                           f"account_number {a and a.get('account_number')} id {a and a.get('id')} "
                           f"status {a and a.get('status')}"))
            deps.reader.clock()
            checks.append(("clock", True, "ok"))
            q = deps.md.latest_quotes(["SPY"])
            checks.append(("stock_feed_entitled", bool(q.get("SPY")), cfg.feeds["stock_feed"]))
        except Exception as e:
            checks.append(("broker_read_only", False, SS.redact(str(e))))
    passed = all(c[1] for c in checks)
    if verbose:
        for n, o, d in checks:
            print(f"[{'PASS' if o else 'FAIL'}] {n}: {d}")
        print("DOCTOR:", "PASS" if passed else "FAIL (new entries stay blocked; nothing was sent)")
    return passed, checks


def cmd_cycle(cfg, mode):
    log = SS.setup_logging(ROOT)
    try:
        with ProcessLock(LOCK):
            j = Journal(DB, cfg)
            start_hash = cfg.config_hash
            j.set("active_run", {"run_id": j.run_id, "config_hash": start_hash})
            deps = build_deps(cfg, mode)
            if mode == "AUTO_PAPER" and deps.writer is None:
                print("AUTO_PAPER refused: secrets missing; no orders possible.")
                j.event("AUTO_PAPER_REFUSED", {"reason": "secrets missing"})
                return 2
            if deps.research:
                deps.research.j = j; deps.research.clock = j.clock
            summary = C.run_cycle(cfg, j, mode, deps, ROOT)
            if Config(ROOT).config_hash != start_hash:
                j.event("POLICY_CHANGED_DURING_RUN", {"start": start_hash})
                summary["entry_blocks"].append("POLICY_CHANGED_DURING_RUN")
            summary["stamp"] = RP.stamp(cfg)
            label = date.today().isoformat()
            h, c = RP.write_local(ROOT, cfg, j, summary, label)
            eid = f"HB-{j.run_id}"
            RP.enqueue(j, eid, {"type": "cycle_summary", "event_id": eid, **summary})
            RP.deliver(j, _secret_safe("GROK_WEBHOOK_URL"), _secret_safe("GROK_WEBHOOK_BEARER"))
            j.set("heartbeat", iso(j.clock()))
            j.set("active_run", None)
            log.info("cycle %s done blocks=%s", mode, summary["entry_blocks"])
            print(json.dumps(summary, indent=2, default=str))
            print(f"Local reports: {h} , {c}")
            return 0
    except AlreadyRunning:
        print("Another cycle holds the lock; exiting without action.")
        return 0


def _secret_safe(n):
    try:
        return _secret(n)
    except Exception:
        return None


def cmd_report(cfg):
    j = Journal(DB, cfg)
    summary = {"type": "report", "events": len(j.events()), "stamp": RP.stamp(cfg),
               "outbox_pending": j.db.execute("SELECT COUNT(*) n FROM outbox WHERE status='PENDING'").fetchone()["n"]}
    h, c = RP.write_local(ROOT, cfg, j, summary, "manual-" + date.today().isoformat())
    eid = f"RPT-{uuid.uuid4().hex[:12]}"
    RP.enqueue(j, eid, summary)
    print(RP.deliver(j, _secret_safe("GROK_WEBHOOK_URL"), _secret_safe("GROK_WEBHOOK_BEARER")))
    print(f"Local reports: {h} , {c}")
    return 0


def build_snapshot(cfg, j, reader, expected_account_id, now_iso):
    """Read-only account + ledger snapshot for SIZER. Never constructs a writer. Broker margin
    buying power is reported for information only and is never used as available cash."""
    from sakibdesk.ledger import Ledger
    from sakibdesk.orders import OrderManager
    a = reader.account() or {}
    f = lambda k: float(a[k]) if a.get(k) not in (None, "") else None
    equity = f("equity")
    initial = float(cfg.policy["capital"]["initial"])
    led, om = Ledger(j, cfg.policy), OrderManager(j, cfg)
    ledgers = {}
    for name in ("STOCK", "WHEEL"):
        ledgers[name] = {"initial_capital": float(cfg.policy["capital"]["stock" if name == "STOCK" else "wheel"]),
                         "ledger_cash": round(led.cash(name), 2),
                         "reserved_dollars": round(om.reserved(name), 2),
                         "free_cash": round(led.free_cash(name, om), 2),
                         "reserved_call_shares": om.reserved(name, kind="CALL_SHARES")}
    raw_pos = reader.positions() or []
    pos = [{k: p.get(k) for k in ("symbol", "asset_class", "qty", "side", "avg_entry_price", "market_value",
                                  "cost_basis", "current_price", "unrealized_pl")} for p in raw_pos]
    orders = [{k: o.get(k) for k in ("id", "client_order_id", "symbol", "side", "type", "qty", "filled_qty",
                                     "limit_price", "status", "submitted_at")} for o in (reader.orders("open") or [])]
    intents = [dict(r) for r in j.db.execute(
        "SELECT * FROM intents WHERE status NOT IN ('filled','canceled','expired','rejected','replaced','DRY_RUN','NOT_FOUND_RELEASED')"
    ).fetchall()]
    option_positions = _option_positions(j, raw_pos)
    lots, coverage, cov_flags = _assigned_lots_and_coverage(j, raw_pos, option_positions)
    return {"type": "snapshot", "as_of_utc": now_iso, "read_only": True,
            "experiment_id": cfg.policy.get("experiment_id"), "strategy_version": cfg.strategy_version,
            "config_hash": cfg.config_hash, "feed_profile": cfg.feed_profile,
            "account": {"account_number": a.get("account_number"), "id": a.get("id"), "status": a.get("status"),
                        "account_matches_expected": C.account_matches(a, expected_account_id),
                        "trading_blocked": a.get("trading_blocked"), "cash": f("cash"), "equity": equity,
                        "last_equity": f("last_equity"), "long_market_value": f("long_market_value"),
                        "short_market_value": f("short_market_value"),
                        "buying_power_info_only_not_used": f("buying_power"), "multiplier": a.get("multiplier"),
                        "options_trading_level": a.get("options_trading_level")},
            "initial_capital": initial,
            "sizing_base": (min(initial, equity) if equity is not None else None),
            "sizing_base_rule": "lower of initial capital and current net equity",
            "ledgers": ledgers,
            "ledger_sum_vs_broker_cash": (round(ledgers["STOCK"]["ledger_cash"] + ledgers["WHEEL"]["ledger_cash"]
                                                - f("cash"), 2) if f("cash") is not None else None),
            "broker_positions": pos, "broker_open_orders": orders, "runner_open_intents": intents,
            "option_positions": option_positions,
            "assigned_lots": lots,
            "assigned_lot_basis": ("acquisition_value = put strike x 100 x contracts, exactly as the runner books a put "
                                   "assignment in the WHEEL ledger (reason put_assignment_buy_100). The opening put "
                                   "premium and the $1 cost overlays are booked as separate cash flows and are NOT "
                                   "netted into it. allocation_value = max(acquisition_value, market_value); "
                                   "null when market value is unknown."),
            "covered_call_coverage": coverage,
            "coverage_flags": cov_flags,
            "put_reserve_rule": f"strike*100 + ${cfg.policy['wheel']['put_reserve_cost_allowance_per_contract']:.2f} per contract"}


def _num(x):
    try:
        return float(x) if x not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _option_positions(j, raw_pos):
    """Broker option positions (OCC symbols) with parsed contract terms and the runner subledger that owns them."""
    from sakibdesk import wheel as W
    by_occ = {}
    for r in j.db.execute("SELECT cycle_id, symbol, state, data FROM wheel_cycles").fetchall():
        d = json.loads(r["data"] or "{}")
        if d.get("option_symbol"):
            by_occ.setdefault(d["option_symbol"], []).append((r["cycle_id"], r["state"]))
    out = []
    for p in raw_pos:
        sym = p.get("symbol") or ""
        occ = W.parse_occ(sym)
        if p.get("asset_class") != "us_option" and occ is None:
            continue
        row = {"symbol": sym, "underlying": None, "type": None, "strike": None, "expiry": None,
               "contracts": None, "side": p.get("side"), "avg_entry_price": _num(p.get("avg_entry_price")),
               "current_price": _num(p.get("current_price")), "market_value": _num(p.get("market_value")),
               "cost_basis": _num(p.get("cost_basis")), "unrealized_pl": _num(p.get("unrealized_pl"))}
        if occ is None:
            row["parse_error"] = "not a valid OCC symbol (terms UNKNOWN)"
        else:
            row.update(occ)
        q = _num(p.get("qty"))
        if q is not None and p.get("side") == "short" and q > 0:
            q = -q                                  # signed: short positions are negative
        row["contracts"] = int(q) if q is not None and q == int(q) else q
        cyc = by_occ.get(sym, [])
        if len(cyc) == 1:
            row.update(subledger="WHEEL", wheel_cycle_id=cyc[0][0], wheel_state=cyc[0][1])
        elif len(cyc) > 1:
            row.update(subledger="WHEEL", wheel_cycle_id=[c[0] for c in cyc], wheel_state=[c[1] for c in cyc],
                       attribution_note="more than one wheel cycle references this contract")
        else:
            it = j.db.execute("SELECT DISTINCT strategy FROM intents WHERE symbol=?", (sym,)).fetchall()
            strategies = sorted({r["strategy"] for r in it if r["strategy"]})
            row["subledger"] = strategies[0] if len(strategies) == 1 else "UNATTRIBUTED"
            if row["subledger"] == "UNATTRIBUTED":
                row["attribution_note"] = "no wheel cycle or runner order references this contract"
        out.append(row)
    return out


HELD_LOT_STATES = ("SHARES_HELD", "CALL_PENDING", "CALL_OPEN", "CALL_CLOSING", "CALL_ASSIGNMENT_PENDING", "UNWINDING")


def _assigned_lots_and_coverage(j, raw_pos, option_positions):
    """Assigned share lots from the wheel state machine (cycles that reached SHARES_HELD via a confirmed
    OPASN/OPXRC assignment carry acquisition_price), valued at max(cost, market), plus covered-call coverage."""
    eq = {p["symbol"]: p for p in raw_pos if p.get("asset_class") == "us_equity" or
          (p.get("asset_class") is None and len(p.get("symbol") or "") <= 6)}
    stock_qty = {}
    for r in j.db.execute("SELECT symbol, data FROM stock_positions").fetchall():
        stock_qty[r["symbol"]] = _num(json.loads(r["data"] or "{}").get("filled_qty")) or 0.0
    short_calls = {}
    for o in option_positions:
        if o.get("type") == "call" and isinstance(o.get("contracts"), (int, float)) and o["contracts"] < 0:
            short_calls.setdefault(o["underlying"], []).append(o)
    lots, coverage, flags = [], [], []
    lot_shares_by_sym = {}
    for r in j.db.execute("SELECT cycle_id, symbol, state, data FROM wheel_cycles ORDER BY created_utc, cycle_id").fetchall():
        d = json.loads(r["data"] or "{}")
        if r["state"] not in HELD_LOT_STATES or d.get("acquisition_price") is None:
            continue
        sym, contracts = r["symbol"], int(d.get("contracts", 1))
        shares = 100 * contracts
        acq = round(float(d["acquisition_price"]) * 100 * contracts, 2)
        bp = eq.get(sym) or {}
        px = _num(bp.get("current_price"))
        broker_qty = _num(bp.get("qty")) or 0.0
        mkt = round(px * shares, 2) if px is not None else None
        lot = {"cycle_id": r["cycle_id"], "underlying": sym, "wheel_state": r["state"], "shares": shares,
               "assignment_contracts": contracts, "acquisition_price_per_share": float(d["acquisition_price"]),
               "acquisition_value": acq, "current_price": px, "market_value": mkt,
               "market_value_source": "broker position current_price x lot shares" if px is not None
               else "UNKNOWN (no broker price for the underlying)",
               "allocation_value": max(acq, mkt) if mkt is not None else None,
               "broker_shares_total": broker_qty, "stock_subledger_shares": stock_qty.get(sym, 0.0)}
        lot_shares_by_sym[sym] = lot_shares_by_sym.get(sym, 0) + shares
        lots.append(lot)
        if mkt is None:
            flags.append(f"{sym}: market value UNKNOWN; allocation value not computed")
    # coverage per underlying: wheel lot shares actually visible at the broker (after STOCK subledger shares)
    for sym in sorted(set(lot_shares_by_sym) | set(short_calls)):
        lot_sh = lot_shares_by_sym.get(sym, 0)
        visible = max(0.0, (_num((eq.get(sym) or {}).get("qty")) or 0.0) - stock_qty.get(sym, 0.0))
        capacity = min(lot_sh, visible)
        if visible < lot_sh:
            flags.append(f"{sym}: broker shows {visible:g} wheel-available shares < {lot_sh} booked lot shares")
        committed, remaining = 0, capacity
        for o in short_calls.get(sym, []):
            need = 100 * abs(int(o["contracts"]))
            covered = remaining >= need
            remaining -= min(remaining, need)
            committed += need
            coverage.append({"option_symbol": o["symbol"], "underlying": sym, "contracts": o["contracts"],
                             "shares_required": need, "covered": covered})
            if not covered:
                flags.append(f"UNCOVERED CALL {o['symbol']}: needs {need} free shares, "
                             f"wheel lot shares available {capacity:g}")
        for lot in lots:
            if lot["underlying"] == sym:
                lot["open_short_call_contracts"] = sum(abs(int(o["contracts"])) for o in short_calls.get(sym, []))
                lot["shares_committed_to_open_calls"] = committed
                lot["free_shares"] = lot_sh - committed
    return lots, coverage, flags


def cmd_snapshot(cfg):
    miss = SS.missing(ROOT)
    if miss:
        print("SNAPSHOT refused: secrets missing: " + ", ".join(miss)); return 2
    from sakibdesk.alpaca import PaperReader
    j = Journal(DB, cfg)
    snap = build_snapshot(cfg, j, PaperReader(_secret("ALPACA_PAPER_KEY_ID"), _secret("ALPACA_PAPER_SECRET")),
                          _secret("ALPACA_EXPECTED_ACCOUNT_ID"), iso(j.clock()))
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out = os.path.join(ROOT, "reports", f"snapshot_{date.today().isoformat()}.json")
    with open(out, "w") as fh:
        json.dump(snap, fh, indent=2, default=str)
    print(SS.redact(json.dumps(snap, indent=2, default=str)))
    print(f"Saved: {out}")
    return 0


def run_research_check(cfg, repo, token, now, session=None):
    """Read-only bridge check: GET research/*.json from the repo (pinned to the latest research/ commit), validate
    each file against the pinned v1.5 rules and return per-file / per-record results with reason codes.
    Never trades, never writes to GitHub, never touches the journal (so the card-immutability check, which needs
    the journal's memory of earlier cards, is not applied here)."""
    ok, why, actual = R.schema_status(cfg.root, cfg.policy)
    res = {"type": "research_check", "as_of_utc": iso(now), "read_only": True, "repo": repo,
           "schema": {"status": why, "pinned_version": R.PINNED_SCHEMA_VERSION, "file_sha256": actual,
                      "signed_sha256": cfg.policy["research"]["signed_schema_sha256"] or None},
           "commit": None, "files": [], "error": None}
    try:
        got = R.fetch_research_readonly(repo, token, session)
    except Exception as e:
        res["error"] = SS.redact(f"{type(e).__name__}: {e}")
        return res
    res["commit"], res["error"] = got["commit"], got["error"]
    for f in got["files"]:
        row = {"path": f["path"], "http": f["http"], "blob_sha": f["blob_sha"], "etag": f["etag"]}
        if f["text"] is None:
            row.update(file_status="NOT_READ", file_codes=[f"HTTP_{f['http']}"], records=[])
        else:
            rep = R.check_text(f["text"], cfg, now)
            for r in rep["records"]:
                r.pop("_parsed", None)
            row.update(rep)
        res["files"].append(row)
    return res


def print_research_check(res):
    print(f"Research check (read-only) repo={res['repo']} commit={res['commit'] or 'UNKNOWN'}")
    sc = res["schema"]
    print(f"Pinned schema: {sc['pinned_version']} status={sc['status']} sha256={sc['file_sha256']}")
    if res["error"]:
        print(f"ERROR: {res['error']}")
    if not res["files"] and not res["error"]:
        print("No research/*.json files found.")
    for f in res["files"]:
        print(f"- {f['path']}  blob={f['blob_sha']}  etag={f['etag']}  file={f['file_status']}"
              + (f" {f['file_codes']}" if f["file_codes"] else "") + ("  [TEST FILE]" if f.get("test_file") else ""))
        for r in f["records"]:
            print(f"    record {r['index']} {r['symbol']}: stock={r['stock_use']} wheel={r['wheel_use']}")
            for c in r["codes"]:
                print(f"        {c}")
            for c in r["wheel_codes"]:
                print(f"        wheel: {c}")
            for n in r.get("notes", []):
                print(f"        note: {n}")
    print("Nothing was traded and nothing was written to GitHub.")


def cmd_research_check(cfg):
    from sakibdesk.journal import utcnow
    miss = [k for k in ("GITHUB_RESEARCH_REPO", "GITHUB_READONLY_TOKEN") if not _secret_safe(k)]
    if miss:
        print("RESEARCH-CHECK refused: secrets missing: " + ", ".join(miss)); return 2
    res = run_research_check(cfg, _secret("GITHUB_RESEARCH_REPO"), _secret("GITHUB_READONLY_TOKEN"), utcnow())
    os.makedirs(os.path.join(ROOT, "reports"), exist_ok=True)
    out = os.path.join(ROOT, "reports", f"research_check_{date.today().isoformat()}.json")
    with open(out, "w") as fh:
        fh.write(SS.redact(json.dumps(res, indent=2, default=str)))
    print(SS.redact(_capture(print_research_check, res)))
    print(f"Saved: {out}")
    if res["error"]:
        return 2
    return 0


def _capture(fn, *a):
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn(*a)
    return buf.getvalue().rstrip("\n")


def cmd_pause(cfg):
    j = Journal(DB, cfg)
    j.set("paused", True)
    j.event("PAUSED", {"note": "no new entries; exits and reconciliation continue"})
    deps = build_deps(cfg, "AUTO_PAPER")
    summary = {"actions": []}
    if deps.writer:
        from sakibdesk.orders import OrderManager
        C.cancel_stale_entries(cfg, j, OrderManager(j, cfg, deps.writer), deps.reader, j.clock(), summary, force=True)
    else:
        summary["actions"].append("no broker connection: pending entries (if any) NOT cancelled")
    print("PAUSED.", json.dumps(summary))
    return 0


def _doctor_ok(cfg):
    return doctor(cfg)[0]


def resume_allowed(j, cfg):
    if not _doctor_ok(cfg):
        return False, "RESUME REFUSED: doctor failed."
    if j.get("experiment_block"):
        return False, "RESUME REFUSED: experiment drawdown block (10%) cannot be bypassed."
    if (j.get("healthy_streak", 0) or 0) < cfg.policy["orders"]["healthy_reconciliations_to_resume"]:
        return False, "RESUME REFUSED: needs 3 consecutive healthy reconciliations (run DRY_RUN/AUTO_PAPER cycles first)."
    return True, "ok"


def cmd_resume(cfg):
    j = Journal(DB, cfg)
    ok, why = resume_allowed(j, cfg)
    if not ok:
        print(why); return 2
    j.set("paused", False); j.event("RESUMED", {})
    print("RESUMED (daily loss block, if any, still applies today)."); return 0


def cmd_flatten(cfg):
    """Cancel/reconcile pending orders; buy back short calls BEFORE selling covering shares; close puts; then
    sell long shares at fresh quotes. Reports unresolved orders; never claims completion without fills."""
    deps = build_deps(cfg, "AUTO_PAPER")
    if not deps.writer:
        print("FLATTEN NOT POSSIBLE: no broker credentials. Nothing sent."); return 2
    from sakibdesk.orders import OrderManager
    from sakibdesk import wheel as W, stock as S
    from sakibdesk.ledger import Ledger
    with ProcessLock(LOCK):
        j = Journal(DB, cfg)
        j.set("paused", True)
        om = OrderManager(j, cfg, deps.writer, ledger=Ledger(j, cfg.policy))
        unresolved = []
        for o in deps.reader.orders("open"):
            if om.cancel_and_confirm(deps.reader, o["id"]) is None:
                unresolved.append(o["id"])
        if unresolved:
            print("Unconfirmed cancels; not proceeding to sells:", unresolved); return 2
        om.reconcile(deps.reader)
        pos = deps.reader.positions()
        opts = [p for p in pos if p.get("asset_class") == "us_option" and float(p["qty"]) < 0]
        calls = [p for p in opts if p["symbol"][-9] == "C"]
        puts = [p for p in opts if p["symbol"][-9] == "P"]
        sent = []
        for grp in (calls, puts):
            for p in grp:
                sn = deps.md.option_snapshots([p["symbol"]]).get(p["symbol"]) or {}
                ask = (sn.get("latestQuote") or {}).get("ap")
                if not ask:
                    unresolved.append(p["symbol"]); continue
                sent.append(om.place(strategy="WHEEL", symbol=p["symbol"], purpose="FLATTEN", signal_key=iso(j.clock()),
                                     body={"symbol": p["symbol"], "qty": str(int(abs(float(p["qty"])))), "side": "buy",
                                           "type": "limit", "time_in_force": "day",
                                           "limit_price": f"{W.tick_round_up(float(ask)):.2f}",
                                           "position_intent": "buy_to_close"}, reservations=[]))
        if calls:
            print("Short calls buyback submitted; shares are sold on the next flatten run after fills confirm.")
        else:
            for p in pos:
                if p.get("asset_class") == "us_equity" and float(p["qty"]) > 0:
                    q = deps.md.latest_quotes([p["symbol"]]).get(p["symbol"]) or {}
                    if not q.get("bp"):
                        unresolved.append(p["symbol"]); continue
                    sent.append(om.place(strategy="STOCK", symbol=p["symbol"], purpose="FLATTEN", signal_key=iso(j.clock()),
                                         body={"symbol": p["symbol"], "qty": str(int(float(p["qty"]))), "side": "sell",
                                               "type": "limit", "time_in_force": "day",
                                               "limit_price": str(S.round_px(float(q["bp"]))),
                                               "position_intent": "sell_to_close"}, reservations=[]))
        print(json.dumps({"submitted": sent, "unresolved": unresolved}, default=str, indent=2))
        print("Not complete until fills are confirmed by reconciliation.")
    return 0


def cmd_self_test():
    cfg = _cfg()
    print(f"Config OK: {cfg.strategy_version} hash={cfg.config_hash} profile={cfg.feed_profile}")
    ok, why, actual = R.schema_status(ROOT, cfg.policy)
    print(f"Research schema: {why} (file sha256 {actual})")
    print("Running offline test suite (mocked broker/data; no network)...")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", os.path.join(ROOT, "tests")],
                       cwd=ROOT)
    print("SELF-TEST:", "PASS" if r.returncode == 0 else f"FAIL (pytest exit {r.returncode})")
    return r.returncode


def main(argv=None):
    ap = argparse.ArgumentParser(prog="desk.py", description="SakibPaperDesk (Alpaca PAPER only)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for c in ("setup-secrets", "doctor", "self-test", "report", "snapshot", "research-check", "pause", "resume", "flatten"):
        sub.add_parser(c)
    cy = sub.add_parser("cycle")
    cy.add_argument("--mode", required=True, choices=["DRY_RUN", "AUTO_PAPER"])
    a = ap.parse_args(argv)
    if a.cmd == "setup-secrets":
        SS.setup_interactive(ROOT); return 0
    if a.cmd == "self-test":
        return cmd_self_test()
    cfg = _cfg()
    if a.cmd == "doctor":
        return 0 if doctor(cfg)[0] else 2
    if a.cmd == "cycle":
        return cmd_cycle(cfg, a.mode)
    return {"report": cmd_report, "snapshot": cmd_snapshot, "research-check": cmd_research_check, "pause": cmd_pause, "resume": cmd_resume, "flatten": cmd_flatten}[a.cmd](cfg)


if __name__ == "__main__":
    sys.exit(main())
