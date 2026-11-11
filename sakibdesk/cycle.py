"""One short cycle (guide p10 sequence):
lock -> account/orders/positions -> reconcile ledger -> risk-reducing exits -> refresh inputs -> remaining cash
and limits -> evaluate entries -> submit once -> verify statuses -> record events -> summary when due.

DRY_RUN is built with writer=None: OrderManager has no object able to POST an order (by construction)."""
import json, uuid
from datetime import date, timedelta
from . import stock as S, wheel as W, research as R
from .calendar_util import current_session, sessions, session_offset
from .journal import iso
from .ledger import Ledger
from .orders import OrderManager
from .risk import RiskState
from .alpaca import BrokerError, FeedNotEntitled

MODES = ("DRY_RUN", "AUTO_PAPER")


def account_matches(acct, expected):
    """Expected may be the account UUID (id) or the PA... account number; exact match, case-insensitive, trimmed."""
    if not acct or not expected:
        return False
    e = str(expected).strip().lower()
    return e in {str(acct.get("id") or "").strip().lower(), str(acct.get("account_number") or "").strip().lower()} - {""}


class Deps:
    """reader: PaperReader-like (GET only); writer: PaperWriter-like or None; md: MarketData-like."""
    def __init__(self, reader=None, writer=None, md=None, expected_account_id=None, research=None):
        self.reader, self.writer, self.md = reader, writer, md
        self.expected_account_id, self.research = expected_account_id, research


def _blocked(summary, why):
    summary["entry_blocks"].append(why)


def run_cycle(cfg, journal, mode, deps, root):
    if mode not in MODES:
        raise ValueError("mode must be DRY_RUN or AUTO_PAPER")
    if mode == "DRY_RUN" and deps.writer is not None:
        raise RuntimeError("DRY_RUN must not be given a writer")
    p = cfg.policy
    now = journal.clock()
    ledger = Ledger(journal, p)
    om = OrderManager(journal, cfg, writer=deps.writer if mode == "AUTO_PAPER" else None, ledger=ledger)
    summary = {"mode": mode, "utc": iso(now), "entry_blocks": [], "actions": [], "orders": [], "no_trade": True,
               "feed_profile": cfg.feed_profile, "option_quotes": p["data_settings"][cfg.feed_profile]["option_quotes_label"]}
    journal.event("CYCLE_START", {"mode": mode})
    risk = RiskState(journal, p)

    # schema pin (startup check repeated each cycle)
    ok, why, actual = R.schema_status(root, p)
    summary["schema"] = {"status": why, "actual_sha256": actual,
                         "signed_sha256": p["research"]["signed_schema_sha256"] or None}
    if not ok:
        _blocked(summary, why)

    r = deps.reader
    if r is None:
        _blocked(summary, "NO_BROKER_CONNECTION (credentials not configured)")
        journal.event("NO_TRADE", {"reasons": summary["entry_blocks"]})
        return summary

    # 1. account / orders / positions
    try:
        acct = r.account()
        if not account_matches(acct, deps.expected_account_id):
            journal.event("ACCOUNT_MISMATCH", {"got": (acct or {}).get("id"),
                                               "got_number": (acct or {}).get("account_number")})
            _blocked(summary, "ACCOUNT_ID_MISMATCH"); return summary
        if acct.get("status") != "ACTIVE" or acct.get("trading_blocked"):
            _blocked(summary, "ACCOUNT_NOT_ACTIVE")
        positions = r.positions() or []
        clock = r.clock()
        today = date.fromisoformat(clock["timestamp"][:10]).isoformat()
        cal = r.calendar((date.fromisoformat(today) - timedelta(days=420)).isoformat(),
                         (date.fromisoformat(today) + timedelta(days=90)).isoformat())
    except BrokerError as e:
        journal.event("BROKER_STATE_UNAVAILABLE", {"error": str(e)})
        risk.healthy(False)
        _blocked(summary, "BROKER_STATE_UNAVAILABLE")
        return summary

    # 2. reconcile ledger
    try:
        summary["reconciled"] = om.reconcile(r)
        acts = r.activities()
        healthy = True
    except BrokerError as e:
        journal.event("RECONCILE_FAILED", {"error": str(e)}); acts, healthy = [], False
    resume_ok = risk.healthy(healthy)
    if not resume_ok:
        _blocked(summary, "RECOVERY_NEEDS_3_HEALTHY_RECONCILIATIONS")
    sess = current_session(cal, now)
    equity = float(acct.get("equity") or 0)
    blocks = risk.update(equity, today, float(acct.get("last_equity") or 0))
    for b in blocks:
        _blocked(summary, b)
    journal.set("last_equity", equity)
    reconcile_wheel(journal, om, ledger, positions, acts, summary)
    try:
        cancel_stale_entries(cfg, journal, om, r, now, summary)
        reconcile_stock(cfg, journal, om, r, positions, today, summary)
        reprice_exits(cfg, journal, om, deps, now, summary)
    except BrokerError as e:
        journal.event("ORDER_MAINTENANCE_ERROR", {"error": str(e)})
        _blocked(summary, "ORDER_MAINTENANCE_ERROR")

    # 3. risk-reducing exits (always, even when entries are blocked)
    records = {}
    if deps.research:
        try:
            deps.research.poll(p["research"]["poll_min_interval_sec"])
        except Exception as e:  # research outage blocks entries only
            journal.event("RESEARCH_POLL_ERROR", {"error": str(e)})
    research_report = []
    records = R.load_records(journal, cfg, now, research_report)
    summary["research"] = research_report      # per file/record: ACCEPTED or BLOCKED with reason codes
    try:
        manage_stock_exits(cfg, journal, om, deps, positions, cal, sess, today, summary)
        manage_wheel(cfg, journal, om, deps, cal, sess, today, now, records, summary)
    except (BrokerError, FeedNotEntitled) as e:
        journal.event("EXIT_MANAGEMENT_DATA_ERROR", {"error": str(e)})
        _blocked(summary, "DATA_OUTAGE")

    # 4. entries
    if sess is None:
        _blocked(summary, "MARKET_CLOSED")
    if not summary["entry_blocks"]:
        try:
            stock_entries(cfg, journal, om, ledger, deps, cal, sess, today, now, records, summary)
            wheel_entries(cfg, journal, om, ledger, deps, cal, sess, today, now, records, summary)
        except (BrokerError, FeedNotEntitled) as e:
            journal.event("ENTRY_DATA_ERROR", {"error": str(e)})
            _blocked(summary, "DATA_OUTAGE")
    if summary["no_trade"]:
        journal.event("NO_TRADE", {"reasons": summary["entry_blocks"] or ["no qualifying signal"]})
    # 5. verify statuses
    if om.w:
        summary["post_submit"] = om.reconcile(r)
    return summary


