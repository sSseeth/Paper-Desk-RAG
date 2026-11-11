"""WHEEL strategy (guide p8-9): contract screen, state machine and management decisions (pure functions)."""
from datetime import date, datetime
from .calendar_util import sessions_between, session_offset
from .stock import parse_rfc3339

STATES = ("CASH", "PUT_PENDING", "PUT_OPEN", "PUT_CLOSING", "ASSIGNMENT_PENDING", "SHARES_HELD", "CALL_PENDING",
          "CALL_OPEN", "CALL_CLOSING", "CALL_ASSIGNMENT_PENDING", "UNWINDING", "CLOSED")
TRANSITIONS = {
    ("CASH", "PUT_SUBMITTED"): "PUT_PENDING",
    ("PUT_PENDING", "PUT_FILLED"): "PUT_OPEN",
    ("PUT_PENDING", "PUT_UNFILLED_CANCELED"): "CLOSED",
    ("PUT_OPEN", "BUYBACK_SUBMITTED"): "PUT_CLOSING",
    ("PUT_CLOSING", "BUYBACK_FILLED"): "CLOSED",
    ("PUT_CLOSING", "BUYBACK_UNFILLED"): "PUT_OPEN",
    ("PUT_OPEN", "EXPIRED_CONFIRMED"): "CLOSED",
    ("PUT_OPEN", "OPTION_POSITION_GONE_UNEXPLAINED"): "ASSIGNMENT_PENDING",
    ("PUT_CLOSING", "OPTION_POSITION_GONE_UNEXPLAINED"): "ASSIGNMENT_PENDING",
    ("PUT_OPEN", "ASSIGNMENT_CONFIRMED"): "SHARES_HELD",
    ("PUT_CLOSING", "ASSIGNMENT_CONFIRMED"): "SHARES_HELD",
    ("ASSIGNMENT_PENDING", "ASSIGNMENT_CONFIRMED"): "SHARES_HELD",
    ("ASSIGNMENT_PENDING", "EXPIRED_CONFIRMED"): "CLOSED",
    ("SHARES_HELD", "CALL_SUBMITTED"): "CALL_PENDING",
    ("CALL_PENDING", "CALL_FILLED"): "CALL_OPEN",
    ("CALL_PENDING", "CALL_UNFILLED_CANCELED"): "SHARES_HELD",
    ("CALL_OPEN", "BUYBACK_SUBMITTED"): "CALL_CLOSING",
    ("CALL_CLOSING", "BUYBACK_FILLED"): "SHARES_HELD",
    ("CALL_CLOSING", "BUYBACK_UNFILLED"): "CALL_OPEN",
    ("CALL_OPEN", "EXPIRED_CONFIRMED"): "SHARES_HELD",
    ("CALL_OPEN", "OPTION_POSITION_GONE_UNEXPLAINED"): "CALL_ASSIGNMENT_PENDING",
    ("CALL_CLOSING", "OPTION_POSITION_GONE_UNEXPLAINED"): "CALL_ASSIGNMENT_PENDING",
    ("CALL_OPEN", "ASSIGNMENT_CONFIRMED"): "CLOSED",
    ("CALL_ASSIGNMENT_PENDING", "ASSIGNMENT_CONFIRMED"): "CLOSED",
    ("CALL_ASSIGNMENT_PENDING", "EXPIRED_CONFIRMED"): "SHARES_HELD",
    ("SHARES_HELD", "UNWIND_SUBMITTED"): "UNWINDING",
    ("UNWINDING", "SHARES_SOLD"): "CLOSED",
    ("UNWINDING", "SHARES_SALE_UNFILLED"): "SHARES_HELD",
}


