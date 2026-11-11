"""Page 19 accounting: conservative marks, net equity, benchmarks, cost overlay. Zero interest on idle cash."""
import math


def conservative_equity(cash, stock_positions, short_options, accrued_costs=0.0):
    """stock_positions: [{qty, bid}]  short_options: [{qty(contracts>0), ask}] -> net equity.
    Missing bid/ask => None (caller must report UNMARKED, never assume zero liability)."""
    v = cash - accrued_costs
    for p in stock_positions:
        if p.get("bid") is None:
            return None
        v += p["qty"] * p["bid"]
    for o in short_options:
        if o.get("ask") is None:
            return None
        v -= o["qty"] * 100 * o["ask"]
    return round(v, 2)


def premium_open(cash, liabilities, premium_per_share, contracts):
    """Premium = cash in + an equal liability; equity unchanged at open."""
    amt = premium_per_share * 100 * contracts
    return cash + amt, liabilities + amt


def buy_and_hold(capital, entry_price, cost_per_txn=1.0):
    qty = math.floor((capital - cost_per_txn) / entry_price) if entry_price > 0 else 0
    return {"qty": qty, "cash": round(capital - qty * entry_price - (cost_per_txn if qty else 0), 2)}


def bh_value(bh, price, dividends_per_share=0.0):
    return round(bh["cash"] + bh["qty"] * (price + dividends_per_share), 2)


def spy_benchmarks(spy_start, capital):
    return {name: buy_and_hold(capital[k], spy_start) for name, k in
            (("COMBINED_vs_SPY", "initial"), ("STOCK_vs_SPY", "stock"), ("WHEEL_vs_SPY", "wheel"))}


def wheel_vs_same_stock(cycles):
    """cycles: [{reserved, first_put_entry_px, end_px, start, end}] non-overlapping cash use is enforced by
    summing per cycle only (each cycle's capital is its own reservation)."""
    out = []
    for c in cycles:
        bh = buy_and_hold(c["reserved"], c["first_put_entry_px"])
        out.append({"cycle": c.get("cycle_id"), "reserved": c["reserved"], "bh_end_value": bh_value(bh, c["end_px"]),
                    "bh_pnl": round(bh_value(bh, c["end_px"]) - c["reserved"], 2)})
    return out


def cost_overlay(option_contract_sides, stock_txns, p):
    return round(option_contract_sides * p["overlay_per_option_contract_per_side"]
                 + stock_txns * p["overlay_per_stock_transaction"], 2)


def max_drawdown(series):
    hi, mdd = -1e18, 0.0
    for v in series:
        hi = max(hi, v)
        if hi > 0:
            mdd = max(mdd, (hi - v) / hi)
    return round(mdd, 6)