ENTRY_PURPOSES = ("ENTRY", "SELL_PUT_OPEN", "SELL_CALL_OPEN")
EXIT_PURPOSES = ("STRATEGY_EXIT", "BUY_TO_CLOSE", "UNWIND_SELL", "FLATTEN")


def _age(journal, it, now):
    from datetime import datetime
    return (now - datetime.fromisoformat(it["created_utc"].replace("Z", "+00:00"))).total_seconds()


def cancel_stale_entries(cfg, journal, om, reader, now, summary, force=False):
    """Entry limits are DAY orders cancelled if unfilled after five minutes (never lowered)."""
    for it in om.open_intents():
        if it["purpose"] not in ENTRY_PURPOSES or not it["broker_order_id"]:
            continue
        limit = cfg.policy["stock" if it["strategy"] == "STOCK" else "wheel"]["entry_cancel_after_sec"]
        if force or _age(journal, it, now) >= limit:
            o = om.cancel_and_confirm(reader, it["broker_order_id"], it["client_order_id"])
            summary["actions"].append({"cancel_entry": it["client_order_id"], "confirmed": bool(o)})


def reconcile_stock(cfg, journal, om, reader, positions, today, summary):
    held = {x["symbol"]: float(x["qty"]) for x in positions if x.get("asset_class") == "us_equity"}
    for it in journal.db.execute("SELECT * FROM intents WHERE strategy='STOCK' AND purpose='ENTRY' AND filled_qty>0"):
        sym = it["symbol"]
        if not journal.db.execute("SELECT 1 FROM stock_positions WHERE symbol=?", (sym,)).fetchone() and sym in held:
            journal.db.execute("INSERT INTO stock_positions VALUES(?,?,?)", (sym, json.dumps(
                {"entry_session": it["created_utc"][:10], "entry_cid": it["client_order_id"],
                 "signal_ts": it["signal_ts"], "filled_qty": it["filled_qty"]}), iso(journal.clock())))
            journal.event("STOCK_POSITION_OPENED", {"qty": it["filled_qty"], "avg": it["filled_avg_price"]}, "STOCK", sym)
        if it["status"] in ("filled", "canceled", "expired"):
            o = reader.order_by_client_id(it["client_order_id"])
            if o and o.get("legs") is not None:
                gap = S.protection_gap(float(it["filled_qty"]), o["legs"])
                if not gap["ok"] and sym in held:
                    journal.event("PROTECTION_MISMATCH", gap, "STOCK", sym)
                    fix_protection(cfg, journal, om, reader, it, o, gap, summary)
    for row in journal.db.execute("SELECT symbol FROM stock_positions").fetchall():
        if row["symbol"] not in held:
            journal.db.execute("DELETE FROM stock_positions WHERE symbol=?", (row["symbol"],))
            journal.set(f"stock_exit_session:{row['symbol']}", today)
            journal.event("STOCK_POSITION_CLOSED", {}, "STOCK", row["symbol"])