import re as _re
_OCC = _re.compile(r"^([A-Z][A-Z0-9.]{0,5})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


def parse_occ(symbol):
    """Parse an OCC option symbol (ROOT + YYMMDD + C/P + strike*1000 as 8 digits), e.g. MU261204P00150000.
    Returns dict(underlying, type, expiry, strike) or None if the symbol is not a valid OCC symbol."""
    m = _OCC.match(str(symbol or "").replace(" ", ""))
    if not m:
        return None
    root, yy, mm, dd, cp, k = m.groups()
    try:
        exp = date(2000 + int(yy), int(mm), int(dd)).isoformat()
    except ValueError:
        return None
    return {"underlying": root, "type": "call" if cp == "C" else "put", "expiry": exp, "strike": int(k) / 1000.0}


class InvalidTransition(Exception):
    pass


def transition(state, event):
    k = (state, event)
    if k not in TRANSITIONS:
        raise InvalidTransition(f"{state} --{event}--> ?")
    return TRANSITIONS[k]


def classify_option_outcome(occ_symbol, held_before, positions, activities):
    """Decide what happened to a short option using ONLY broker positions + REST activities.
    Never infer assignment from price. Returns one of: STILL_OPEN, ASSIGNMENT_CONFIRMED, EXPIRED_CONFIRMED,
    OPTION_POSITION_GONE_UNEXPLAINED, CLOSED_BY_FILL (handled by order reconciliation)."""
    if any(p["symbol"] == occ_symbol and float(p["qty"]) != 0 for p in positions):
        return "STILL_OPEN"
    acts = [a for a in activities if a.get("symbol") == occ_symbol]
    if any(a.get("activity_type") in ("OPASN", "OPXRC") for a in acts):
        return "ASSIGNMENT_CONFIRMED"
    if any(a.get("activity_type") == "OPEXP" for a in acts):
        return "EXPIRED_CONFIRMED"
    return "OPTION_POSITION_GONE_UNEXPLAINED"   # keep reserves locked; activity may arrive next day


def is_regular_monthly(exp, calendar_dates):
    d = date.fromisoformat(exp)
    if 15 <= d.day <= 21 and d.weekday() == 4:
        return True
    # third-Friday holiday -> Thursday expiry
    if 14 <= d.day <= 20 and d.weekday() == 3:
        fri = date.fromordinal(d.toordinal() + 1).isoformat()
        return fri not in calendar_dates
    return False


def standard_contract(c, underlying):
    if str(c.get("multiplier")) not in ("100", "100.0") or str(c.get("size", "100")) not in ("100", "100.0"):
        return False, "multiplier/size not 100"
    if c.get("style") != "american":
        return False, "not american-style"
    if c.get("root_symbol") != underlying or c.get("underlying_symbol") != underlying:
        return False, "adjusted/nonstandard root"
    if not c.get("tradable", False):
        return False, "not tradable"
    dl = c.get("deliverables")
    if dl is not None:
        if len(dl) != 1 or dl[0].get("type") != "equity" or dl[0].get("symbol") != underlying \
                or float(dl[0].get("amount", 0)) != 100 or dl[0].get("settlement_method") not in (None, "", "CCC", "BTOB"):
            return False, "nonstandard deliverable"
    return True, "ok"


def screen_contracts(kind, underlying, contracts, snaps, *, spot_mid, spot_bid, spot_ask, sma50, today, now,
                     calendar, earnings_earliest, latest_release, p, acq_price=None):
    """Return (best contract dict or None, list of (symbol, reason) rejections)."""
    rej, ok = [], []
    if earnings_earliest in (None, "UNKNOWN"):
        return None, [("*", "earnings date unknown")]
    if latest_release in (None, "UNKNOWN"):
        return None, [("*", "latest release unknown")]
    if latest_release <= today and sessions_between(calendar, latest_release, today) <= p["earnings_wait_sessions_after"]:
        return None, [("*", "within 2 sessions after earnings release")]
    cal_dates = {d["date"] for d in calendar}
    ceiling = min(p["put_ceiling_mid_pct"] * spot_mid, sma50) if kind == "put" else None
    for c in contracts:
        sym = c["symbol"]
        std, why = standard_contract(c, underlying)
        if not std:
            rej.append((sym, why)); continue
        if c.get("type") != kind:
            continue
        exp = c["expiration_date"]
        dte = (date.fromisoformat(exp) - date.fromisoformat(today)).days
        if not (p["dte_min"] <= dte <= p["dte_max"]):
            rej.append((sym, "dte")); continue
        if exp >= earnings_earliest:
            rej.append((sym, "expiry on/after next earnings")); continue
        k = float(c["strike_price"])
        if kind == "put" and not (k < spot_bid and k < spot_mid and k <= ceiling):
            rej.append((sym, "strike above ceiling/spot")); continue
        if kind == "call" and not (k > spot_ask and k > spot_mid and acq_price is not None and k >= acq_price):
            rej.append((sym, "call strike not above spot / below acquisition")); continue
        oi, oid = c.get("open_interest"), c.get("open_interest_date")
        if oi is None or oid is None or float(oi) < p["min_open_interest"]:
            rej.append((sym, "open interest")); continue
        if sessions_between(calendar, oid, today) > p["oi_max_age_sessions"]:
            rej.append((sym, "open interest stale")); continue
        s = snaps.get(sym) or {}
        q, g = s.get("latestQuote") or {}, s.get("greeks") or {}
        delta = g.get("delta")
        if delta is None:
            rej.append((sym, "missing greeks")); continue
        lo, hi = (p["put_delta_min"], p["put_delta_max"]) if kind == "put" else (p["call_delta_min"], p["call_delta_max"])
        if not (lo <= delta <= hi):
            rej.append((sym, "delta")); continue
        try:
            bid, ask, qt = float(q["bp"]), float(q["ap"]), parse_rfc3339(q["t"])
        except Exception:
            rej.append((sym, "no quote")); continue
        if bid <= 0 or ask < bid:
            rej.append((sym, "bid<=0 or crossed")); continue
        mid = (bid + ask) / 2
        if (ask - bid) / mid > p["max_spread_pct_of_mid"]:
            rej.append((sym, "spread")); continue
        if (now - qt).total_seconds() > p["quote_max_age_sec"]:
            rej.append((sym, "quote stale")); continue
        ok.append(dict(symbol=sym, strike=k, expiry=exp, dte=dte, delta=delta, bid=bid, ask=ask, mid=mid,
                       spread=ask - bid, monthly=is_regular_monthly(exp, cal_dates), oi=float(oi), oi_date=oid,
                       quote_t=q["t"], ceiling=ceiling))
    ok.sort(key=lambda x: (not x["monthly"], abs(x["dte"] - p["dte_target"]),
                           abs(abs(x["delta"]) - p["delta_target_abs"]), x["spread"], x["symbol"]))
    return (ok[0] if ok else None), rej


def put_collateral(strike, p):
    return strike * 100 + p["put_reserve_cost_allowance_per_contract"]


def tick_round_down(px):
    t = 0.05 if px < 3 else 0.10   # conservative standard (non-penny) increments
    return round(int(px / t + 1e-9) * t, 2)


def tick_round_up(px):
    import math
    t = 0.05 if px < 3 else 0.10
    return round(math.ceil(px / t - 1e-9) * t, 2)


def management_actions(cycle, *, now_session_date, calendar, stock_mid, opt_bid, opt_ask, earnings_earliest,
                       dividend, distress, p, closing_session_preceding_exdiv=False):
    """Return list of (action, reason) for an active cycle. Risk-reducing only."""
    acts = []
    st, d = cycle["state"], cycle["data"]
    short = st in ("PUT_OPEN", "CALL_OPEN")
    if short and opt_ask is not None and opt_ask <= p["take_profit_ask_pct_of_open"] * d["open_premium"]:
        acts.append(("BUY_TO_CLOSE", "50% premium captured (ask <= 50% of opening premium)"))
    if short and earnings_earliest not in (None, "UNKNOWN") and earnings_earliest <= d["expiry"]:
        acts.append(("BUY_TO_CLOSE", "earnings now overlaps expiry"))
    if short and earnings_earliest in (None, "UNKNOWN"):
        acts.append(("FLAG", "earnings date unknown while short option open"))
    if st == "PUT_OPEN" and stock_mid is not None and stock_mid <= (1 - p["put_unwind_pct_below_strike"]) * d["strike"]:
        acts.append(("BUY_TO_CLOSE", "stock 20% below put strike"))
    acq = d.get("acquisition_price")
    if acq and stock_mid is not None:
        if stock_mid <= (1 - p["unwind_drawdown_pct"]) * acq:
            acts.append(("BUY_TO_CLOSE" if st == "CALL_OPEN" else "SELL_SHARES", "20% below acquisition: unwind"))
        elif stock_mid <= (1 - p["warn_drawdown_pct"]) * acq:
            acts.append(("BLOCK_ADDITIONS", "10% below acquisition"))
    if distress:
        acts.append(("BUY_TO_CLOSE" if short else ("SELL_SHARES" if st == "SHARES_HELD" else "FLAG"),
                     "verified going-concern/bankruptcy flag"))
    if st == "CALL_OPEN" and dividend not in (None, "UNKNOWN") and closing_session_preceding_exdiv \
            and opt_bid is not None and opt_ask is not None and stock_mid is not None:
        intrinsic = max(0.0, stock_mid - d["strike"])
        extrinsic = max(0.0, (opt_bid + opt_ask) / 2 - intrinsic)
        if intrinsic > 0 and float(dividend.get("amount", 0)) > extrinsic:
            acts.append(("BUY_TO_CLOSE", "ITM call: dividend exceeds remaining time value"))
    if short:
        dte = (date.fromisoformat(d["expiry"]) - date.fromisoformat(now_session_date)).days
        if dte in p["notify_dte"]:
            acts.append(("NOTIFY", f"{dte} days to expiry"))
    # unwind ordering: never sell shares while a short call remains
    if st == "CALL_OPEN":
        acts = [a for a in acts if a[0] != "SELL_SHARES"]
    return acts
