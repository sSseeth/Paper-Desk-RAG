"""`desk.py backtest` command.

Reads the CSV cache, optionally filling it from Alpaca market data, runs the
simulator, and writes a JSON report and a Markdown report. It does not open
the paper journal and it does not write the paper performance series.
"""

import os
from datetime import date, timedelta

from sakibdesk.backtest import metrics as M
from sakibdesk.backtest.data import CredentialsMissing, fetch_cache, load_cache
from sakibdesk.backtest.candidates import fetch_symbols, resolve_strategies
from sakibdesk.backtest.compare import run_comparison, write_comparison
from sakibdesk.backtest.engine import BAR_LOOKBACK_DAYS, IN_SAMPLE_FRACTION, run_backtest, split_sessions
from sakibdesk.backtest.pricing import ENTRY_HALF_SPREAD, STOCK_HALF_SPREAD
from sakibdesk.backtest.report import (
    ASSUMPTIONS,
    JSON_NAME,
    LIMITATIONS,
    MD_NAME,
    capital_skips,
    headline_row,
    write_report,
)
from sakibdesk.config import Config
from sakibdesk.secrets_store import redact

# The sensitivity grid is a robustness check. It is not a fit.
GRID_SLIPPAGE_BPS = (0.0, 10.0, 25.0)
GRID_IV_HAIRCUT = (0.0, 0.10, 0.25)


def default_capitals(policy_initial):
    """Starting capitals the report covers, including the configured paper capital.

    The low levels ($100, $300, $500, $1,000) are there so a small account is
    visible. They use the same percents and the same whole-share sizing.
    """
    values = [100.0, 300.0, 500.0, 1000.0, 2000.0, 5000.0, 10000.0, 25000.0, float(policy_initial)]
    out = []
    for value in values:
        if value not in out:
            out.append(value)
    return out


def parse_capitals(text, policy_initial):
    if not text:
        return default_capitals(policy_initial)
    out = []
    for part in str(text).split(","):
        part = part.strip()
        if not part:
            continue
        value = float(part)
        if value <= 0:
            raise ValueError("capital must be positive")
        if value not in out:
            out.append(value)
    if not out:
        raise ValueError("capital list is empty")
    return out


def parse_books(text):
    parts = [p.strip().lower() for p in str(text).split(",") if p.strip()]
    if not parts:
        raise ValueError("books must be stock, wheel, or both")
    bad = [p for p in parts if p not in ("stock", "wheel")]
    if bad:
        raise ValueError("books must be stock, wheel, or both")
    # Stable order, no duplicates.
    return tuple(dict.fromkeys(parts))


def symbols_for(cfg, specs=None):
    if specs:
        return fetch_symbols(cfg, specs)
    names = list(cfg.us_symbols)
    if "SPY" not in names:
        names.append("SPY")
    return names


def _price(hist, day, field):
    if hist is None:
        return None
    bar = hist.on(day)
    if bar is None:
        return None
    return bar.o if field == "o" else bar.c


def _last_close(hist, end):
    if hist is None:
        return None, None
    chosen = None
    for bar in hist.bars:
        if bar.date <= end:
            chosen = bar
        else:
            break
    if chosen is None:
        return None, None
    return chosen.c, chosen.date