def fix_protection(cfg, journal, om, reader, it, o, gap, summary):
    """Cancel mismatched legs (confirmed), then protect exactly the filled shares with an OCO."""
    for leg in o["legs"]:
        if leg.get("status") in ("new", "accepted", "held", "pending_new"):
            if om.cancel_and_confirm(reader, leg["id"]) is None:
                return
    body = json.loads(it["body"])
    res = om.place(strategy="STOCK", symbol=it["symbol"], purpose="PROTECT_OCO", signal_key=it["client_order_id"],
                   body={"symbol": it["symbol"], "qty": str(int(gap["filled"])), "side": "sell", "type": "limit",
                         "time_in_force": "gtc", "order_class": "oco",
                         "take_profit": body["take_profit"], "stop_loss": body["stop_loss"]}, reservations=[])
    summary["orders"].append(res)


def reprice_exits(cfg, journal, om, deps, now, summary):
    """Unfilled risk-reducing exits: cancel (confirmed) then reprice once per minute, max 3 attempts, at the
    fresh ask (buyback) / bid (share sale) within 1% of the current valid quote. Never report as closed."""
    po = cfg.policy["orders"]
    for it in om.open_intents():
        if it["purpose"] not in EXIT_PURPOSES or not it["broker_order_id"] or it["status"] == "UNKNOWN":
            continue
        if _age(journal, it, now) < po["exit_reprice_interval_sec"]:
            continue
        if it["attempt"] + 1 >= po["exit_max_attempts"]:
            journal.event("EXIT_STILL_UNFILLED_ALERT", {"client_order_id": it["client_order_id"]}, it["strategy"], it["symbol"])
            continue
        body = json.loads(it["body"])
        if len(it["symbol"]) > 10:
            sn = deps.md.option_snapshots([it["symbol"]]).get(it["symbol"]) or {}
            px = (sn.get("latestQuote") or {}).get("ap" if body["side"] == "buy" else "bp")
            px = px and (W.tick_round_up(float(px)) if body["side"] == "buy" else W.tick_round_down(float(px)))
        else:
            q = deps.md.latest_quotes([it["symbol"]]).get(it["symbol"]) or {}
            px = q.get("ap" if body["side"] == "buy" else "bp")
            px = px and S.round_px(float(px))
        if not px:
            continue
        o = om.cancel_and_confirm(deps.reader, it["broker_order_id"], it["client_order_id"])
        if o is None or o["status"] == "filled":
            continue
        remaining = float(body["qty"]) - float(o.get("filled_qty") or 0)
        if remaining <= 0:
            continue
        nb = {k: v for k, v in body.items() if k != "client_order_id"}
        nb.update(qty=str(int(remaining)), limit_price=f"{px:.2f}")
        res = om.place(strategy=it["strategy"], symbol=it["symbol"], purpose=it["purpose"],
                       signal_key=f"reprice:{it['client_order_id']}", body=nb, reservations=[], attempt=it["attempt"] + 1)
        summary["orders"].append(res)


