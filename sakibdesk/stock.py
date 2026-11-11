"""STOCK strategy (guide p7). Pure functions + an executor that talks to OrderManager."""
import math
from datetime import datetime, timedelta, timezone
from .indicators import sma, atr_wilder, pct_return
from .calendar_util import session_offset, sessions_between


def parse_rfc3339(s):
    import re
    s = s.replace("Z", "+00:00")
    s = re.sub(r"\.(\d{6})\d+", r".\1", s)
    return datetime.fromisoformat(s)


def completed_bars(bars, session_date):
    """Only bars dated strictly before the current session date (completed)."""
    return [b for b in bars if b["t"][:10] < session_date]


def signal(bars, p):
    """bars: completed split-adjusted daily bars ending 'yesterday'. Returns dict or None + reason."""
    valid = [b for b in bars if all(isinstance(b.get(k), (int, float)) and b[k] > 0 for k in ("o", "h", "l", "c"))]
    if len(valid) < p["min_valid_bars"]:
        return None, f"only {len(valid)} valid bars (<{p['min_valid_bars']})"
    closes = [b["c"] for b in valid]
    y = valid[-1]
    prior_high = max(b["h"] for b in valid[-1 - p["breakout_lookback"]:-1])
    s50, s200 = sma(closes, p["sma_fast"]), sma(closes, p["sma_slow"])
    if not (y["c"] > prior_high):
        return None, "no 20-session breakout"
    if not (y["c"] > s50 > s200):
        return None, "close > SMA50 > SMA200 fails"
    atr = atr_wilder(valid, p["atr_period"])
    return {"signal_close": y["c"], "signal_bar_ts": y["t"], "sma50": s50, "sma200": s200, "atr14": atr,
            "R": p["r_atr_mult"] * atr, "ret63": pct_return(closes, p["rank_lookback"]),
            "prior_high": prior_high}, "ok"


def market_filter(spy_bars, p):
    closes = [b["c"] for b in spy_bars]
    s200 = sma(closes, p["sma_slow"])
    return (s200 is not None and closes[-1] > s200), {"spy_close": closes[-1] if closes else None, "spy_sma200": s200}


def earnings_blocked(calendar, today, earliest, latest, p):
    if not earliest or earliest == "UNKNOWN":
        return True
    start = session_offset(calendar, earliest, -p["earnings_block_sessions_before"])
    end = session_offset(calendar, latest or earliest, p["earnings_block_sessions_after"])
    if start is None or end is None:
        return True  # calendar does not reach: conservative block
    return start <= today <= end


def rank(cands):
    return sorted(cands, key=lambda c: (-(c["ret63"] if c["ret63"] is not None else -1e9), c["symbol"]))


def size(base, entry, R, free_cash, p):
    if entry <= 0 or R <= 0:
        return 0
    return max(0, math.floor(min(p["max_pct_per_company"] * base / entry, p["risk_pct_per_entry"] * base / R,
                                 free_cash / entry)))


def quote_ok(q, now, max_age, max_spread):
    try:
        bid, ask = float(q["bp"]), float(q["ap"])
        t = parse_rfc3339(q["t"])
    except Exception:
        return False, "quote missing/unparseable"
    if bid <= 0 or ask <= 0 or ask < bid:
        return False, "quote not positive two-sided"
    if (now - t).total_seconds() > max_age:
        return False, "quote stale"
    mid = (bid + ask) / 2
    if (ask - bid) / mid > max_spread:
        return False, "spread too wide"
    return True, "ok"


def round_px(x):
    return round(x + 1e-9, 2) if x >= 1 else round(x, 4)


def bracket_body(symbol, qty, entry, R, p):
    stop, target = round_px(entry - R), round_px(entry + p["target_r_mult"] * R)
    return {"symbol": symbol, "qty": str(qty), "side": "buy", "type": "limit", "time_in_force": "day",
            "limit_price": str(round_px(entry)), "order_class": "bracket",
            "take_profit": {"limit_price": str(target)}, "stop_loss": {"stop_price": str(stop)},
            "position_intent": "buy_to_open"}, stop, target


def exit_due(bars_completed, entry_session, today, calendar, p):
    closes = [b["c"] for b in bars_completed]
    s50 = sma(closes, p["sma_fast"])
    if s50 is not None and closes[-1] < s50:
        return "CLOSE_BELOW_SMA50"
    if sessions_between(calendar, entry_session, bars_completed[-1]["t"][:10]) + 1 >= p["max_holding_sessions"]:
        return "MAX_HOLDING_20_SESSIONS"
    return None


def protection_gap(filled_qty, legs):
    """Partial fill coverage: active protective legs must cover exactly the filled shares."""
    active = [l for l in legs if l.get("status") in ("new", "accepted", "held", "pending_new", "partially_filled")]
    stop_q = sum(float(l["qty"]) - float(l.get("filled_qty") or 0) for l in active if l.get("type") in ("stop", "stop_limit"))
    tp_q = sum(float(l["qty"]) - float(l.get("filled_qty") or 0) for l in active if l.get("type") == "limit")
    return {"filled": filled_qty, "stop_cover": stop_q, "target_cover": tp_q,
            "ok": abs(stop_q - filled_qty) < 1e-9 and abs(tp_q - filled_qty) < 1e-9}
