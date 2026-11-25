"""Walk-forward comparison of the pre-declared RESEARCH grid.

Out-of-sample results are the headline. In-sample Sharpe only chooses among
the variants of a family. The paper journal is not opened and policy.json is
not written.
"""

import os
from collections import defaultdict
from dataclasses import replace

from sakibdesk.backtest.candidates import (
    DRAWDOWN_RESEARCH,
    DRAWDOWN_SHIPPED,
    GRID,
    SELECTION_RULE,
    SPLIT_ARTIFACT,
    TBILL_ANNUAL,
    grid_counts,
    run_spec,
)
from sakibdesk.backtest.engine import split_sessions
from sakibdesk.backtest.metrics import summarise
from sakibdesk.backtest.pricing import STOCK_HALF_SPREAD
from sakibdesk.backtest.report import dump_json, money, num, pct
from sakibdesk.stock import round_px

JSON_NAME = "research_comparison.json"
MD_NAME = "research_comparison.md"

# Robustness windows. They are not used to choose a variant.
SUBPERIODS = (
    ("2021", "2021-01-01", "2021-12-31"),
    ("2022", "2022-01-01", "2022-12-31"),
    ("2023-2025", "2023-01-01", "2025-12-31"),
)
_EMPTY_METRICS = {
    "total_return": None,
    "annualised_return": None,
    "max_drawdown": None,
    "sharpe": None,
    "worst_month": None,
    "worst_month_return": None,
    "trades": 0,
}


