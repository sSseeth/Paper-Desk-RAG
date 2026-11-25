"""JSON and Markdown reports for a backtest run.

The files are the backtest's own output. They are not the paper journal and
they are not the paper performance series.
"""

import json
import os

from sakibdesk.backtest.pricing import (
    DIVIDEND_YIELD,
    ENTRY_HALF_SPREAD,
    RISK_FREE,
    RV_LOOKBACK,
    STOCK_HALF_SPREAD,
    VOL_FLOOR,
)

JSON_NAME = "backtest_report.json"
MD_NAME = "backtest_report.md"
# Wheel refusals after a contract passed the screen, plus a stock entry whose
# live floor (or the policy overlay on top of that floor) cannot buy one share.
WHEEL_CAPITAL_REASONS = ("notional_cap", "insufficient_cash", "combined_cap")
STOCK_CAPITAL_REASONS = ("insufficient_capital",)

ASSUMPTIONS = [
    "Wheel premiums are European Black-Scholes estimates on realised volatility. "
    "They are not historical option quotes. Every wheel figure is ESTIMATED.",
    f"Realised volatility is the annualised sample standard deviation of the last "
    f"{RV_LOOKBACK} log returns, floored at {VOL_FLOOR:.0%} and capped at 300%.",
    f"The rate is {RISK_FREE:.0%} and the dividend yield is {DIVIDEND_YIELD:.0%}. "
    "A zero dividend yield understates put premiums and overstates call premiums "
    "relative to a name that pays a dividend.",
    "No implied-vol premium over realised vol is assumed. The IV haircut reduces "
    "the Black-Scholes premium before the spread. The default haircut is 10%.",
    f"The option bid/ask is {ENTRY_HALF_SPREAD:.1%} each side of that haircut mid "
    "(5% full width), inside the policy 10% maximum. Sales use wheel.tick_round_down "
    "on the bid. Buybacks use wheel.tick_round_up on the ask.",
    f"Stock quotes are not historical NBBO. The open is the decision price, with a "
    f"{STOCK_HALF_SPREAD:.2%} half-spread. Slippage is then applied against the "
    "fill. The default is 10 bps.",
    "A daily bar that touches both the stop and the target is filled at the stop. "
    "A gap through the stop fills at the open. A target is a limit: a gap through "
    "it fills at the target, not at the open.",
    "Short puts are assigned early when the European value does not exceed "
    "intrinsic. Calls are assigned at expiry when the close is above the strike. "
    "With a zero dividend yield a call is not early-assigned. At expiry, a put "
    "is assigned when the close is below the strike.",
    "Historical verified research cards are not in the cache. The live runner "
    "would take no new trade without one. This backtest passes synthetic fields "
    "so the price rules can run, and it says so.",
    "Historical earnings dates are not in the cache. The synthetic earnings date "
    "is outside the contract window, so the live earnings blackout does not "
    "remove trades. Trade counts are an upper bound relative to a run that had "
    "real earnings dates.",
    "Dividends are assumed VERIFIED_NONE, so the covered-call dividend block does "
    "not fire and early call assignment from a dividend is not modelled.",
    "Open interest is set to the policy minimum and dated on the decision day, "
    "so the open-interest gate does not reject the chain for missing data. Live "
    "trading skips thin strikes. The contract set is wider than a real chain.",
    "Listed strikes are approximated by a price-banded grid (0.50, 1, 2.50, 5, "
    "or 10). The grid is not the historical chain.",
    "The asset tradable flag is not replayed. A symbol with a bar is treated as "
    "tradable.",
    "One decision is taken per session, inside each book's entry window. "
    "Stock uses 45 minutes after the open. The wheel uses 120 minutes after the "
    "open. Stops and targets are treated as resting all day.",
    "Shares are whole. Quantity is stock.size, which floors, and the fee loop "
    "only steps down by one share. The wheel sells one contract. There is no "
    "fractional share and no fractional contract.",
    "Commissions are the policy overlays: one currency unit per stock "
    "transaction and per option side, including expiry and assignment. The "
    "overlay is not scaled down at a small account. Slippage is basis points "
    "of the fill, so the dollar amount scales with price. A fill that cannot "
    "pay one whole share plus the overlay is skipped and counted. A skipped "
    "order is not charged. Alpaca paper fills are not used.",
    "Base for the percent limits is min(scenario starting capital, last marked "
    "equity). The percents are the policy percents. Sleeve cash starts at the "
    "policy stock/wheel ratio of the scenario capital. policy.json is not edited.",
    "The 10% experiment drawdown block and the 2% daily-loss block follow "
    "risk.RiskState. The experiment block stays on. Exits still run.",
    "A symbol the stock book holds, or bought earlier that session, is not "
    "eligible for a new wheel put that session.",
    "Walk-forward is a chronological 60/40 split with a cold start on each "
    "side. Parameters are not searched and are not refit.",
    "Bars are split-adjusted, matching the policy, and are not dividend-adjusted. "
    "Buy-and-hold comparisons use the same prices, so dividend income is missing "
    "on both sides.",
]