# ---------------- STOCK ----------------
def manage_stock_exits(cfg, journal, om, deps, positions, cal, sess, today, summary):
    p = cfg.policy["stock"]
    held = {x["symbol"]: x for x in positions if x.get("asset_class") == "us_equity"}
    for row in journal.db.execute("SELECT * FROM stock_positions").fetchall():
        sym, data = row["symbol"], json.loads(row["data"])
        if sym not in held:
            continue
        if sess is None or not sess.in_window(journal.clock(), p["entry_window_start_min"], p["entry_window_end_min"]):
            continue
        bars = S.completed_bars(deps.md.daily_bars([sym], _bars_start(today)).get(sym, []), today)
        why = S.exit_due(bars, data["entry_session"], today, cal, p)
        if why:
            summary["actions"].append({"symbol": sym, "exit": why})
            # cancel/confirm existing bracket legs first so two sells cannot short
            for o in deps.reader.orders("open"):
                if o.get("symbol") == sym and o.get("side") == "sell":
                    if om.cancel_and_confirm(deps.reader, o["id"]) is None:
                        journal.event("EXIT_BLOCKED_UNCONFIRMED_CANCEL", {"order": o["id"]}, "STOCK", sym)
                        break
            else:
                q = deps.md.latest_quotes([sym]).get(sym)
                ok, _ = S.quote_ok(q, journal.clock(), p["quote_max_age_sec"], 1.0) if q else (False, "")
                if not ok:
                    journal.event("EXIT_NO_FRESH_QUOTE", {}, "STOCK", sym); continue
                qty = int(float(held[sym]["qty"]))
                res = om.place(strategy="STOCK", symbol=sym, purpose="STRATEGY_EXIT", signal_key=f"{today}:{why}",
                               body={"symbol": sym, "qty": str(qty), "side": "sell", "type": "limit",
                                     "time_in_force": "day", "limit_price": str(S.round_px(float(q["bp"]))),
                                     "position_intent": "sell_to_close"}, reservations=[])
                summary["orders"].append(res)
                journal.set(f"stock_exit_session:{sym}", today)


def _bars_start(today):
    return (date.fromisoformat(today) - timedelta(days=420)).isoformat()


def stock_entries(cfg, journal, om, ledger, deps, cal, sess, today, now, records, summary):
    p = cfg.policy["stock"]
    if not sess.in_window(now, p["entry_window_start_min"], p["entry_window_end_min"]):
        summary["stock"] = "outside 30-60 min entry window"; return
    syms = cfg.us_symbols
    bars = deps.md.daily_bars(syms + ["SPY"], _bars_start(today))
    spy_ok, spy = S.market_filter(S.completed_bars(bars.get("SPY", []), today), p)
    summary["spy_filter"] = spy
    if not spy_ok:
        summary["stock"] = "SPY below SMA200: no new stock positions"; return
    cands = []
    for sym in syms:
        rec = records.get(sym)
        if not rec or not rec[0]:
            journal.event("GATE_FAIL", {"reason": "research missing/stale/unverified", "detail": rec and rec[1]}, "STOCK", sym)
            continue
        if journal.get(f"stock_exit_session:{sym}") == today:
            continue
        if S.earnings_blocked(cal, today, rec[2].get("earnings_earliest"), rec[2].get("earnings_latest"), p):
            journal.event("GATE_FAIL", {"reason": "earnings window"}, "STOCK", sym); continue
        sig, why = S.signal(S.completed_bars(bars.get(sym, []), today), p)
        if not sig:
            continue
        sig["symbol"] = sym
        cands.append(sig)
    held = {r["symbol"] for r in journal.db.execute("SELECT symbol FROM stock_positions")}
    pending = {i["symbol"] for i in om.open_intents("STOCK")}
    slots = p["max_open_or_pending_names"] - len(held | pending)
    entries_today = journal.get(f"stock_entries:{today}", 0)
    picks = [c for c in S.rank(cands) if c["symbol"] not in held | pending]
    picks = picks[:max(0, min(p["max_new_entries_per_session"] - entries_today, slots))]
    base = min(cfg.policy["capital"]["initial"], float(journal.get("last_equity", cfg.policy["capital"]["initial"])))
    quotes = deps.md.latest_quotes([c["symbol"] for c in picks]) if picks else {}
    for c in picks:
        sym, q = c["symbol"], quotes.get(c["symbol"])
        ok, why = S.quote_ok(q, now, p["quote_max_age_sec"], p["max_spread_pct_of_mid"]) if q else (False, "no quote")
        if not ok:
            journal.event("GATE_FAIL", {"reason": why}, "STOCK", sym); continue
        ask = float(q["ap"])
        if abs(ask / c["signal_close"] - 1) > p["ask_band_pct"]:
            journal.event("GATE_FAIL", {"reason": "ask outside +/-3% of signal close"}, "STOCK", sym); continue
        asset = deps.reader.asset(sym)
        if not asset or not asset.get("tradable"):
            journal.event("GATE_FAIL", {"reason": "not tradable"}, "STOCK", sym); continue
        body, stop, target = S.bracket_body(sym, 1, ask, c["R"], p)

        qty = S.size(base, ask, c["R"], ledger.free_cash("STOCK", om), p)
        if qty <= 0:
            journal.event("GATE_FAIL", {"reason": "zero size"}, "STOCK", sym); continue
        body["qty"] = str(qty)
        res = om.place(strategy="STOCK", symbol=sym, purpose="ENTRY", signal_key=c["signal_bar_ts"], body=body,
                       reservations=[("STOCK", "ORDER_NOTIONAL", qty * ask)], signal_ts=c["signal_bar_ts"],
                       cash_check=lambda db, need=qty * ask: _cash_ok(db, ledger, "STOCK", need))
        res.update({"symbol": sym, "qty": qty, "stop": stop, "target": target, "R": c["R"], "atr14": c["atr14"]})
        summary["orders"].append(res)
        if res["status"] not in ("BLOCKED", "DUPLICATE"):
            summary["no_trade"] = False
            journal.set(f"stock_entries:{today}", journal.get(f"stock_entries:{today}", 0) + 1)