def _benchmarks(raw, histories, cfg):
    start, end = raw["start"], raw["end"]
    cost = float(cfg.policy["costs"]["overlay_per_stock_transaction"])
    spy_hist = histories.get("SPY")
    spy_entry = _price(spy_hist, start, "o")
    spy_exit, spy_exit_date = _last_close(spy_hist, end)
    spy = None
    if spy_entry and spy_exit:
        spy = M.round_trip(raw["capital"], spy_entry, spy_exit, cost)
        spy["entry"] = spy_entry
        spy["exit"] = spy_exit
        spy["exit_date"] = spy_exit_date
        spy["note"] = "Buy and hold SPY from the first session open to the last available close."

    def basket(symbols, capital):
        rows, excluded = [], []
        for symbol in symbols:
            hist = histories.get(symbol)
            entry = _price(hist, start, "o")
            exit_px, exit_date = _last_close(hist, end)
            if entry is None or exit_px is None:
                excluded.append(symbol)
                continue
            rows.append({"symbol": symbol, "entry": entry, "exit": exit_px, "exit_date": exit_date})
        result = M.equal_weight(capital, rows, cost)
        result["excluded"] = excluded
        return result

    equal = basket(cfg.us_symbols, raw["capital"])
    equal["note"] = (
        "Equal-weight buy-and-hold of stock-eligible names that have a bar on "
        "the first session. Names without that bar are excluded and listed.")
    out = {
        "spy": spy,
        "equal_weight": equal,
        "spy_on_stock_sleeve": None,
        "spy_on_wheel_sleeve": None,
        "equal_weight_wheel_names": basket(list(cfg.wheel_symbols), raw["wheel_capital"]),
    }
    if spy_entry and spy_exit:
        out["spy_on_stock_sleeve"] = M.round_trip(raw["stock_capital"], spy_entry, spy_exit, cost)
        out["spy_on_wheel_sleeve"] = M.round_trip(raw["wheel_capital"], spy_entry, spy_exit, cost)
    combined = raw["metrics"]["combined"]["total_return"] if raw.get("metrics") else None
    # Metrics are attached by the caller before excess is filled. Compute from
    # the curves if metrics are not there yet.
    return out


def _per_symbol(raw, cfg):
    rows = []
    symbols = list(dict.fromkeys(list(cfg.us_symbols) + list(cfg.wheel_symbols)))
    for symbol in symbols:
        stock_closed = [t for t in raw["trades"]
                        if t["book"] == "STOCK" and t["symbol"] == symbol and t.get("pnl") is not None]
        wheel_closed = [t for t in raw["trades"]
                        if t["book"] == "WHEEL" and t["symbol"] == symbol and t.get("pnl") is not None]
        puts = sum(t.get("put_assignments") or 0 for t in raw["trades"] if t["symbol"] == symbol)
        calls = sum(t.get("call_assignments") or 0 for t in raw["trades"] if t["symbol"] == symbol)
        puts += sum(p.get("put_assignments") or 0 for p in raw["open_positions"]
                    if p["symbol"] == symbol)
        calls += sum(p.get("call_assignments") or 0 for p in raw["open_positions"]
                     if p["symbol"] == symbol)
        rows.append({
            "symbol": symbol,
            "stock_trades": len(stock_closed),
            "stock_pnl": round(sum(t["pnl"] for t in stock_closed), 2),
            "wheel_cycles": len(wheel_closed),
            "wheel_pnl": round(sum(t["pnl"] for t in wheel_closed), 2),
            "put_assignments": puts,
            "call_assignments": calls,
        })
    return rows


def decorate(raw, histories, cfg):
    """Add metrics, benchmarks, and the per-symbol table. Does not write a file."""
    start = raw["start"]
    combined = [{"date": p["date"], "equity": p["equity"]} for p in raw["curve"]]
    stock_curve = [{"date": p["date"], "equity": p["stock_equity"]} for p in raw["curve"]]
    wheel_curve = [{"date": p["date"], "equity": p["wheel_equity"]} for p in raw["curve"]]
    stock_trades = [t for t in raw["trades"] if t["book"] == "STOCK"]
    wheel_trades = [t for t in raw["trades"] if t["book"] == "WHEEL"]
    raw["metrics"] = {
        "combined": M.summarise(raw["capital"], start, combined, raw["trades"]),
        "stock": M.summarise(raw["stock_capital"], start, stock_curve, stock_trades),
        "wheel": M.summarise(raw["wheel_capital"], start, wheel_curve, wheel_trades),
    }
    raw["benchmarks"] = _benchmarks(raw, histories, cfg)
    strategy = raw["metrics"]["combined"]["total_return"]
    bench = raw["benchmarks"]
    bench["excess_vs_spy"] = M.excess(strategy, (bench["spy"] or {}).get("total_return"))
    bench["excess_vs_equal_weight"] = M.excess(
        strategy, bench["equal_weight"].get("total_return"))
    bench["note"] = (
        "Excess is the strategy total return minus the benchmark total return. "
        "It is not a beta-adjusted alpha.")
    raw["per_symbol"] = _per_symbol(raw, cfg)
    return raw