LIMITATIONS = [
    "Survivorship bias: the universe is the watchlist chosen for this "
    "repository, fixed as of today. Names that failed or left the theme before "
    "that choice are absent. Names that listed during the sample cannot be "
    "traded before their first bar, and the equal-weight benchmark keeps only "
    "names with a price on the first session.",
    "Estimated option prices. Free data does not provide a usable historical "
    "option chain. Black-Scholes with a realised-vol haircut is not the "
    "premium a short put or call would have traded at.",
    "Idealised fills. The daily bar does not show the quote inside the entry "
    "window, the queue, or partial fills. Same-day fills at the modelled bid "
    "or ask are an assumption.",
    "This is not a forecast and it is not financial advice. Paper and backtest "
    "results are not live results.",
]


def pct(value):
    if value is None:
        return "n/a"
    return f"{value * 100:.2f}%"


def num(value):
    if value is None:
        return "n/a"
    return f"{value:.2f}"


def money(value):
    if value is None:
        return "n/a"
    return f"{value:,.2f}"


def capital_skips(run):
    """Skipped entries that failed for capital, by book and by symbol.

    Stock: the live size floor, or the policy overlay on that floor, left no
    whole share. Wheel: the screen accepted a contract and cash or a notional
    cap then refused it. Counts are for this run's starting capital.
    """
    stock = wheel = 0
    by_symbol = {}
    for symbol, reasons in (run.get("skips") or {}).items():
        s = sum(int(reasons.get(name) or 0) for name in STOCK_CAPITAL_REASONS)
        w = sum(int(reasons.get(name) or 0) for name in WHEEL_CAPITAL_REASONS)
        stock += s
        wheel += w
        if s or w:
            by_symbol[symbol] = {"stock": s, "wheel": w, "total": s + w}
    return {"stock": stock, "wheel": wheel, "total": stock + wheel, "by_symbol": by_symbol}