def _cash_ok(db, ledger, name, need):
    free = ledger.cash(name) - db.execute("SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' "
                                          "AND ledger=? AND kind!='CALL_SHARES'", (name,)).fetchone()["a"]
    return (free >= need, f"free {name} cash {free:.2f} < need {need:.2f}" if free < need else "ok")


# ---------------- WHEEL ----------------
def cycles(journal, active=True):
    rows = journal.db.execute("SELECT * FROM wheel_cycles").fetchall()
    out = [dict(r, data=json.loads(r["data"])) for r in rows]
    return [c for c in out if (c["state"] != "CLOSED") == active] if active is not None else out


def set_state(journal, c, event, **data):
    new = W.transition(c["state"], event)
    c["data"].update(data)
    journal.db.execute("UPDATE wheel_cycles SET state=?, data=?, updated_utc=? WHERE cycle_id=?",
                       (new, json.dumps(c["data"]), iso(journal.clock()), c["cycle_id"]))
    journal.event("WHEEL_TRANSITION", {"cycle": c["cycle_id"], "from": c["state"], "event": event, "to": new},
                  "WHEEL", c["symbol"])
    c["state"] = new
    return new


def reconcile_wheel(journal, om, ledger, positions, acts, summary):
    for c in cycles(journal):
        d, st = c["data"], c["state"]
        occ = d.get("option_symbol")
        if st in ("PUT_PENDING", "CALL_PENDING", "PUT_CLOSING", "CALL_CLOSING", "UNWINDING"):
            it = om.intent(d.get("pending_cid")) if d.get("pending_cid") else None
            if it and it["status"] == "filled":
                ev = {"PUT_PENDING": "PUT_FILLED", "CALL_PENDING": "CALL_FILLED", "PUT_CLOSING": "BUYBACK_FILLED",
                      "CALL_CLOSING": "BUYBACK_FILLED", "UNWINDING": "SHARES_SOLD"}[st]
                extra = {"open_premium": it["filled_avg_price"]} if ev in ("PUT_FILLED", "CALL_FILLED") else {}
                set_state(journal, c, ev, **extra)
                if ev == "BUYBACK_FILLED" and c["state"] == "CLOSED":
                    om.release_kind_for_symbol("WHEEL", "PUT_COLLATERAL", c["symbol"], "put closed (confirmed fill)")
                if ev == "BUYBACK_FILLED" and c["state"] == "SHARES_HELD":
                    om.release_kind_for_symbol("WHEEL", "CALL_SHARES", c["symbol"], "call closed (confirmed fill)")
            elif it and it["status"] in ("canceled", "expired", "rejected") and _follow_reprice(journal, c, it):
                continue
            elif it and it["status"] in ("canceled", "expired", "rejected") and not it["filled_qty"]:
                ev = {"PUT_PENDING": "PUT_UNFILLED_CANCELED", "CALL_PENDING": "CALL_UNFILLED_CANCELED",
                      "PUT_CLOSING": "BUYBACK_UNFILLED", "CALL_CLOSING": "BUYBACK_UNFILLED",
                      "UNWINDING": "SHARES_SALE_UNFILLED"}[st]
                set_state(journal, c, ev)
                if ev == "PUT_UNFILLED_CANCELED":
                    om.release_kind_for_symbol("WHEEL", "PUT_COLLATERAL", c["symbol"], "put entry unfilled")
                if ev == "CALL_UNFILLED_CANCELED":
                    om.release_kind_for_symbol("WHEEL", "CALL_SHARES", c["symbol"], "call entry unfilled")
            if st not in ("PUT_CLOSING", "CALL_CLOSING"):
                continue
        if c["state"] in ("PUT_OPEN", "CALL_OPEN", "PUT_CLOSING", "CALL_CLOSING", "ASSIGNMENT_PENDING",
                          "CALL_ASSIGNMENT_PENDING") and occ:
            out = W.classify_option_outcome(occ, True, positions, acts)
            if out == "STILL_OPEN":
                continue
            if out == "OPTION_POSITION_GONE_UNEXPLAINED" and c["state"] in ("ASSIGNMENT_PENDING", "CALL_ASSIGNMENT_PENDING"):
                journal.event("ASSIGNMENT_ACTIVITY_PENDING", {"cycle": c["cycle_id"]}, "WHEEL", c["symbol"])
                continue
            was_put = c["state"] in ("PUT_OPEN", "PUT_CLOSING", "ASSIGNMENT_PENDING")
            if out == "ASSIGNMENT_CONFIRMED":
                shares = sum(float(x["qty"]) for x in positions if x["symbol"] == c["symbol"])
                if was_put and shares < 100:
                    journal.event("ASSIGNMENT_SHARES_NOT_YET_VISIBLE", {}, "WHEEL", c["symbol"])
                    if c["state"] != "ASSIGNMENT_PENDING":
                        set_state(journal, c, "OPTION_POSITION_GONE_UNEXPLAINED")
                    continue
                ref = f"ASSIGN-{occ}"
                k = d["strike"]
                if was_put:
                    ledger.post("WHEEL", c["symbol"], -k * 100, "put_assignment_buy_100", ref)
                    om.release_kind_for_symbol("WHEEL", "PUT_COLLATERAL", c["symbol"], "assignment confirmed")
                    set_state(journal, c, "ASSIGNMENT_CONFIRMED", acquisition_price=k, option_symbol=None)
                else:
                    ledger.post("WHEEL", c["symbol"], k * 100, "call_assignment_deliver_100", ref)
                    om.release_kind_for_symbol("WHEEL", "CALL_SHARES", c["symbol"], "call assignment confirmed")
                    set_state(journal, c, "ASSIGNMENT_CONFIRMED", option_symbol=None)
            elif out == "EXPIRED_CONFIRMED":
                set_state(journal, c, "EXPIRED_CONFIRMED", option_symbol=None)
                kind = "PUT_COLLATERAL" if was_put else "CALL_SHARES"
                om.release_kind_for_symbol("WHEEL", kind, c["symbol"], "expiry confirmed")
            else:
                set_state(journal, c, out)   # reserves stay locked