def run_comparison(histories, cfg, *, start, end, capitals, specs, fractional=False,
                   slippage_bps=10.0, iv_haircut=0.10, dd_reset_flag=False,
                   walk_forward=True):
    """Run the requested specs and return a JSON-ready report.

    Curves stay in memory only long enough to score. The returned report has
    no equity curve and no trade list.
    """
    specs = list(specs)
    ids = {spec.id for spec in specs}
    full_grid = {spec.id for spec in GRID if spec.role == "candidate"} <= ids
    apply_flag = bool(dd_reset_flag) and "shipped_dd_reset" not in ids
    effective = []
    for spec in specs:
        if apply_flag and spec.kind in ("desk", "desk_stock", "desk_wheel") and not spec.dd_reset:
            spec = replace(
                spec, dd_reset="monthly",
                title=spec.title + " [RESEARCH monthly drawdown reset]",
                caveat=DRAWDOWN_RESEARCH)
        effective.append(spec)

    anchor = _anchor(capitals, cfg)
    sessions = _sessions(histories, start, end)
    parts = split_sessions(sessions) if walk_forward and len(sessions) >= 2 else None
    memo = {}

    def execute(spec, slice_start, slice_end, capital):
        key = (spec.id, spec.dd_reset, slice_start, slice_end, float(capital), bool(fractional))
        if key not in memo:
            run = run_spec(
                spec, histories, cfg, start=slice_start, end=slice_end, capital=capital,
                fractional=fractional, slippage_bps=slippage_bps, iv_haircut=iv_haircut)
            _attach(run)
            memo[key] = run
        return memo[key]

    in_sample = {}
    if parts:
        is_start, is_end = parts[0][0], parts[0][-1]
        for spec in effective:
            in_sample[spec.id] = execute(spec, is_start, is_end, anchor)

    winners, selection = _select(effective, in_sample) if parts else ({}, [])
    seen = set()
    ordered = []
    for spec in effective:
        if spec.role != "candidate" or spec.id in seen:
            continue
        chosen = winners.get(spec.family, spec.id) if parts else spec.id
        if chosen == spec.id:
            ordered.append(spec)
            seen.add(spec.id)
    missing_families = [family for family, chosen in winners.items() if chosen is None]

    def spy_of(slice_start, slice_end, capital):
        from sakibdesk.backtest.candidates import BY_ID
        run = execute(BY_ID["bh_spy"], slice_start, slice_end, capital)
        metrics = run.get("metrics") or {}
        if run.get("status") != "ok":
            return None
        return metrics.get("total_return")

    oos_start = oos_end = None
    if parts:
        oos_start, oos_end = parts[1][0], parts[1][-1]

    def table(spec, slice_start, slice_end):
        rows = []
        for capital in capitals:
            run = execute(spec, slice_start, slice_end, capital)
            spy = spy_of(slice_start, slice_end, capital)
            rows.append(_public(spec, run, spy))
        return rows

    full_rows = {spec.id: table(spec, start, end) for spec in effective}
    oos_rows = {}
    if parts:
        oos_rows = {spec.id: table(spec, oos_start, oos_end) for spec in effective}

    headline = []
    for spec in ordered:
        block = (oos_rows if parts else full_rows).get(spec.id) or []
        anchor_row = _at(block, anchor)
        is_row = _public(spec, in_sample[spec.id], spy_of(parts[0][0], parts[0][-1], anchor)) if parts else None
        if anchor_row is None and block:
            anchor_row = block[-1]
        if anchor_row is None:
            continue
        anchor_row = dict(anchor_row)
        anchor_row["in_sample_sharpe"] = None if not is_row else is_row.get("sharpe")
        anchor_row["in_sample_cagr"] = None if not is_row else is_row.get("cagr")
        anchor_row["verdict"] = _verdict(spec, anchor_row)
        anchor_row["by_capital"] = block
        headline.append(anchor_row)
    headline.sort(key=_rank_key)

    appendix = []
    chosen_ids = {row["id"] for row in headline}
    for spec in effective:
        if spec.role != "candidate" or spec.id in chosen_ids:
            continue
        block = (oos_rows if parts else full_rows).get(spec.id) or []
        anchor_row = _at(block, anchor) or (block[-1] if block else None)
        if anchor_row is None:
            continue
        anchor_row = dict(anchor_row)
        anchor_row["by_capital"] = block
        anchor_row["verdict"] = _verdict(spec, anchor_row)
        appendix.append(anchor_row)

    robustness = []
    for spec in effective:
        if spec.role != "robustness":
            continue
        block = (oos_rows if parts else full_rows).get(spec.id) or []
        anchor_row = _at(block, anchor) or (block[-1] if block else None)
        if anchor_row is None:
            continue
        anchor_row = dict(anchor_row)
        anchor_row["by_capital"] = block
        anchor_row["verdict"] = _verdict(spec, anchor_row)
        robustness.append(anchor_row)

    subperiods = _subperiods(effective, ordered, histories, start, end, anchor, execute, spy_of)
    counts = grid_counts()
    dividend = _dividend_note(histories, sessions, anchor, fractional, cfg)
    shipped_run = None
    for spec in effective:
        if spec.id == "shipped":
            shipped_run = execute(spec, start, end, anchor)
            break
    tried = sum(1 for spec in effective if spec.role == "candidate")
    report = {
        "label": "RESEARCH",
        "config_hash": cfg.config_hash,
        "strategy_version": cfg.strategy_version,
        "start": start,
        "end": end,
        "anchor_capital": anchor,
        "fractional": bool(fractional),
        "share_rounding": "fractional_RESEARCH" if fractional else "whole_share",
        "dd_reset_flag": bool(dd_reset_flag),
        "dd_reset_note": _dd_note(dd_reset_flag, full_grid, apply_flag),
        "slippage_bps": slippage_bps,
        "iv_haircut": iv_haircut,
        "predeclared_candidates": counts[0],
        "predeclared_robustness": counts[1],
        "variants_tried": tried,
        "robustness_tried": sum(1 for spec in effective if spec.role == "robustness"),
        "selection_rule": SELECTION_RULE,
        "selection": selection,
        "walk_forward": None if not parts else {
            "in_sample_start": parts[0][0],
            "in_sample_end": parts[0][-1],
            "out_of_sample_start": parts[1][0],
            "out_of_sample_end": parts[1][-1],
            "headline_is": "out_of_sample",
        },
        "split_artifact": SPLIT_ARTIFACT,
        "drawdown_shipped": DRAWDOWN_SHIPPED,
        "drawdown_research": DRAWDOWN_RESEARCH,
        "experiment_block_date": None if not shipped_run else shipped_run.get("experiment_block_date"),
        "experiment_block_still_on": None if not shipped_run else shipped_run.get("experiment_block_still_on"),
        "option_price_space": None if not shipped_run else shipped_run.get("option_price_space"),
        "dividend_benchmarks": dividend["status"],
        "dividend_note": dividend["note"],
        "spy_total_return": dividend.get("spy_total_return"),
        "equal_weight_total_return": dividend.get("equal_weight_total_return"),
        "tbill_annual": TBILL_ANNUAL,
        "cash_yield_note": (
            "When a research rule is in cash it earns a T-bill proxy of "
            f"{TBILL_ANNUAL:.0%} compounded once per session. That is not a BIL "
            "total-return series. BIL bars are used instead when data/backtest_cache "
            "contains BIL. Buy-and-hold benchmarks do not earn this yield on leftover cash. "
            "The shipped desk still earns nothing on idle cash."
        ),
        "ranked_oos": headline,
        "appendix": appendix,
        "sector_momentum": robustness,
        "subperiods": subperiods,
        "no_selection": missing_families,
        "rules": [{"id": spec.id, "letter": spec.letter, "title": spec.title, "rules": spec.rules}
                  for spec in effective],
        "survivorship": (
            "The watchlist is the one chosen for this repository today. Names that "
            "failed or left the theme before that choice are absent. Momentum on "
            "that list is the variant most exposed to this bias. The sector-ETF "
            "panel is the check that does not use the stock watchlist."
        ),
    }
    if not parts:
        report["walk_forward_note"] = (
            "The sample has fewer than two sessions, so there is no out-of-sample "
            "slice. The table is the full sample and is not a walk-forward headline."
        )
    return report


