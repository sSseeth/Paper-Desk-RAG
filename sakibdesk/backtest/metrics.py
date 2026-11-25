"""Return, drawdown, and benchmark statistics for one backtest path.

Premium is not treated as a win at entry. Win rate uses closed trades only.
Drawdown uses the marked equity curve, which does include the open option
liability.
"""

import math

from sakibdesk import accounting


def _returns(equities):
    out = []
    for prev, cur in zip(equities, equities[1:]):
        if prev:
            out.append(cur / prev - 1.0)
    return out


def sharpe(rets):
    """Annualised Sharpe versus zero. Sample standard deviation. None if undefined."""
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    if var <= 0:
        return None
    return mean / math.sqrt(var) * math.sqrt(252.0)


def sortino(rets):
    """Annualised Sortino versus zero.

    Downside deviation uses every return, with positive returns contributing
    zero. None when there is no downside or fewer than two returns.
    """
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    down = sum(min(r, 0.0) ** 2 for r in rets) / len(rets)
    if down <= 0:
        return None
    return mean / math.sqrt(down) * math.sqrt(252.0)


def annualised(start_equity, end_equity, start_date, end_date):
    """Compound annual growth from calendar days between the two dates."""
    from datetime import date
    if start_equity <= 0 or end_equity <= 0:
        return None
    days = (date.fromisoformat(end_date) - date.fromisoformat(start_date)).days
    if days <= 0:
        return None
    years = days / 365.25
    return (end_equity / start_equity) ** (1.0 / years) - 1.0


def worst_month(dates, equities):
    """Worst month-end return. The first month is measured from the first point.

    `dates[0]` / `equities[0]` is the starting capital. Later points are
    session closes. Returns (month, return) or (None, None).
    """
    if len(equities) < 2:
        return None, None
    month_end = {}
    for day, equity in zip(dates[1:], equities[1:]):
        month_end[day[:7]] = equity
    prev = equities[0]
    worst_m, worst_r = None, None
    for month in sorted(month_end):
        equity = month_end[month]
        ret = (equity / prev - 1.0) if prev else None
        if ret is not None and (worst_r is None or ret < worst_r):
            worst_m, worst_r = month, ret
        prev = equity
    return worst_m, worst_r


def summarise(start_equity, start_date, curve, trades):
    """Summary statistics for one equity curve and its closed trades.

    `curve` is a list of {date, equity} session closes. The start point is
    prepended so the first day's move is included. An empty curve, a missing
    mark, or a non-positive starting equity does not raise.
    """
    if not start_equity or start_equity < 0:
        start_equity = 0.0
    dates = [start_date]
    equities = [float(start_equity)]
    for point in curve or []:
        dates.append(point.get("date") or start_date)
        equity = point.get("equity")
        if equity is None:
            equity = equities[-1]
        equities.append(float(equity))
    end_equity = equities[-1]
    total = (end_equity / start_equity - 1.0) if start_equity else None
    rets = _returns(equities)
    closed = [t for t in trades if t.get("pnl") is not None]
    wins = [t for t in closed if t["pnl"] > 0]
    losses = [t for t in closed if t["pnl"] < 0]
    flat = [t for t in closed if t["pnl"] == 0]
    month, month_ret = worst_month(dates, equities)
    end_date = dates[-1]
    return {
        "start_equity": round(start_equity, 2),
        "end_equity": round(end_equity, 2),
        "total_return": total,
        "annualised_return": annualised(start_equity, end_equity, start_date, end_date),
        "max_drawdown": accounting.max_drawdown(equities),
        "sharpe": sharpe(rets),
        "sortino": sortino(rets),
        "win_rate": (len(wins) / len(closed)) if closed else None,
        "trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(flat),
        "open_trades": sum(1 for t in trades if t.get("pnl") is None),
        "worst_month": month,
        "worst_month_return": month_ret,
        "avg_trade_pnl": (sum(t["pnl"] for t in closed) / len(closed)) if closed else None,
        "worst_trade_pnl": min((t["pnl"] for t in closed), default=None),
    }


def round_trip(capital, entry, exit_px, cost):
    """Buy-and-hold of one name, entry and exit both charged `cost`.

    Quantity is the live whole-share floor. A sleeve that cannot buy one
    share keeps its cash and is not charged.
    """
    if not capital or capital <= 0 or not entry or entry <= 0:
        return {"qty": 0, "end_equity": capital, "total_return": None}
    held = accounting.buy_and_hold(capital, entry, cost)
    qty = held["qty"] if held["qty"] and held["qty"] > 0 else 0
    if qty <= 0:
        return {"qty": 0, "end_equity": round(float(capital), 2), "total_return": 0.0}
    end = accounting.bh_value({"qty": qty, "cash": held["cash"]}, exit_px)
    end = round(end - cost, 2)
    return {"qty": qty, "end_equity": end, "total_return": end / capital - 1.0}


def equal_weight(capital, names, cost):
    """Equal-weight buy-and-hold.

    `names` is a list of {symbol, entry, exit}. Names without a price are not
    passed in. Leftover cash stays inside each name's buy-and-hold result.
    """
    if not names or capital <= 0:
        return {"symbols": [], "end_equity": None, "total_return": None, "excluded": []}
    each = capital / float(len(names))
    end = 0.0
    rows = []
    for name in names:
        entry = name.get("entry") or 0.0
        exit_px = name.get("exit")
        if exit_px is None:
            exit_px = entry
        held = round_trip(each, entry, exit_px, cost)
        end += held["end_equity"] if held["end_equity"] is not None else 0.0
        rows.append({"symbol": name["symbol"], **held})
    return {
        "symbols": rows,
        "end_equity": round(end, 2),
        "total_return": end / capital - 1.0,
    }


def excess(strategy_return, benchmark_return):
    """Difference of total returns. This is not a beta-adjusted alpha."""
    if strategy_return is None or benchmark_return is None:
        return None
    return strategy_return - benchmark_return