def _follow_reprice(journal, c, it):
    """If a cancelled exit was replaced by a repriced attempt, track the replacement instead."""
    from .orders import client_order_id  # noqa
    row = journal.db.execute("SELECT * FROM intents WHERE symbol=? AND purpose=? AND attempt>? ORDER BY attempt DESC",
                             (it["symbol"], it["purpose"], it["attempt"])).fetchone()
    if row:
        c["data"]["pending_cid"] = row["client_order_id"]
        journal.db.execute("UPDATE wheel_cycles SET data=? WHERE cycle_id=?", (json.dumps(c["data"]), c["cycle_id"]))
        return True
    return False


def manage_wheel(cfg, journal, om, deps, cal, sess, today, now, records, summary):
    p = cfg.policy["wheel"]
    act = cycles(journal)
    if not act or sess is None:
        return
    for c in act:
        d = c["data"]
        rec = records.get(c["symbol"], (False, [], {}))[2]
        q = deps.md.latest_quotes([c["symbol"]]).get(c["symbol"]) or {}
        mid = (float(q["ap"]) + float(q["bp"])) / 2 if q.get("ap") and q.get("bp") else None
        ob = oa = None
        if d.get("option_symbol"):
            sn = deps.md.option_snapshots([d["option_symbol"]]).get(d["option_symbol"]) or {}
            lq = sn.get("latestQuote") or {}
            ob, oa = lq.get("bp"), lq.get("ap")
        nxt = session_offset(cal, today, 1)
        pre_exdiv = isinstance(rec.get("dividend"), dict) and nxt == rec["dividend"].get("ex_date")
        acts = W.management_actions(c, now_session_date=today, calendar=cal, stock_mid=mid, opt_bid=ob, opt_ask=oa,
                                    earnings_earliest=rec.get("earnings_earliest"), dividend=rec.get("dividend"),
                                    distress=rec.get("distress_flag", False), p=p,
                                    closing_session_preceding_exdiv=pre_exdiv)
        for a, why in acts:
            summary["actions"].append({"cycle": c["cycle_id"], "symbol": c["symbol"], "action": a, "why": why})
            journal.event("WHEEL_ACTION", {"action": a, "why": why, "cycle": c["cycle_id"]}, "WHEEL", c["symbol"])
            if a == "BLOCK_ADDITIONS":
                journal.set(f"wheel_block:{c['symbol']}", today)
            if a == "BUY_TO_CLOSE" and c["state"] in ("PUT_OPEN", "CALL_OPEN") and oa:
                res = om.place(strategy="WHEEL", symbol=d["option_symbol"], purpose="BUY_TO_CLOSE",
                               signal_key=f"{c['cycle_id']}:{today}:{why}",
                               body={"symbol": d["option_symbol"], "qty": "1", "side": "buy", "type": "limit",
                                     "time_in_force": "day", "limit_price": str(W.tick_round_up(float(oa))),
                                     "position_intent": "buy_to_close"}, reservations=[])
                summary["orders"].append(res)
                if res["status"] not in ("BLOCKED", "DUPLICATE", "DRY_RUN"):
                    set_state(journal, c, "BUYBACK_SUBMITTED", pending_cid=res["client_order_id"])
                break