def write_comparison(directory, report):
    """Write the comparison JSON and Markdown. Does not write the journal."""
    os.makedirs(directory, exist_ok=True)
    json_path = os.path.join(directory, JSON_NAME)
    md_path = os.path.join(directory, MD_NAME)
    with open(json_path, "w", encoding="utf-8") as handle:
        handle.write(dump_json(report))
        handle.write("\n")
    with open(md_path, "w", encoding="utf-8") as handle:
        handle.write(render_comparison(report))
    return json_path, md_path


def render_comparison(report):
    """Markdown comparison. The ranked table is out of sample when a split exists."""
    lines = ["# RESEARCH comparison", ""]
    lines.append(
        "These candidates are RESEARCH. They are not the live strategy. "
        f"Config hash `{report['config_hash']}` was not changed. policy.json and "
        "watchlist.json were not modified. This file is not the paper journal "
        "and it is not the paper performance series. Wheel prices, where a "
        "candidate sells options, are **ESTIMATED**.")
    lines.append("")
    headline_name = "out of sample" if report.get("walk_forward") else "full sample"
    lines.append(
        f"Window {report['start']} to {report['end']}. "
        f"The ranked table is {headline_name} at the anchor capital "
        f"{money(report['anchor_capital'])}. "
        f"Share rounding: {report['share_rounding']}.")
    lines.append("")
    lines.append(report["dd_reset_note"])
    lines.append("")
    lines.append("## What the $5,000 wheel result was")
    lines.append("")
    lines.append(report["split_artifact"])
    lines.append("")
    if report.get("option_price_space") == "unadjusted":
        lines.append("This run priced option collateral on unadjusted bars.")
    elif report.get("option_price_space") == "split_adjusted":
        lines.append(
            "This run did not have raw bars, so the shipped wheel still used "
            "split-adjusted strikes. Treat those wheel numbers as the artifact "
            "until `--fetch` has written `raw/`.")
    lines.append("")
    lines.append("## Drawdown block")
    lines.append("")
    lines.append(report["drawdown_shipped"])
    lines.append("")
    lines.append(report["drawdown_research"])
    lines.append("")
    if report.get("experiment_block_date"):
        still = "still on" if report.get("experiment_block_still_on") else "not still on"
        lines.append(
            f"On this cache the shipped desk's block first tripped on "
            f"{report['experiment_block_date']} and was {still} at the end of the full sample.")
        lines.append("")
    lines.append("## Selection")
    lines.append("")
    lines.append(report["selection_rule"])
    lines.append("")
    lines.append(
        f"Pre-declared candidates: {report['predeclared_candidates']}. "
        f"Pre-declared sector robustness replicas: {report['predeclared_robustness']}. "
        f"Candidates tried in this run: {report['variants_tried']}. "
        f"Robustness replicas tried in this run: {report['robustness_tried']}.")
    lines.append("")
    if report.get("walk_forward"):
        walk = report["walk_forward"]
        lines.append(
            f"In sample {walk['in_sample_start']} to {walk['in_sample_end']}. "
            f"Out of sample {walk['out_of_sample_start']} to {walk['out_of_sample_end']}.")
        lines.append("")
    elif report.get("walk_forward_note"):
        lines.append(report["walk_forward_note"])
        lines.append("")
    for block in report.get("selection") or []:
        chosen = block.get("chosen") or "none qualified"
        bits = []
        for row in block.get("candidates") or []:
            flag = "eligible" if row.get("eligible") else "ineligible"
            bits.append(f"{row['id']} Sharpe {num(row.get('sharpe'))} ({flag})")
        lines.append(f"- {block['family']}: chose {chosen}. " + "; ".join(bits))
    lines.append("")
    lines.append(f"## Ranked {headline_name} table")
    lines.append("")
    lines.append(
        "Sort is out-of-sample Sharpe at the anchor when a split exists, "
        "otherwise full-sample Sharpe. Beat SPY uses the same share rounding "
        "and the same costs. Yes means the total return is higher. The SPY "
        "benchmark row has no beat flag.")
    lines.append("")
    lines.extend(_table(report["ranked_oos"], in_sample=bool(report.get("walk_forward"))))
    lines.append("### Out-of-sample total return by starting capital")
    lines.append("")
    lines.extend(_capital_grid(report["ranked_oos"]))
    lines.append("## Verdicts")
    lines.append("")
    for row in report["ranked_oos"]:
        lines.append(row.get("verdict") or "")
        lines.append("")
    if report.get("no_selection"):
        lines.append(
            "No variant qualified in sample for: " + ", ".join(report["no_selection"]) + ". "
            "Nothing from those families was promoted on the out-of-sample result.")
        lines.append("")
    lines.append("## Sub-periods")
    lines.append("")
    lines.append(
        "The selected variant of each family, at the anchor capital, on "
        "2021, 2022, and 2023-2025. These slices are not used to choose the variant.")
    lines.append("")
    lines.extend(_sub_table(report.get("subperiods") or []))
    lines.append("## Sector-ETF momentum")
    lines.append("")
    lines.append(
        "Same 6- and 12-month rules, top 3 and top 5, on XLK, XLF, XLE, XLV, "
        "XLI, XLY, XLP, XLU, and XLB. Not used for selection.")
    lines.append("")
    if not report.get("sector_momentum"):
        lines.append("The sector panel was not in this run.")
        lines.append("")
    else:
        lines.extend(_table(report["sector_momentum"], in_sample=False))
        for row in report["sector_momentum"]:
            lines.append(row.get("verdict") or "")
            lines.append("")
    lines.append("## Appendix (not selected)")
    lines.append("")
    lines.append(
        "These variants were tried and were not the in-sample winner. "
        "They are not re-ranked into the headline.")
    lines.append("")
    if not report.get("appendix"):
        lines.append("No appendix rows. Every candidate in this run is a headline row.")
        lines.append("")
    else:
        lines.extend(_table(report["appendix"], in_sample=False))
    lines.append("## Rules")
    lines.append("")
    for rule in report.get("rules") or []:
        lines.append(f"### {rule['letter']}. {rule['title']} (`{rule['id']}`)")
        lines.append("")
        lines.append(rule["rules"])
        lines.append("")
    lines.append("## Dividends and cash yield")
    lines.append("")
    lines.append(report.get("dividend_note") or "")
    lines.append("")
    lines.append(report.get("cash_yield_note") or "")
    lines.append("")
    lines.append("## Survivorship")
    lines.append("")
    lines.append(report.get("survivorship") or "")
    lines.append("")
    lines.append(
        "This is not a forecast and it is not financial advice. "
        "A research result is not a live result.")
    lines.append("")
    return "\n".join(lines)