def _slim(raw):
    """Sensitivity / walk-forward row without the equity curve or every trade."""
    metrics = raw["metrics"]["combined"]
    return {
        "capital": raw["capital"],
        "slippage_bps": raw["slippage_bps"],
        "iv_haircut": raw["iv_haircut"],
        "start": raw["start"],
        "end": raw["end"],
        "sessions": raw["sessions"],
        "total_return": metrics["total_return"],
        "annualised_return": metrics["annualised_return"],
        "max_drawdown": metrics["max_drawdown"],
        "sharpe": metrics["sharpe"],
        "sortino": metrics["sortino"],
        "win_rate": metrics["win_rate"],
        "trades": metrics["trades"],
        "worst_month": metrics["worst_month"],
        "worst_month_return": metrics["worst_month_return"],
        "capital_skips": capital_skips(raw)["total"],
        "stock_capital_skips": capital_skips(raw)["stock"],
        "wheel_capital_skips": capital_skips(raw)["wheel"],
        "assignments": raw["assignments"],
        "metrics": raw["metrics"],
    }


def _run(histories, cfg, **kwargs):
    raw = run_backtest(histories, cfg, **kwargs)
    return decorate(raw, histories, cfg)


def _guard_output(output_dir):
    path = os.path.abspath(os.path.normpath(output_dir))
    if os.path.basename(path) == "journal.sqlite3" or path.endswith(".sqlite3"):
        raise ValueError("refusing to write the backtest onto a journal database")
    return path