def headline_row(run):
    metrics = run["metrics"]["combined"]
    bench = run.get("benchmarks") or {}
    return {
        "capital": run["capital"],
        "total_return": metrics["total_return"],
        "annualised_return": metrics["annualised_return"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "sortino": metrics["sortino"],
        "win_rate": metrics["win_rate"],
        "trades": metrics["trades"],
        "worst_month": metrics["worst_month"],
        "worst_month_return": metrics["worst_month_return"],
        "spy_return": (bench.get("spy") or {}).get("total_return"),
        "equal_weight_return": (bench.get("equal_weight") or {}).get("total_return"),
        "capital_skips": capital_skips(run),
        "put_assignments": run["assignments"]["put"],
        "call_assignments": run["assignments"]["call"],
    }


def render_markdown(report):
    """Human-readable report. Wheel numbers stay labelled ESTIMATED."""
    lines = []
    lines.append("# Historical backtest (ESTIMATED)")
    lines.append("")
    lines.append(
        "This file is the backtest report. It is not the paper journal and it "
        "is not the paper performance series. Wheel option prices are "
        "**ESTIMATED**. They are not historical quotes.")
    lines.append("")
    lines.append(
        f"Config hash `{report['config_hash']}` and strategy "
        f"`{report['strategy_version']}`. policy.json and watchlist.json were "
        "not modified.")
    lines.append("")
    lines.append(
        f"Window requested {report['start']} to {report['end']}. "
        f"Sessions in the primary path depend on the cached bars.")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append(
        "Combined account, both sleeves, default costs unless a run says "
        "otherwise. Total return is the marked equity, including open option "
        "liability. Win rate uses closed trades only. Premium is not a win at "
        "entry. SPY and equal-weight are buy-and-hold over the same sessions.")
    lines.append("")
    lines.append(
        "| Capital | Total | Ann. | Max DD | Sharpe | Sortino | Win rate | "
        "Trades | Worst month | SPY | Equal-weight | Capital skips |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for run in report["runs"]:
        row = headline_row(run)
        worst = row["worst_month"] or "n/a"
        if row["worst_month_return"] is not None:
            worst = f"{worst} ({pct(row['worst_month_return'])})"
        skipped = row["capital_skips"]["total"]
        lines.append(
            f"| {money(row['capital'])} | {pct(row['total_return'])} | "
            f"{pct(row['annualised_return'])} | {pct(row['max_drawdown'])} | "
            f"{num(row['sharpe'])} | {num(row['sortino'])} | {pct(row['win_rate'])} | "
            f"{row['trades']} | {worst} | {pct(row['spy_return'])} | "
            f"{pct(row['equal_weight_return'])} | {skipped} |")
    lines.append("")
    lines.extend(_capital_skip_section(report))
    lines.append("## Books")
    lines.append("")
    lines.append(
        "Sleeve returns use that sleeve's starting cash. The combined return "
        "uses the whole account, including an idle sleeve when a book is off.")
    lines.append("")
    lines.append(
        "| Capital | Book | Total | Ann. | Max DD | Sharpe | Sortino | "
        "Win rate | Trades | Assignments |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for run in report["runs"]:
        for name in ("stock", "wheel", "combined"):
            metrics = run["metrics"][name]
            if name == "wheel":
                assigned = run["assignments"]["put"] + run["assignments"]["call"]
            elif name == "combined":
                assigned = run["assignments"]["put"] + run["assignments"]["call"]
            else:
                assigned = 0
            lines.append(
                f"| {money(run['capital'])} | {name} | {pct(metrics['total_return'])} | "
                f"{pct(metrics['annualised_return'])} | {pct(metrics['max_drawdown'])} | "
                f"{num(metrics['sharpe'])} | {num(metrics['sortino'])} | "
                f"{pct(metrics['win_rate'])} | {metrics['trades']} | {assigned} |")
    lines.append("")
    primary = _primary(report)
    if primary:
        lines.extend(_notional_section(primary))
        lines.extend(_symbol_section(primary))
        lines.extend(_worst_trades(primary))
    if report.get("walk_forward"):
        lines.extend(_walk_section(report["walk_forward"]))
    if report.get("sensitivity"):
        lines.extend(_sensitivity_section(report["sensitivity"]))
    lines.append("## Assumptions")
    lines.append("")
    for item in report["assumptions"]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Limitations")
    lines.append("")
    for item in report["limitations"]:
        lines.append(f"- {item}")
    lines.append("")
    coverage = report.get("coverage") or {}
    missing = coverage.get("missing_symbols") or []
    if missing:
        lines.append("Symbols with no cached bars, so they were not traded: " + ", ".join(missing) + ".")
        lines.append("")
    lines.append(
        "Alerts-only names on the watchlist are not in either book: "
        + ", ".join(coverage.get("alerts_only") or []) + ".")
    lines.append("")
    return "\n".join(lines)


def _primary(report):
    runs = report.get("runs") or []
    anchor = report.get("anchor_capital")
    for run in runs:
        if run["capital"] == anchor:
            return run
    return runs[-1] if runs else None


def _capital_skip_section(report):
    """One row per starting capital. Empty runs still render a zero."""
    lines = ["## Capital skips by starting capital", ""]
    lines.append(
        "A skip is an entry the rules reached and then refused because the "
        "account could not fund it. Stock: `stock.size` floored to no share, "
        "or one share plus the policy overlay did not fit in the sleeve. "
        "Wheel: the estimated contract passed the screen, then cash or the "
        "notional cap refused it. Skipped orders are not charged a fee.")
    lines.append("")
    lines.append("| Capital | Stock whole-share skips | Wheel capital skips | Total |")
    lines.append("| --- | --- | --- | --- |")
    for run in report.get("runs") or []:
        skipped = capital_skips(run)
        lines.append(
            f"| {money(run.get('capital'))} | {skipped['stock']} | "
            f"{skipped['wheel']} | {skipped['total']} |")
    if not report.get("runs"):
        lines.append("| n/a | 0 | 0 | 0 |")
    lines.append("")
    return lines


def _notional_section(run):
    lines = ["## Wheel capital", ""]
    lines.append(
        "Minimum capital to open one contract is 100 times the strike the live "
        "screen selected. The figure is **ESTIMATED** because the strike comes "
        "from the estimated chain. A day with no passing contract is omitted. "
        "Capital skips are entries the screen accepted and the cash or the "
        "policy notional cap then refused. They are not the same as a day the "
        "screen found nothing.")
    lines.append("")
    lines.append("| Symbol | Days with a contract | Min 100×strike | Median | Max | Wheel capital skips |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    skipped = capital_skips(run)
    by_symbol = skipped["by_symbol"]
    for symbol, stats in sorted((run.get("notionals") or {}).items()):
        wheel_n = (by_symbol.get(symbol) or {}).get("wheel", 0)
        lines.append(
            f"| {symbol} | {stats['days']} | {money(stats['min'])} | "
            f"{money(stats['median'])} | {money(stats['max'])} | {wheel_n} |")
    lines.append("")
    lines.append(
        f"Wheel entries skipped for capital in this run: {skipped['wheel']}. "
        f"Stock entries skipped because one whole share did not fit: {skipped['stock']}.")
    lines.append("")
    if run.get("experiment_block_date"):
        lines.append(
            f"The 10% experiment drawdown block turned on {run['experiment_block_date']} "
            "and stayed on. New entries after that date were refused. Exits still ran.")
        lines.append("")
    return lines


def _symbol_section(run):
    lines = ["## Per symbol", ""]
    lines.append(
        "Closed-trade P&L. Wheel P&L is **ESTIMATED** and is booked only when "
        "the cycle closes. Open marks are not in the win rate.")
    lines.append("")
    lines.append("| Symbol | Stock trades | Stock P&L | Wheel cycles | Wheel P&L | Put assigns | Call assigns |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in run.get("per_symbol") or []:
        lines.append(
            f"| {row['symbol']} | {row['stock_trades']} | {money(row['stock_pnl'])} | "
            f"{row['wheel_cycles']} | {money(row['wheel_pnl'])} | "
            f"{row['put_assignments']} | {row['call_assignments']} |")
    lines.append("")
    return lines


def _worst_trades(run):
    closed = [t for t in run.get("trades") or [] if t.get("pnl") is not None]
    closed.sort(key=lambda t: t["pnl"])
    lines = ["## Worst closed trades", ""]
    if not closed:
        lines.append("No closed trades.")
        lines.append("")
        return lines
    lines.append("| Book | Symbol | Opened | Closed | P&L | Reason |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for trade in closed[:5]:
        label = "ESTIMATED" if trade.get("estimated") else trade.get("reason")
        reason = trade.get("reason") or ""
        if trade.get("estimated"):
            reason = f"{reason} (ESTIMATED)".strip()
        lines.append(
            f"| {trade['book']} | {trade['symbol']} | {trade['opened']} | "
            f"{trade.get('closed')} | {money(trade['pnl'])} | {reason or label} |")
    lines.append("")
    return lines


def _walk_section(walk):
    lines = ["## Walk-forward", ""]
    lines.append(walk.get("note") or "")
    lines.append("")
    lines.append("| Slice | Sessions | Total | Ann. | Max DD | Sharpe | Trades | Win rate |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for name in ("in_sample", "out_of_sample"):
        block = walk.get(name)
        if not block:
            continue
        metrics = block["metrics"]["combined"]
        lines.append(
            f"| {name} | {block['sessions']} | {pct(metrics['total_return'])} | "
            f"{pct(metrics['annualised_return'])} | {pct(metrics['max_drawdown'])} | "
            f"{num(metrics['sharpe'])} | {metrics['trades']} | {pct(metrics['win_rate'])} |")
    lines.append("")
    return lines


def _sensitivity_section(rows):
    lines = ["## Sensitivity", ""]
    lines.append(
        "Same rules, anchor capital, varying stock slippage and the IV haircut. "
        "This is not a search for a better parameter. The shipped policy does "
        "not change.")
    lines.append("")
    lines.append("| Slippage bps | IV haircut | Total | Max DD | Sharpe | Trades | Capital skips |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in rows:
        lines.append(
            f"| {row['slippage_bps']:g} | {row['iv_haircut']:.0%} | "
            f"{pct(row['total_return'])} | {pct(row['max_drawdown'])} | "
            f"{num(row['sharpe'])} | {row['trades']} | {row['capital_skips']} |")
    lines.append("")
    return lines


def dump_json(report):
    return json.dumps(report, indent=2, allow_nan=False, default=str)


def write_report(directory, report):
    """Write only the two backtest report files. Creates `directory`."""
    os.makedirs(directory, exist_ok=True)
    json_path = os.path.join(directory, JSON_NAME)
    md_path = os.path.join(directory, MD_NAME)
    with open(json_path, "w", encoding="utf-8") as handle:
        handle.write(dump_json(report))
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_markdown(report))
    return json_path, md_path