def _anchor(capitals, cfg):
    initial = float(cfg.policy["capital"]["initial"])
    if initial in list(capitals):
        return initial
    return max(capitals)


def _sessions(histories, start, end):
    if "SPY" in histories:
        dates = histories["SPY"].dates
    else:
        dates = sorted({day for hist in histories.values() for day in hist.dates})
    return [day for day in dates if start <= day <= end]


def _attach(run):
    if run.get("metrics"):
        return run
    if run.get("status") == "not_run" or not run.get("curve"):
        run["metrics"] = dict(_EMPTY_METRICS)
        return run
    curve = [{"date": point["date"], "equity": point["equity"]} for point in run["curve"]]
    run["metrics"] = summarise(run["capital"], run["start"], curve, run.get("trades") or [])
    return run


def _eligible(run):
    if run.get("status") != "ok":
        return False
    sharpe = (run.get("metrics") or {}).get("sharpe")
    if sharpe is None:
        return False
    fills = run.get("fills") or 0
    exposure = run.get("avg_exposure") or 0.0
    if fills <= 0 and exposure < 0.05:
        return False
    return True


def _select(specs, in_sample):
    families = defaultdict(list)
    for spec in specs:
        if spec.role != "candidate":
            continue
        families[spec.family].append(spec)
    winners = {}
    log = []
    for family, group in families.items():
        rows = []
        for spec in group:
            run = in_sample.get(spec.id)
            metrics = (run or {}).get("metrics") or {}
            rows.append({
                "id": spec.id,
                "sharpe": metrics.get("sharpe"),
                "annualised_return": metrics.get("annualised_return"),
                "max_drawdown": metrics.get("max_drawdown"),
                "eligible": bool(run) and _eligible(run) and len(group) >= 1,
            })
        if len(group) == 1:
            winners[family] = group[0].id
            chosen = group[0].id
        else:
            from sakibdesk.backtest.candidates import select_winner
            # A single-variant family is reported even when it never traded.
            # A multi-variant family must earn eligibility in sample.
            chosen = select_winner(rows)
            winners[family] = chosen
        log.append({"family": family, "chosen": chosen, "candidates": rows})
    return winners, log