def run_cli(root, args):
    """Entry point used by desk.py. Returns a process exit code."""
    try:
        cfg = Config(root)
    except Exception as exc:
        print(f"CONFIG INVALID: {exc}")
        return 3
    try:
        capitals = parse_capitals(getattr(args, "capital", None), cfg.policy["capital"]["initial"])
        books = parse_books(getattr(args, "books", "stock,wheel"))
        start = args.start
        end = args.end
        date.fromisoformat(start)
        date.fromisoformat(end)
        if end < start:
            raise ValueError("end is before start")
    except ValueError as exc:
        print(f"backtest refused: {exc}")
        return 2

    try:
        output_dir = _guard_output(args.output_dir or os.path.join(root, "reports", "backtest"))
    except ValueError as exc:
        print(f"backtest refused: {exc}")
        return 2
    cache_dir = os.path.abspath(args.cache_dir or os.path.join(root, "data", "backtest_cache"))
    slippage = float(args.slippage_bps)
    haircut = float(args.iv_haircut)
    fractional = bool(getattr(args, "fractional", False))
    dd_reset_flag = bool(getattr(args, "dd_reset", False))
    try:
        specs = resolve_strategies(getattr(args, "strategies", None))
    except ValueError as exc:
        print(f"backtest refused: {exc}")
        return 2
    names = symbols_for(cfg, specs)

    if args.fetch:
        warmup = (date.fromisoformat(start) - timedelta(days=BAR_LOOKBACK_DAYS)).isoformat()
        print(
            f"Fetching split, raw, and dividend-adjusted daily bars {warmup} to {end} "
            f"into {cache_dir}")
        try:
            written, empty = fetch_cache(root, names, warmup, end, cache_dir)
        except CredentialsMissing as exc:
            print(str(exc))
            print(_fetch_hint(start, end, output_dir))
            return 2
        except Exception as exc:
            print("Fetch failed: " + redact(str(exc)))
            return 2
        print(f"Cached {sum(written.values())} bars for {len(written)} symbols.")
        if empty:
            print("No bars returned for: " + ", ".join(empty))

    histories, missing = load_cache(cache_dir, names)
    if not histories:
        print(f"No cached bars in {cache_dir}.")
        print("The backtest did not run. Nothing was written to the journal.")
        print(_fetch_hint(start, end, output_dir))
        return 2

    if specs is not None:
        print(
            f"RESEARCH comparison {start} to {end} "
            f"candidates={len(specs)} fractional={fractional} "
            f"dd_reset={dd_reset_flag} hash={cfg.config_hash}")
        report = run_comparison(
            histories, cfg, start=start, end=end, capitals=capitals, specs=specs,
            fractional=fractional, slippage_bps=slippage, iv_haircut=haircut,
            dd_reset_flag=dd_reset_flag,
            walk_forward=not args.skip_walk_forward)
        json_path, md_path = write_comparison(output_dir, report)
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
        print(
            f"Variants tried: {report['variants_tried']} "
            f"(pre-declared grid {report['predeclared_candidates']}). "
            "RESEARCH only. The paper journal was not written.")
        _print_research(report)
        return 0

    print(
        f"Backtest {start} to {end} books={','.join(books)} "
        f"slippage={slippage:g}bps iv_haircut={haircut:.0%} "
        f"hash={cfg.config_hash}")
    dd_reset = "monthly" if dd_reset_flag else None
    common = dict(
        histories=histories, cfg=cfg, start=start, end=end, books=books,
        slippage_bps=slippage, iv_haircut=haircut, fractional=fractional,
        dd_reset=dd_reset)
    runs = []
    for capital in capitals:
        print(f"  capital {capital:,.0f}")
        runs.append(_run(capital=capital, **common))

    anchor = float(cfg.policy["capital"]["initial"])
    if anchor not in capitals:
        anchor = capitals[-1]
    sensitivity = []
    if not args.skip_sensitivity:
        for slip in GRID_SLIPPAGE_BPS:
            for hair in GRID_IV_HAIRCUT:
                existing = _match(runs, anchor, slip, hair)
                if existing is not None:
                    sensitivity.append(_slim(existing))
                    continue
                print(f"  sensitivity slippage={slip:g}bps iv_haircut={hair:.0%}")
                path = _run(capital=anchor, **{**common, "slippage_bps": slip, "iv_haircut": hair})
                sensitivity.append(_slim(path))

    walk = None
    if not args.skip_walk_forward:
        walk = _walk_forward(
            runs, histories, cfg, anchor, books, slippage, haircut, start, end,
            fractional=fractional, dd_reset=dd_reset)

    alerts = [n["symbol"] for n in cfg.watchlist["names"] if n.get("alerts_only")]
    report = {
        "label": "ESTIMATED",
        "config_hash": cfg.config_hash,
        "strategy_version": cfg.strategy_version,
        "start": start,
        "end": end,
        "books": list(books),
        "slippage_bps": slippage,
        "iv_haircut": haircut,
        "anchor_capital": anchor,
        "option_half_spread": ENTRY_HALF_SPREAD,
        "stock_half_spread": STOCK_HALF_SPREAD,
        "assumptions": list(ASSUMPTIONS),
        "limitations": list(LIMITATIONS),
        "coverage": {
            "symbols_requested": names,
            "symbols_loaded": sorted(histories),
            "missing_symbols": missing,
            "alerts_only": alerts,
            "stock_universe": list(cfg.us_symbols),
            "wheel_universe": list(cfg.wheel_symbols),
        },
        "runs": runs,
        "sensitivity": sensitivity,
        "walk_forward": walk,
        "headline": [headline_row(run) for run in runs],
        "dividend_benchmarks": _dividend_flag(histories),
        "option_price_space": _option_space(histories),
        "fractional": fractional,
        "dd_reset": dd_reset,
    }
    json_path, md_path = write_report(output_dir, report)
    print(f"Wrote {json_path}")
    print(f"Wrote {md_path}")
    print("Wheel results are ESTIMATED. The paper journal was not written.")
    _print_headline(report)
    return 0