def wheel_entries(cfg, journal, om, ledger, deps, cal, sess, today, now, records, summary):
    p = cfg.policy["wheel"]
    if not sess.in_window(now, p["entry_start_min_after_open"], stop_before_close_min=p["entry_stop_min_before_close"]):
        summary["wheel"] = "outside wheel entry window"; return
    base = min(cfg.policy["capital"]["initial"], float(journal.get("last_equity", cfg.policy["capital"]["initial"])))
    act = cycles(journal)
    # covered calls for SHARES_HELD
    for c in act:
        if c["state"] != "SHARES_HELD":
            continue
        rec = records.get(c["symbol"])
        if not rec or not rec[0] or not rec[2].get("wheel_ok") or rec[2].get("dividend") == "UNKNOWN" \
                or journal.get(f"wheel_block:{c['symbol']}"):
            journal.event("GATE_FAIL", {"reason": "call blocked (research/dividend/loss block)"}, "WHEEL", c["symbol"])
            continue
        _sell_option(cfg, journal, om, deps, cal, today, now, c["symbol"], rec[2], "call", c, base, summary)
    # new puts
    active_syms = {c["symbol"] for c in act}
    if len(act) >= p["max_active_cycles"]:
        return
    bars = deps.md.daily_bars(list(p["universe"]), _bars_start(today))
    ranked = sorted(p["universe"], key=lambda s: (-(S.pct_return([b["c"] for b in S.completed_bars(bars.get(s, []), today)],
                                                                   cfg.policy["stock"]["rank_lookback"]) or -1e9), s))
    for sym in ranked:
        if sym in active_syms or len(cycles(journal)) >= p["max_active_cycles"]:
            continue
        rec = records.get(sym)
        if not rec or not rec[0] or not rec[2].get("wheel_ok") or journal.get(f"wheel_block:{sym}"):
            journal.event("GATE_FAIL", {"reason": "wheel card/research not usable",
                                        "detail": rec and (rec[1] + rec[2].get("wheel_reasons", []))}, "WHEEL", sym)
            continue
        if _sell_option(cfg, journal, om, deps, cal, today, now, sym, rec[2], "put", None, base, summary,
                        bars=S.completed_bars(bars.get(sym, []), today)):
            break   # one new option sale per cycle