def _public(spec, run, spy_return):
    metrics = run.get("metrics") or {}
    total = metrics.get("total_return")
    if spec.id == "bh_spy" or run.get("status") != "ok" or total is None or spy_return is None:
        beat = None
    else:
        beat = bool(total > spy_return + 1e-12)
    trades = run.get("fills")
    if trades is None:
        trades = metrics.get("trades")
    return {
        "id": spec.id,
        "letter": spec.letter,
        "family": spec.family,
        "title": spec.title,
        "role": spec.role,
        "status": run.get("status"),
        "reason": run.get("reason") or "",
        "capital": run.get("capital"),
        "sharpe": metrics.get("sharpe"),
        "cagr": metrics.get("annualised_return"),
        "total_return": total,
        "max_drawdown": metrics.get("max_drawdown"),
        "worst_month": metrics.get("worst_month"),
        "worst_month_return": metrics.get("worst_month_return"),
        "trades": trades,
        "closed_trades": metrics.get("trades"),
        "turnover": run.get("turnover"),
        "costs": (run.get("costs") or {}).get("total"),
        "avg_exposure": run.get("avg_exposure"),
        "beat_spy": beat,
        "spy_return": spy_return,
        "start": run.get("start"),
        "end": run.get("end"),
        "sessions": run.get("sessions"),
        "share_rounding": run.get("share_rounding"),
        "cash_yield": run.get("cash_yield"),
        "option_price_space": run.get("option_price_space"),
        "experiment_block_date": run.get("experiment_block_date"),
        "dd_reset_count": run.get("dd_reset_count") or 0,
        "estimated": bool(run.get("estimated")),
        "label": run.get("label") or "RESEARCH",
        "eligible": _eligible(run),
    }


def _at(rows, capital):
    for row in rows:
        if row.get("capital") == capital:
            return row
    return None


def _rank_key(row):
    sharpe = row.get("sharpe")
    cagr = row.get("cagr")
    return (sharpe is None, -(sharpe or 0.0), -(cagr or -1e9), row.get("id") or "")


def _verdict(spec, row):
    if row.get("status") == "not_run":
        return f"{spec.title}: not run ({row.get('reason') or 'no result'})."
    if row.get("status") == "no_signal":
        return (
            f"{spec.title}: the lookback never became available in this window, "
            "so there is no performance claim."
        )
    beat = row.get("beat_spy")
    if beat is None and spec.id == "bh_spy":
        beat_txt = "This row is the SPY benchmark."
    elif beat is None:
        beat_txt = "Beat SPY out of sample: n/a."
    elif beat:
        beat_txt = "Beat SPY out of sample: yes."
    else:
        beat_txt = "Beat SPY out of sample: no."
    worst = row.get("worst_month") or "n/a"
    if row.get("worst_month_return") is not None:
        worst = f"{worst} {pct(row['worst_month_return'])}"
    return (
        f"{spec.title} (`{spec.id}`), {row.get('label') or 'RESEARCH'}. {beat_txt} "
        f"CAGR {pct(row.get('cagr'))}, total return {pct(row.get('total_return'))}, "
        f"max drawdown {pct(row.get('max_drawdown'))}, Sharpe {num(row.get('sharpe'))}, "
        f"worst month {worst}. Trades {row.get('trades')}, annualised one-way turnover "
        f"{pct(row.get('turnover'))}, costs {money(row.get('costs'))}, average exposure "
        f"{pct(row.get('avg_exposure'))}. {spec.caveat}"
    )