def _match(runs, capital, slippage, haircut):
    for run in runs:
        if (run["capital"] == capital and run["slippage_bps"] == slippage
                and run["iv_haircut"] == haircut):
            return run
    return None


def _dividend_flag(histories):
    if any(getattr(hist, "has_total", False) for hist in histories.values()):
        return "included"
    return "omitted"


def _option_space(histories):
    if any(getattr(hist, "has_raw", False) for hist in histories.values()):
        return "unadjusted"
    return "split_adjusted"


def _print_research(report):
    print(f"Headline is {report.get('walk_forward') and 'out of sample' or 'full sample'}.")
    print(f"{'Id':<24} {'Sharpe':>8} {'CAGR':>10} {'MaxDD':>10} {'Beat SPY':>10}")
    for row in report.get("ranked_oos") or []:
        beat = row.get("beat_spy")
        beat_txt = "n/a" if beat is None else ("yes" if beat else "no")
        print(
            f"{row['id']:<24} {_fmt(row.get('sharpe'), False):>8} "
            f"{_fmt(row.get('cagr')):>10} {_fmt(row.get('max_drawdown')):>10} {beat_txt:>10}")


def _walk_forward(runs, histories, cfg, anchor, books, slippage, haircut, start, end,
                  fractional=False, dd_reset=None):
    primary = _match(runs, anchor, slippage, haircut)
    sessions = [point["date"] for point in primary["curve"]] if primary else []
    parts = split_sessions(sessions, IN_SAMPLE_FRACTION)
    note = (
        f"Chronological {IN_SAMPLE_FRACTION:.0%} / {1 - IN_SAMPLE_FRACTION:.0%} split "
        f"at the anchor capital. Each slice starts flat. Bars before the slice "
        "remain available as indicator warmup. No parameter was chosen from the "
        "in-sample result. The shipped policy is unchanged.")
    if not parts:
        return {"note": note + " The sample has fewer than two sessions, so the split was not run.",
                "in_sample": None, "out_of_sample": None}
    in_sample, out_sample = parts
    print(f"  walk-forward split {out_sample[0]} (out of sample starts)")
    inn = _run(
        histories=histories, cfg=cfg, start=in_sample[0], end=in_sample[-1],
        capital=anchor, books=books, slippage_bps=slippage, iv_haircut=haircut,
        fractional=fractional, dd_reset=dd_reset)
    out = _run(
        histories=histories, cfg=cfg, start=out_sample[0], end=out_sample[-1],
        capital=anchor, books=books, slippage_bps=slippage, iv_haircut=haircut,
        fractional=fractional, dd_reset=dd_reset)
    return {
        "note": note,
        "split_date": out_sample[0],
        "in_sample_fraction": IN_SAMPLE_FRACTION,
        "in_sample": _slim(inn),
        "out_of_sample": _slim(out),
    }


def _fetch_hint(start, end, output_dir):
    return (
        "Fetch read-only split-adjusted daily bars with:\n"
        f"py desk.py backtest --start {start} --end {end} "
        f"--output-dir {output_dir} --fetch")


def _print_headline(report):
    print("Headline (combined, marked equity). Wheel prices are ESTIMATED.")
    print(f"{'Capital':>12} {'Total':>10} {'Ann.':>10} {'MaxDD':>10} {'Sharpe':>8} {'Trades':>7}")
    for row in report["headline"]:
        print(
            f"{row['capital']:12,.0f} {_fmt(row['total_return']):>10} "
            f"{_fmt(row['annualised_return']):>10} {_fmt(row['max_drawdown']):>10} "
            f"{_fmt(row['sharpe'], False):>8} {row['trades']:7d}")


def _fmt(value, percent=True):
    if value is None:
        return "n/a"
    if percent:
        return f"{value * 100:.2f}%"
    return f"{value:.2f}"