def _sell_option(cfg, journal, om, deps, cal, today, now, sym, rec, kind, cyc, base, summary, bars=None):
    p = cfg.policy["wheel"]
    q = deps.md.latest_quotes([sym]).get(sym)
    ok, why = S.quote_ok(q, now, p["quote_max_age_sec"], 1.0) if q else (False, "no stock quote")
    if not ok:
        journal.event("GATE_FAIL", {"reason": why}, "WHEEL", sym); return False
    bid, ask = float(q["bp"]), float(q["ap"])
    mid = (bid + ask) / 2
    sma50 = None
    if kind == "put":
        from .indicators import sma
        sma50 = sma([b["c"] for b in bars], cfg.policy["stock"]["sma_fast"]) if bars else None
        if sma50 is None:
            journal.event("GATE_FAIL", {"reason": "no completed SMA50"}, "WHEEL", sym); return False
    t0 = date.fromisoformat(today)
    contracts = deps.reader.option_contracts(sym, (t0 + timedelta(days=p["dte_min"])).isoformat(),
                                             (t0 + timedelta(days=p["dte_max"])).isoformat(), kind)
    snaps = deps.md.option_snapshots([c["symbol"] for c in contracts]) if contracts else {}
    best, rej = W.screen_contracts(kind, sym, contracts, snaps, spot_mid=mid, spot_bid=bid, spot_ask=ask, sma50=sma50,
                                   today=today, now=now, calendar=cal, earnings_earliest=rec.get("earnings_earliest"),
                                   latest_release=rec.get("latest_release_date"), p=p,
                                   acq_price=cyc["data"].get("acquisition_price") if cyc else None)
    if not best:
        journal.event("NO_CONTRACT", {"kind": kind, "rejections": rej[:50]}, "WHEEL", sym); return False
    limit = W.tick_round_down(best["bid"])
    if limit <= 0:
        return False
    if kind == "put":
        need = W.put_collateral(best["strike"], p)
        notional = best["strike"] * 100
        if notional > p["max_pct_per_name"] * base:
            journal.event("GATE_FAIL", {"reason": "strike notional > 20% base"}, "WHEEL", sym); return False

        def check(db):
            used = db.execute("SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND ledger='WHEEL'"
                              " AND kind='PUT_COLLATERAL'").fetchone()["a"]
            used += sum(100 * (c["data"].get("acquisition_price") or 0) for c in cycles(journal)
                        if c["state"] not in ("CASH", "PUT_PENDING", "PUT_OPEN", "PUT_CLOSING", "ASSIGNMENT_PENDING"))
            if used + notional > p["max_pct_combined"] * base:
                return False, "combined strike exposure > 40% base"
            return _cash_ok(db, om.ledger, "WHEEL", need)
        cid_res = om.place(strategy="WHEEL", symbol=best["symbol"], purpose="SELL_PUT_OPEN",
                           signal_key=f"{today}:{best['symbol']}", cash_check=check,
                           body={"symbol": best["symbol"], "qty": "1", "side": "sell", "type": "limit",
                                 "time_in_force": "day", "limit_price": f"{limit:.2f}", "position_intent": "sell_to_open"},
                           reservations=[("WHEEL", "PUT_COLLATERAL", need, sym)])
    else:
        def check(db):
            held = db.execute("SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND "
                              "kind='CALL_SHARES' AND symbol=?", (sym,)).fetchone()["a"]
            shares = sum(float(x["qty"]) for x in deps.reader.positions() if x["symbol"] == sym)
            return (shares - held >= 100, "share coverage: <100 free shares")
        cid_res = om.place(strategy="WHEEL", symbol=best["symbol"], purpose="SELL_CALL_OPEN",
                           signal_key=f"{today}:{best['symbol']}", cash_check=check,
                           body={"symbol": best["symbol"], "qty": "1", "side": "sell", "type": "limit",
                                 "time_in_force": "day", "limit_price": f"{limit:.2f}", "position_intent": "sell_to_open"},
                           reservations=[("WHEEL", "CALL_SHARES", 100, sym)])
    cid_res.update({"underlying": sym, "contract": best})
    summary["orders"].append(cid_res)
    if cid_res["status"] in ("BLOCKED", "DUPLICATE"):
        return False
    summary["no_trade"] = False
    if cid_res["status"] == "DRY_RUN":
        return True
    if kind == "put":
        cid = uuid.uuid4().hex[:12]
        journal.db.execute("INSERT INTO wheel_cycles VALUES(?,?,?,?,?,?)",
                           (cid, sym, "CASH", json.dumps({}), iso(now), iso(now)))
        cyc = {"cycle_id": cid, "symbol": sym, "state": "CASH", "data": {}}
        set_state(journal, cyc, "PUT_SUBMITTED", option_symbol=best["symbol"], strike=best["strike"],
                  expiry=best["expiry"], pending_cid=cid_res["client_order_id"], first_put_stock_mid=mid,
                  reserved=need)
    else:
        set_state(journal, cyc, "CALL_SUBMITTED", option_symbol=best["symbol"], strike=best["strike"],
                  expiry=best["expiry"], pending_cid=cid_res["client_order_id"])
    return True