def _subperiods(specs, ordered, histories, start, end, anchor, execute, spy_of):
    dates = histories["SPY"].dates if "SPY" in histories else []
    rows = []
    by_id = {spec.id: spec for spec in specs}
    for spec in ordered:
        for name, lo, hi in SUBPERIODS:
            window = [day for day in dates if lo <= day <= hi and start <= day <= end]
            if len(window) < 20:
                rows.append({
                    "id": spec.id, "period": name, "status": "not_run",
                    "reason": "fewer than 20 sessions in the cache for this sub-period",
                    "total_return": None, "sharpe": None, "max_drawdown": None, "beat_spy": None,
                })
                continue
            run = execute(by_id[spec.id], window[0], window[-1], anchor)
            spy = spy_of(window[0], window[-1], anchor)
            public = _public(spec, run, spy)
            rows.append({
                "id": spec.id, "period": name, "status": public["status"],
                "reason": public["reason"], "total_return": public["total_return"],
                "sharpe": public["sharpe"], "max_drawdown": public["max_drawdown"],
                "beat_spy": public["beat_spy"], "cagr": public["cagr"],
            })
    return rows


def _dividend_note(histories, sessions, capital, fractional, cfg):
    if not sessions:
        return {"status": "omitted", "note": "No sessions, so no dividend benchmark was computed."}
    spy = histories.get("SPY")
    if spy is None or not getattr(spy, "has_total", False):
        return {
            "status": "omitted",
            "note": (
                "Dividend-adjusted total-return benchmarks were omitted. The cache "
                "has no `total/` bars (Alpaca adjustment=all). SPY and equal-weight "
                "figures in the table are price returns, so dividends are missing. "
                "Re-run with `--fetch` to store that series. A price-return SPY "
                "result is lower than a total-return result by roughly the dividends "
                "SPY paid, which the cache does not contain."
            ),
        }
    start, end = sessions[0], sessions[-1]
    cost = float(cfg.policy["costs"]["overlay_per_stock_transaction"])
    spy_total = _priced_hold(spy, start, end, capital, fractional, cost, total=True)
    equal = _equal_total(histories, cfg, start, end, capital, fractional, cost)
    note = (
        "Dividend-adjusted benchmarks use `total/` bars (split and dividend). "
        f"SPY total return at the anchor over the full sample is {pct(spy_total)}. "
    )
    if equal is None:
        note += "Equal-weight total return was omitted because not every held name has a total-return bar."
    else:
        note += f"Equal-weight total return at the anchor is {pct(equal)}."
    return {
        "status": "included",
        "note": note,
        "spy_total_return": spy_total,
        "equal_weight_total_return": equal,
    }


def _priced_hold(hist, start, end, capital, fractional, cost, total=False):
    """One-name buy and hold. `total=True` reads dividend-adjusted bars."""
    if total:
        if not hist.has_total:
            return None
        entry_bar = hist.total_on(start)
        exit_bar = None
        for bar_date, bar in hist.total_by_date.items():
            if start <= bar_date <= end:
                exit_bar = bar
        entry = None if entry_bar is None else entry_bar.o
        exit_px = None if exit_bar is None else exit_bar.c
    else:
        entry_bar = hist.on(start)
        exit_px = None
        entry = None if entry_bar is None else entry_bar.o
        for bar in hist.bars:
            if start <= bar.date <= end:
                exit_px = bar.c
    if not entry or not exit_px or entry <= 0:
        return None
    fill = round_px(entry * (1.0 + STOCK_HALF_SPREAD))
    budget = capital - cost
    if fractional:
        qty = 0.0 if fill <= 0 else math_floor_qty(budget, fill)
    else:
        qty = 0 if fill <= 0 else int(budget // fill)
    if qty <= 0:
        return 0.0
    end_value = (capital - qty * fill - cost) + qty * exit_px - cost
    return end_value / capital - 1.0


def math_floor_qty(budget, fill):
    if budget <= 0 or fill <= 0:
        return 0.0
    qty = int(budget / fill * 1_000_000) / 1_000_000
    if qty * fill < 1.0:
        return 0.0
    return qty


def _equal_total(histories, cfg, start, end, capital, fractional, cost):
    names = []
    for symbol in cfg.us_symbols:
        hist = histories.get(symbol)
        if hist is None or not hist.has_total or hist.total_on(start) is None:
            return None
        names.append(hist)
    if not names:
        return None
    each = capital / float(len(names))
    end_value = 0.0
    for hist in names:
        # Recompute in dollars, then sum. A name that cannot buy contributes its cash.
        held = _priced_hold(hist, start, end, each, fractional, cost, total=True)
        if held is None:
            return None
        end_value += each * (1.0 + held)
    return end_value / capital - 1.0


def _dd_note(flag, full_grid, applied):
    if flag and full_grid:
        return (
            "`--dd-reset` was set. The pre-declared grid already contains "
            "shipped_dd_reset, so the other candidates keep the shipped block "
            "that never clears."
        )
    if applied:
        return (
            "`--dd-reset` was set. Engine candidates in this run clear the 10% "
            "block on the first session of the next month. That is RESEARCH. "
            "Equity rules (momentum, trend, dual momentum, buy and hold) do not "
            "use the experiment block."
        )
    if flag:
        return (
            "`--dd-reset` was set. This selection already includes the monthly "
            "reset variant, or it has no engine candidate to apply it to."
        )
    return (
        "`--dd-reset` was not set. The shipped desk keeps the block that never "
        "clears. shipped_dd_reset is included only when it is in the requested set."
    )


def _yn(value):
    if value is None:
        return "n/a"
    return "yes" if value else "no"


def _table(rows, in_sample):
    head = (
        "| Id | Sharpe | CAGR | Max DD | Worst month | Trades | Turnover | "
        "Costs | Exposure | Beat SPY |"
    )
    if in_sample:
        head = (
            "| Id | OOS Sharpe | IS Sharpe | CAGR | Max DD | Worst month | Trades | "
            "Turnover | Costs | Exposure | Beat SPY |"
        )
    lines = [head, "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |" + (" --- |" if in_sample else "")]
    for row in rows:
        worst = row.get("worst_month") or "n/a"
        if row.get("worst_month_return") is not None:
            worst = f"{worst} {pct(row['worst_month_return'])}"
        cells = [
            row.get("id") or "",
        ]
        if in_sample:
            cells.append(num(row.get("sharpe")))
            cells.append(num(row.get("in_sample_sharpe")))
        else:
            cells.append(num(row.get("sharpe")))
        cells.extend([
            pct(row.get("cagr")),
            pct(row.get("max_drawdown")),
            worst,
            str(row.get("trades") if row.get("trades") is not None else "n/a"),
            pct(row.get("turnover")),
            money(row.get("costs")),
            pct(row.get("avg_exposure")),
            _yn(row.get("beat_spy")),
        ])
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _capital_grid(rows):
    capitals = []
    for row in rows:
        for item in row.get("by_capital") or []:
            if item.get("capital") not in capitals:
                capitals.append(item.get("capital"))
    if not capitals:
        return ["No capital grid.", ""]
    header = "| Id | " + " | ".join(money(c) for c in capitals) + " |"
    sep = "| --- | " + " | ".join("---" for _ in capitals) + " |"
    lines = [header, sep]
    for row in rows:
        by = {item.get("capital"): item for item in row.get("by_capital") or []}
        cells = [row.get("id") or ""]
        for capital in capitals:
            item = by.get(capital)
            cells.append(pct(None if item is None else item.get("total_return")))
        lines.append("| " + " | ".join(cells) + " |")
    lines.append("")
    return lines


def _sub_table(rows):
    if not rows:
        lines = ["No sub-period rows.", ""]
        return lines
    lines = [
        "| Id | Period | Total | CAGR | Max DD | Sharpe | Beat SPY |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row.get('id')} | {row.get('period')} | {pct(row.get('total_return'))} | "
            f"{pct(row.get('cagr'))} | {pct(row.get('max_drawdown'))} | "
            f"{num(row.get('sharpe'))} | {_yn(row.get('beat_spy'))} |"
        )
    lines.append("")
    return lines
