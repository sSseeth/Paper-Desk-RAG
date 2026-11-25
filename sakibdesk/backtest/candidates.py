"""RESEARCH candidates. They do not change the live runner.

The grid is pre-declared. A variant is not added because a sample looked
good. When a family has more than one variant, walk-forward selection uses
in-sample Sharpe only. Out-of-sample numbers are the headline.

Wheel premiums in this module are Black-Scholes estimates. They stay labelled
ESTIMATED. Cash that a rule parks out of the market earns a T-bill proxy.
"""

import copy
import math
from dataclasses import dataclass

from sakibdesk.backtest.engine import run_backtest
from sakibdesk.backtest.pricing import RISK_FREE, STOCK_HALF_SPREAD
from sakibdesk.indicators import sma
from sakibdesk.stock import round_px

# Formation window. 21 sessions is the skipped recent month. 126 and 252 are
# the six- and twelve-month lookbacks. The return is close[-21] / close[-126]
# (or close[-252]) minus one, on completed bars only.
SKIP_SESSIONS = 21
LOOKBACK_6M = 126
LOOKBACK_12M = 252
TREND_DAYS = 200
TREND_MONTHS = 10
# Same rate the option pricer uses. It is not a fitted T-bill curve and it is
# not a BIL total-return series. BIL is used only when its bars are loaded.
TBILL_ANNUAL = RISK_FREE
TBILL_DAILY = (1.0 + TBILL_ANNUAL) ** (1.0 / 252.0) - 1.0

SECTOR_ETFS = ("XLK", "XLF", "XLE", "XLV", "XLI", "XLY", "XLP", "XLU", "XLB")
INDEX_ETFS = ("SPY", "QQQ", "IWM")
CHEAP_ETFS = ("SPLG", "QQQM", "XLF")
FETCH_EXTRAS = INDEX_ETFS + CHEAP_ETFS + SECTOR_ETFS + ("BIL",)

SPLIT_ARTIFACT = (
    "A listed equity option is 100 shares at the strike quoted that day, in "
    "that day's dollars. Split-adjusted bars rewrite earlier prices as if "
    "later splits had already happened, so a pre-split name such as NVDA "
    "(4-for-1 in 2021 and 10-for-1 in 2024) or AVGO (10-for-1 in 2024) can "
    "print at a few dollars. Collateral of 100 times that adjusted strike "
    "fits a small account, and the assigned 100 'shares' are then marked on "
    "the adjusted series. The real contract needed 100 times the unadjusted "
    "strike, which that account could not fund, so the entry would have been "
    "skipped. The owner's $5,000 run (about +339% from 6 wheel trades) matches "
    "this artifact. With raw bars loaded, strikes, premiums, collateral, and "
    "the deliverable use the unadjusted price, and a later split rescales the "
    "open contract so its dollar value stays continuous. This process does "
    "not invent a replacement 2021-2026 percentage."
)

DRAWDOWN_SHIPPED = (
    "The shipped block is risk.RiskState: once equity is 10% below the "
    "high-water mark, experiment_block is set and never cleared. New entries "
    "stay off for the rest of the sample. Exits still run. The owner reported "
    "that this flag tripped on 2022-04-27 and then left most of the window "
    "with no entries. A backtest on a fresh cache reports the date it "
    "actually trips; that date is not hardcoded."
)

DRAWDOWN_RESEARCH = (
    "RESEARCH variant shipped_dd_reset clears the flag on the first session "
    "of a later calendar month and restarts the high-water mark at that "
    "session's equity. One rule, not a search over cooling periods. The live "
    "runner does not do this. --dd-reset on a single-strategy run uses the "
    "same rule. On --strategies all the grid already contains the variant, "
    "and the other candidates keep the shipped block."
)

SELECTION_RULE = (
    "Walk-forward is a chronological 60/40 split. Each slice starts flat. "
    "Bars before the slice remain available as warmup. A family with more "
    "than one variant is reduced to one headline row by the highest "
    "in-sample Sharpe at the anchor capital. Ties break to the higher "
    "in-sample CAGR, then the lower max drawdown, then the id. A variant "
    "with no Sharpe, no fill, and average exposure under 5% is not eligible, "
    "so a flat T-bill path cannot win. The headline numbers are that "
    "winner's out-of-sample path. Sibling variants are in the appendix and "
    "are not re-ranked on the out-of-sample result."
)


@dataclass(frozen=True)
class Spec:
    """One pre-declared candidate. `role` is candidate or robustness."""

    id: str
    letter: str
    family: str
    role: str
    title: str
    kind: str
    rules: str
    caveat: str
    estimated: bool = False
    dd_reset: str = ""
    entry_frequency: str = "daily"
    put_trend_sma: int = 0
    hold: int = 0
    lookback: int = 0
    universe: str = ""
    window: str = ""
    mode: str = ""
    wheel_overrides: tuple = ()
    stock_overrides: tuple = ()
    symbols: tuple = ()
    require: str = "spy"

    def wheel_params(self, cfg):
        if not self.wheel_overrides:
            return None
        params = copy.deepcopy(cfg.policy["wheel"])
        params.update(dict(self.wheel_overrides))
        return params

    def stock_params(self, cfg):
        if not self.stock_overrides:
            return None
        params = copy.deepcopy(cfg.policy["stock"])
        params.update(dict(self.stock_overrides))
        return params

    def needed(self, cfg):
        """Symbols that must be present. A partial watchlist is allowed."""
        if self.require == "listed":
            return list(self.symbols)
        if self.require == "sector":
            return ["SPY", *SECTOR_ETFS]
        if self.require == "spy_qqq":
            return ["SPY", "QQQ"]
        if self.require == "spy":
            return ["SPY"]
        if self.require == "one_stock":
            return []
        return ["SPY"]

    def extra_symbols(self):
        names = list(self.symbols)
        if self.kind in ("trend", "dual"):
            names.extend(["SPY", "QQQ"])
        if self.kind == "momentum" and self.universe == "sector":
            names.extend(["SPY", *SECTOR_ETFS])
        if self.kind == "buy_hold" and self.mode == "spy":
            names.append("SPY")
        if self.require == "listed":
            names.extend(self.symbols)
        return names


def _momentum_rules(hold, lookback, universe):
    months = "12" if lookback == LOOKBACK_12M else "6"
    where = "sector ETFs" if universe == "sector" else "the stock-eligible watchlist"
    return (
        f"RESEARCH momentum. Each month hold the top {hold} names in {where} "
        f"by the {months}-month return, skipping the most recent {SKIP_SESSIONS} "
        "sessions (close at a lag of 21 over close at a lag of "
        f"{lookback}). Equal weight. If the completed SPY close is not above "
        "its 200-day average, hold cash. Cash earns the T-bill proxy. "
        "Negative formation returns are still eligible when the regime is on. "
        "This is not the live stock book."
    )


def _build():
    """The whole grid. Tests lock the ids so a variant cannot appear quietly."""
    specs = [
        Spec(
            id="shipped", letter="A", family="shipped", role="candidate",
            title="Shipped desk", kind="desk", require="spy",
            rules=(
                "The live stock book and the live wheel, including the policy "
                "20/80 split, whole shares, and the 10% drawdown block that "
                "never clears. No parameter is changed. Wheel prices are ESTIMATED."
            ),
            caveat=(
                "This is the live desk, including the 20/80 split and the "
                "drawdown block that never clears. Wheel prices are ESTIMATED."
            ),
        ),
        Spec(
            id="shipped_dd_reset", letter="A", family="shipped_dd", role="candidate",
            title="Shipped desk, monthly drawdown reset", kind="desk", require="spy",
            dd_reset="monthly",
            rules=(
                "Same rules as the shipped desk. RESEARCH difference: the 10% "
                "experiment block clears on the first session of a later "
                "calendar month and the high-water mark restarts at that "
                "equity. The live runner never clears the flag."
            ),
            caveat=(
                "Same desk, but the 10% block clears on the first session of "
                "the next month. The live runner does not do this."
            ),
        ),
    ]
    for hold in (3, 5):
        for lookback, label in ((LOOKBACK_6M, "6m"), (LOOKBACK_12M, "12m")):
            specs.append(Spec(
                id=f"momentum_{hold}_{label}", letter="B", family="momentum",
                role="candidate", kind="momentum", universe="watchlist",
                hold=hold, lookback=lookback, require="spy",
                title=f"Momentum top {hold}, {label}, watchlist",
                rules=_momentum_rules(hold, lookback, "watchlist"),
                caveat=(
                    "Today's watchlist is a survivor set. Momentum on that "
                    "list is flattered. The sector-ETF panel is the less "
                    "hindsight-biased check."
                ),
            ))
    specs.append(Spec(
        id="trend_200", letter="C", family="trend", role="candidate",
        kind="trend", window="200", require="spy_qqq",
        title="SPY and QQQ above the 200-day average",
        rules=(
            "RESEARCH. Once a month, hold SPY when its completed close is "
            "above the 200-day average and QQQ on the same test. Equal weight "
            "among the names that pass. A name that fails is cash. Cash earns "
            "the T-bill proxy. This is not the live SPY filter, which only "
            "gates the stock book."
        ),
        caveat="Two index ETFs, checked monthly. Cash earns the T-bill proxy when BIL bars are absent.",
    ))
    specs.append(Spec(
        id="trend_10m", letter="C", family="trend", role="candidate",
        kind="trend", window="10m", require="spy_qqq",
        title="SPY and QQQ above the 10-month average",
        rules=(
            "RESEARCH. Once a month, hold SPY when last month's close is above "
            f"the average of the last {TREND_MONTHS} month-end closes, and QQQ "
            "on the same test. The current month-end is included in the "
            "average. Equal weight among the names that pass, otherwise cash "
            "earning the T-bill proxy."
        ),
        caveat="Ten-month version of the index filter. One pre-declared window, not a search.",
    ))
    specs.append(Spec(
        id="dual_momentum", letter="D", family="dual", role="candidate",
        kind="dual", lookback=LOOKBACK_12M, require="spy_qqq",
        title="Dual momentum, SPY vs QQQ vs cash",
        rules=(
            "RESEARCH dual momentum, monthly. Compare the 12-month return of "
            f"SPY and of QQQ, skipping the most recent {SKIP_SESSIONS} sessions. "
            "Hold the higher one when that return beats the T-bill proxy over "
            "the same span. Otherwise hold cash earning the T-bill proxy. "
            "One position. BIL is used for the hurdle only when its bars are loaded."
        ),
        caveat=(
            "One pre-declared rule: 12-month return, skip one month, SPY "
            "against QQQ against the T-bill proxy."
        ),
    ))
    specs.append(Spec(
        id="wheel_index", letter="E", family="wheel", role="candidate",
        kind="desk_wheel", require="listed", symbols=INDEX_ETFS, estimated=True,
        title="Wheel on SPY, QQQ, and IWM",
        rules=(
            "RESEARCH wheel. Same screen, delta band, 30-45 DTE, 50% take "
            "profit, and state machine as the live wheel. The universe is "
            "SPY, QQQ, and IWM instead of the policy names. The whole account "
            "is the wheel (no idle stock sleeve). Prices are ESTIMATED. One "
            "contract needs 100 times the unadjusted strike."
        ),
        caveat=(
            "Premiums are ESTIMATED. One contract needs 100 times the "
            "unadjusted strike. Skips are counted. The account is deployed in the wheel."
        ),
    ))
    specs.append(Spec(
        id="wheel_cheap", letter="E", family="wheel", role="candidate",
        kind="desk_wheel", require="listed", symbols=CHEAP_ETFS, estimated=True,
        title="Wheel on SPLG, QQQM, and XLF",
        rules=(
            "RESEARCH wheel on cheaper liquid ETFs (SPLG, QQQM, XLF) so a "
            "small account has a chance to fund 100 times the strike. Same "
            "live screen and management otherwise. Prices are ESTIMATED."
        ),
        caveat=(
            "Cheaper underlyings, same contract size of 100 shares. A strike "
            "the account cannot fund is a skip, not a partial contract. ESTIMATED."
        ),
    ))
    specs.append(Spec(
        id="wheel_trend", letter="E", family="wheel", role="candidate",
        kind="desk_wheel", require="spy", put_trend_sma=TREND_DAYS, estimated=True,
        title="Wheel, puts only above the 200-day average",
        rules=(
            "RESEARCH. The live wheel universe and the live screen. A new put "
            "is skipped unless the completed adjusted close is above the "
            "200-day average. Covered calls on shares already held are "
            "unchanged. Prices are ESTIMATED."
        ),
        caveat="The trend filter is research-only. The live wheel does not have it. ESTIMATED.",
    ))
    specs.append(Spec(
        id="wheel_delta15", letter="E", family="wheel", role="candidate",
        kind="desk_wheel", require="spy", estimated=True,
        title="Wheel, delta near 0.15",
        rules=(
            "RESEARCH. Live universe and 30-45 DTE. Put delta band is -0.20 "
            "to -0.10 with a 0.15 target (the live band is -0.30 to -0.15 "
            "with a 0.20 target). Call band is 0.10 to 0.20. The 50% take "
            "profit is unchanged. Prices are ESTIMATED."
        ),
        wheel_overrides=(
            ("put_delta_min", -0.20), ("put_delta_max", -0.10),
            ("call_delta_min", 0.10), ("call_delta_max", 0.20),
            ("delta_target_abs", 0.15),
        ),
        caveat="One pre-declared delta band, not a sweep. ESTIMATED.",
    ))
    specs.append(Spec(
        id="wheel_dte45_tp25", letter="E", family="wheel", role="candidate",
        kind="desk_wheel", require="spy", estimated=True,
        title="Wheel, 45 DTE target and 25% take profit",
        rules=(
            "RESEARCH. Live universe and live delta band. DTE window is 30 to "
            "60 with a target of 45 (live target is 35 inside 30-45). Take "
            "profit buys the option back when the ask is 25% of the open "
            "premium (live is 50%). Prices are ESTIMATED."
        ),
        wheel_overrides=(
            ("dte_min", 30), ("dte_max", 60), ("dte_target", 45),
            ("take_profit_ask_pct_of_open", 0.25),
        ),
        caveat="One pre-declared DTE and take-profit pair. The live book takes profit at 50%. ESTIMATED.",
    ))
    specs.append(Spec(
        id="stock_weekly", letter="F", family="stock_time", role="candidate",
        kind="desk_stock", require="spy", entry_frequency="weekly",
        title="Stock signal, weekly entries",
        rules=(
            "RESEARCH. The live stock signal, size, and exits. New entries "
            "are allowed only on the first session of an ISO week. Stops and "
            "the live exit checks still run every session. The whole account "
            "is the stock book. The shipped drawdown block does not reset."
        ),
        caveat=(
            "Same breakout rule as the live stock book, with weekly entries. "
            "The whole account is in the stock book. The shipped drawdown block still never clears."
        ),
    ))
    specs.append(Spec(
        id="stock_hold60", letter="F", family="stock_time", role="candidate",
        kind="desk_stock", require="spy",
        title="Stock signal, 60-session hold",
        stock_overrides=(("max_holding_sessions", 60),),
        rules=(
            "RESEARCH. The live signal with max_holding_sessions set to 60 "
            "(the live value is 20). The exit reason string in the live "
            "helper still says 20; the threshold used is 60. Everything else "
            "matches the live stock book, deployed on the whole account."
        ),
        caveat="One pre-declared holding length. The live book exits at 20 sessions.",
    ))
    specs.append(Spec(
        id="stock_lookback63", letter="F", family="stock_time", role="candidate",
        kind="desk_stock", require="spy",
        title="Stock signal, 63-session breakout",
        stock_overrides=(("breakout_lookback", 63), ("rank_lookback", 126)),
        rules=(
            "RESEARCH. The live signal with breakout_lookback 63 and "
            "rank_lookback 126 (live values are 20 and 63). Daily decisions. "
            "The whole account is the stock book."
        ),
        caveat="One pre-declared longer lookback. Not a search over windows.",
    ))
    specs.append(Spec(
        id="bh_spy", letter="G", family="bh_spy", role="candidate",
        kind="buy_hold", mode="spy", require="spy",
        title="Buy and hold SPY",
        rules=(
            "Buy SPY on the first session and hold it. Whole shares unless "
            "--fractional is set. The policy overlay is charged on the buy. "
            "Leftover cash does not earn the T-bill proxy. This is a benchmark."
        ),
        caveat="Benchmark. Not a timing rule. Whole shares unless --fractional is set.",
    ))
    specs.append(Spec(
        id="bh_equal_weight", letter="G", family="bh_equal", role="candidate",
        kind="buy_hold", mode="equal", require="one_stock",
        title="Equal-weight buy and hold of the watchlist",
        rules=(
            "Buy the stock-eligible names that have a bar on the first "
            "session, equal weight, and hold them. Names without that bar "
            "are excluded and listed. Whole shares unless --fractional is "
            "set. This is a benchmark, and it inherits the watchlist's "
            "survivorship bias."
        ),
        caveat=(
            "Benchmark on today's watchlist. Names that failed before the "
            "list was chosen are absent, which flatters the result."
        ),
    ))
    for hold in (3, 5):
        for lookback, label in ((LOOKBACK_6M, "6m"), (LOOKBACK_12M, "12m")):
            specs.append(Spec(
                id=f"sector_momentum_{hold}_{label}", letter="B",
                family="momentum_sector", role="robustness", kind="momentum",
                universe="sector", hold=hold, lookback=lookback, require="sector",
                title=f"Sector-ETF momentum top {hold}, {label}",
                rules=_momentum_rules(hold, lookback, "sector"),
                caveat=(
                    "Same momentum rule on XLK, XLF, XLE, XLV, XLI, XLY, XLP, "
                    "XLU, and XLB. A robustness check. It is not allowed to "
                    "win the watchlist selection. Sector ETFs can still be "
                    "revised, but they are not this repository's survivor list."
                ),
            ))
    return tuple(specs)


GRID = _build()
BY_ID = {spec.id: spec for spec in GRID}

_GROUPS = {
    "momentum": [s.id for s in GRID if s.family == "momentum"],
    "trend": [s.id for s in GRID if s.family == "trend"],
    "dual": ["dual_momentum"],
    "wheel": [s.id for s in GRID if s.family == "wheel"],
    "stock_timeframe": [s.id for s in GRID if s.family == "stock_time"],
    "benchmark": ["bh_spy", "bh_equal_weight"],
    "benchmarks": ["bh_spy", "bh_equal_weight"],
    "shipped": ["shipped"],
}


def grid_counts():
    """(selection candidates, sector robustness replicas)."""
    candidates = sum(1 for spec in GRID if spec.role == "candidate")
    robust = sum(1 for spec in GRID if spec.role == "robustness")
    return candidates, robust


def resolve_strategies(text):
    """Turn a CLI token list into specs.

    `all` is every candidate plus the sector robustness panel. A family name
    expands to that family's pre-declared variants. An unknown token raises
    ValueError. None means the caller wants the original shipped report.
    """
    if text is None:
        return None
    parts = [p.strip().lower() for p in str(text).split(",") if p.strip()]
    if not parts:
        raise ValueError("strategies list is empty")
    if "all" in parts and len(parts) > 1:
        raise ValueError("strategies all cannot be combined with other names")
    if parts == ["all"]:
        return list(GRID)
    ids = []
    for part in parts:
        if part in _GROUPS:
            ids.extend(_GROUPS[part])
            if part == "momentum":
                ids.extend(spec.id for spec in GRID if spec.role == "robustness")
        elif part in BY_ID:
            ids.append(part)
        else:
            known = ", ".join(sorted(set(_GROUPS) | set(BY_ID)))
            raise ValueError(f"unknown strategy {part!r}. Known names: {known}")
    chosen = []
    seen = set()
    for spec_id in ids:
        if spec_id in seen:
            continue
        seen.add(spec_id)
        chosen.append(BY_ID[spec_id])
    return chosen


def fetch_symbols(cfg, specs):
    """Symbols to cache. The watchlist file is not modified."""
    names = list(cfg.us_symbols)
    if "SPY" not in names:
        names.append("SPY")
    if not specs:
        return names
    for spec in specs:
        for symbol in spec.extra_symbols():
            if symbol not in names:
                names.append(symbol)
    for symbol in FETCH_EXTRAS:
        if symbol not in names:
            names.append(symbol)
    return names


def formation_return(closes, lookback, skip=SKIP_SESSIONS):
    """Return from `lookback` sessions ago to `skip` sessions ago.

    `closes` are completed sessions, oldest first. The last element is the
    most recent completed close. None when the history is shorter than
    `lookback` or a price is not positive.
    """
    if lookback <= skip or closes is None or len(closes) < lookback:
        return None
    end = closes[-skip]
    start = closes[-lookback]
    if start <= 0 or end <= 0:
        return None
    return end / start - 1.0


def above_sma(closes, window):
    """True when the last close is strictly above its simple average."""
    if not closes or len(closes) < window:
        return None
    avg = sma(closes, window)
    if avg is None:
        return None
    return closes[-1] > avg


def month_end_closes(dated, today):
    """Completed month-end closes strictly before `today`.

    `dated` is (session date, close), oldest first. The month that contains
    `today` is not included, so a decision on the first session of a month
    sees the previous month as the last point.
    """
    last = {}
    month = today[:7]
    for day, close in dated:
        if day >= today or day[:7] >= month:
            break
        last[day[:7]] = close
    return [last[key] for key in sorted(last)]


def select_winner(rows):
    """Id with the best in-sample Sharpe, or None when nobody is eligible.

    `rows` are dicts with id, sharpe, annualised_return, max_drawdown, eligible.
    This function does not look at out-of-sample fields.
    """
    pool = [row for row in rows if row.get("eligible") and row.get("sharpe") is not None]
    if not pool:
        return None

    def sort_key(row):
        cagr = row.get("annualised_return")
        drawdown = row.get("max_drawdown")
        return (
            -row["sharpe"],
            -(cagr if cagr is not None else -1e9),
            drawdown if drawdown is not None else 1e9,
            row["id"],
        )

    pool.sort(key=sort_key)
    return pool[0]["id"]


def _completed(hist, today):
    closes = []
    dated = []
    if hist is None:
        return closes, dated
    for bar in hist.bars:
        if bar.date >= today:
            break
        closes.append(bar.c)
        dated.append((bar.date, bar.c))
    return closes, dated


def _open_px(hist, day):
    """Fill price: unadjusted open when raw bars exist, else the split open."""
    if hist is None:
        return None
    if hist.has_raw:
        raw = hist.raw_on(day)
        if raw is None:
            return None
        return raw.o
    bar = hist.on(day)
    return None if bar is None else bar.o


def _close_px(hist, day):
    if hist is None:
        return None
    if hist.has_raw:
        raw = hist.raw_on(day)
        if raw is None:
            return None
        return raw.c
    bar = hist.on(day)
    return None if bar is None else bar.c


def _qty_for(dollars, fill, fractional):
    if fill <= 0 or dollars <= 0:
        return 0
    if not fractional:
        return max(0, math.floor(dollars / fill))
    qty = math.floor((dollars / fill) * 1_000_000) / 1_000_000
    if qty * fill < 1.0:
        return 0
    return qty


def _targets(spec, histories, today, symbols):
    """Return (weights, ready).

    `ready` is False when the lookback does not exist yet. An empty weight
    dict with ready True means the rule chose cash.
    """
    if spec.kind == "buy_hold":
        return None, True
    if spec.kind == "trend":
        weights = {}
        ready = False
        for symbol in symbols:
            hist = histories.get(symbol)
            closes, dated = _completed(hist, today)
            if spec.window == "200":
                flag = above_sma(closes, TREND_DAYS)
            else:
                ends = month_end_closes(dated, today)
                if len(ends) < TREND_MONTHS:
                    flag = None
                else:
                    window = ends[-TREND_MONTHS:]
                    flag = window[-1] > (sum(window) / float(TREND_MONTHS))
            if flag is None:
                continue
            ready = True
            if flag:
                weights[symbol] = 1.0
        if not ready:
            return {}, False
        count = len(weights)
        if count:
            for symbol in weights:
                weights[symbol] = 1.0 / count
        return weights, True
    if spec.kind == "dual":
        returns = {}
        for symbol in ("SPY", "QQQ"):
            closes, _dated = _completed(histories.get(symbol), today)
            value = formation_return(closes, spec.lookback, SKIP_SESSIONS)
            if value is not None:
                returns[symbol] = value
        if len(returns) < 2:
            return {}, False
        span = (spec.lookback - SKIP_SESSIONS) / 252.0
        hurdle = (1.0 + TBILL_ANNUAL) ** span - 1.0
        best = max(returns, key=lambda name: (returns[name], name))
        if returns[best] > hurdle:
            return {best: 1.0}, True
        return {}, True
    if spec.kind == "momentum":
        spy, _dated = _completed(histories.get("SPY"), today)
        regime = above_sma(spy, TREND_DAYS)
        if regime is None:
            return {}, False
        ranked = []
        for symbol in symbols:
            if symbol == "SPY":
                continue
            closes, _dated = _completed(histories.get(symbol), today)
            value = formation_return(closes, spec.lookback, SKIP_SESSIONS)
            if value is None:
                continue
            ranked.append((value, symbol))
        if not ranked and not regime:
            return {}, True
        if not ranked:
            return {}, False
        if not regime:
            return {}, True
        ranked.sort(key=lambda item: (-item[0], item[1]))
        picked = [symbol for _value, symbol in ranked[:spec.hold]]
        weight = 1.0 / len(picked)
        return {symbol: weight for symbol in picked}, True
    return {}, False


def _not_run(spec, capital, start, end, reason):
    return {
        "status": "not_run",
        "reason": reason,
        "spec_id": spec.id,
        "capital": capital,
        "start": start,
        "end": end,
        "curve": [],
        "trades": [],
        "costs": {"total": 0.0},
        "fills": 0,
        "skips": {},
        "avg_exposure": 0.0,
        "turnover": None,
        "traded_dollars": 0.0,
        "label": "ESTIMATED" if spec.estimated else "RESEARCH",
        "research": True,
        "estimated": spec.estimated,
        "fractional": False,
        "share_rounding": "whole_share",
        "cash_yield": None,
        "excluded": [],
        "sessions": 0,
    }


def _missing(spec, histories, cfg):
    missing = [symbol for symbol in spec.needed(cfg) if symbol not in histories]
    if spec.kind == "momentum" and spec.universe == "watchlist":
        names = [s for s in cfg.us_symbols if s in histories]
        if "SPY" not in histories:
            missing.append("SPY")
        if not names:
            missing.append("stock-eligible names")
    if spec.kind == "buy_hold" and spec.mode == "equal":
        if not any(symbol in histories for symbol in cfg.us_symbols):
            missing.append("stock-eligible names")
    return missing


def run_spec(spec, histories, cfg, *, start, end, capital, fractional=False,
             slippage_bps=10.0, iv_haircut=0.10):
    """Run one candidate on one capital. Does not write a file or a journal."""
    missing = _missing(spec, histories, cfg)
    if missing:
        return _not_run(spec, capital, start, end, "missing bars: " + ", ".join(missing))
    if spec.kind == "desk":
        return _engine(
            spec, histories, cfg, start=start, end=end, capital=capital,
            fractional=fractional, slippage_bps=slippage_bps, iv_haircut=iv_haircut,
            books=("stock", "wheel"), deploy="policy")
    if spec.kind == "desk_stock":
        return _engine(
            spec, histories, cfg, start=start, end=end, capital=capital,
            fractional=fractional, slippage_bps=slippage_bps, iv_haircut=iv_haircut,
            books=("stock",), deploy="stock")
    if spec.kind == "desk_wheel":
        return _engine(
            spec, histories, cfg, start=start, end=end, capital=capital,
            fractional=fractional, slippage_bps=slippage_bps, iv_haircut=iv_haircut,
            books=("wheel",), deploy="wheel")
    return _equity(
        spec, histories, cfg, start=start, end=end, capital=capital,
        fractional=fractional, slippage_bps=slippage_bps)


def _engine(spec, histories, cfg, *, start, end, capital, fractional, slippage_bps,
            iv_haircut, books, deploy):
    raw = run_backtest(
        histories, cfg, start=start, end=end, capital=capital, books=books,
        slippage_bps=slippage_bps, iv_haircut=iv_haircut, fractional=fractional,
        dd_reset=spec.dd_reset or None, stock_params=spec.stock_params(cfg),
        wheel_params=spec.wheel_params(cfg),
        wheel_symbols=list(spec.symbols) or None,
        put_trend_sma=spec.put_trend_sma or None,
        entry_frequency=spec.entry_frequency, research_id=spec.id, deploy=deploy)
    raw["status"] = "ok"
    raw["reason"] = ""
    raw["spec_id"] = spec.id
    raw["fills"] = len(raw["trades"])
    raw["research"] = True
    raw["estimated"] = spec.estimated or ("wheel" in books)
    raw["label"] = "ESTIMATED" if raw["estimated"] else "RESEARCH"
    raw["cash_yield"] = "none"
    raw["excluded"] = []
    return raw


def _sessions(histories, start, end):
    if "SPY" in histories:
        dates = histories["SPY"].dates
    else:
        dates = sorted({day for hist in histories.values() for day in hist.dates})
    return [day for day in dates if start <= day <= end]


def _equity(spec, histories, cfg, *, start, end, capital, fractional, slippage_bps):
    sessions = _sessions(histories, start, end)
    if len(sessions) < 2:
        return _not_run(spec, capital, start, end, "fewer than two sessions")
    excluded = []
    if spec.kind == "momentum" and spec.universe == "sector":
        symbols = list(SECTOR_ETFS)
    elif spec.kind == "momentum":
        symbols = [symbol for symbol in cfg.us_symbols if symbol in histories]
    elif spec.kind in ("trend", "dual"):
        symbols = ["SPY", "QQQ"]
    elif spec.mode == "spy":
        symbols = ["SPY"]
    else:
        first = sessions[0]
        symbols = []
        for symbol in cfg.us_symbols:
            hist = histories.get(symbol)
            if hist is None or hist.on(first) is None:
                excluded.append(symbol)
            else:
                symbols.append(symbol)
        if not symbols:
            return _not_run(spec, capital, start, end, "no name has a first-session bar")
    slip = float(slippage_bps) / 10000.0
    cost = float(cfg.policy["costs"]["overlay_per_stock_transaction"])
    accrue = spec.kind != "buy_hold"
    cash = float(capital)
    positions = {}
    trades = []
    skips = {}
    traded = 0.0
    fills = 0
    ready_days = 0
    curve = []
    yield_label = "MODEL_4PCT" if accrue else "none"
    prev = None
    held_weights = None
    for today in sessions:
        if accrue:
            rate, label = _cash_yield(histories, prev, today)
            cash *= (1.0 + rate)
            if label == "BIL":
                yield_label = "BIL"
        _rescale(positions, histories, today)
        if _rebalance_day(spec, today, prev, sessions):
            if spec.kind == "buy_hold":
                if held_weights is None:
                    if spec.mode == "spy":
                        weights, ready = {"SPY": 1.0}, True
                    else:
                        weights = {symbol: 1.0 / len(symbols) for symbol in symbols}
                        ready = True
                    held_weights = weights
                else:
                    weights, ready = held_weights, True
            else:
                weights, ready = _targets(spec, histories, today, symbols)
            if ready:
                ready_days += 1
                cash, new_fills, new_traded = _rebalance(
                    cash, positions, weights, histories, today, fractional, cost, slip,
                    trades, skips)
                fills += new_fills
                traded += new_traded
        equity, invested = _mark(cash, positions, histories, today)
        exposure = (invested / equity) if equity else 0.0
        curve.append({"date": today, "equity": equity, "exposure": exposure})
        prev = today
    status = "ok" if ready_days else "no_signal"
    reason = "" if ready_days else "the lookback never became available in this window"
    avg_equity = sum(point["equity"] for point in curve) / len(curve)
    exposures = [point["exposure"] for point in curve]
    from datetime import date
    years = (date.fromisoformat(sessions[-1]) - date.fromisoformat(sessions[0])).days / 365.25
    turnover = None
    if avg_equity and years > 0:
        turnover = (traded / 2.0) / avg_equity / years
    return {
        "status": status,
        "reason": reason,
        "spec_id": spec.id,
        "capital": float(capital),
        "start": sessions[0],
        "end": sessions[-1],
        "sessions": len(sessions),
        "curve": curve,
        "trades": trades,
        "costs": {"total": round(sum(t.get("commission") or 0.0 for t in trades), 2)},
        "fills": fills,
        "skips": skips,
        "avg_exposure": sum(exposures) / len(exposures),
        "turnover": turnover,
        "traded_dollars": round(traded, 2),
        "label": "RESEARCH",
        "research": True,
        "estimated": False,
        "fractional": bool(fractional),
        "share_rounding": "fractional_RESEARCH" if fractional else "whole_share",
        "cash_yield": yield_label,
        "excluded": excluded if spec.mode == "equal" else [],
        "assignments": {"put": 0, "call": 0},
        "experiment_block_date": None,
        "dd_reset_count": 0,
    }


def _cash_yield(histories, prev, today):
    bil = histories.get("BIL")
    if bil is not None and prev is not None:
        left, right = bil.on(prev), bil.on(today)
        if left is not None and right is not None and left.c > 0:
            return right.c / left.c - 1.0, "BIL"
    return TBILL_DAILY, "MODEL_4PCT"


def _rebalance_day(spec, today, prev, sessions):
    if spec.kind == "buy_hold":
        return prev is None
    if prev is None:
        return True
    return today[:7] != prev[:7]


def _rescale(positions, histories, today):
    for symbol, pos in positions.items():
        hist = histories.get(symbol)
        if hist is None or not hist.has_raw:
            continue
        factor = hist.split_factor(today, "c")
        prev = pos.get("split_factor")
        pos["split_factor"] = factor
        if not prev or prev <= 0 or factor <= 0:
            continue
        ratio = prev / factor
        if 0.8 < ratio < 1.25:
            continue
        pos["qty"] *= ratio


def _rebalance(cash, positions, weights, histories, today, fractional, cost, slip,
               trades, skips):
    opens = {}
    for symbol in set(list(positions) + list(weights)):
        px = _open_px(histories.get(symbol), today)
        if px is not None and px > 0:
            opens[symbol] = px
    equity = cash
    for symbol, pos in positions.items():
        px = opens.get(symbol)
        if px:
            equity += pos["qty"] * px
    targets = {}
    for symbol, weight in weights.items():
        px = opens.get(symbol)
        if px is None or weight <= 0:
            continue
        buy_fill = round_px(px * (1.0 + STOCK_HALF_SPREAD) * (1.0 + slip))
        budget = max(0.0, equity * weight - cost)
        qty = _qty_for(budget, buy_fill, fractional)
        if qty <= 0:
            skips[symbol] = skips.get(symbol, 0) + 1
            continue
        targets[symbol] = qty
    traded = 0.0
    fills = 0
    for symbol in list(positions):
        have = positions[symbol]["qty"]
        want = targets.get(symbol, 0.0)
        if have <= want + 1e-8:
            continue
        px = opens.get(symbol)
        if px is None:
            continue
        sell_qty = have - want
        fill = round_px(px * (1.0 - STOCK_HALF_SPREAD) * (1.0 - slip))
        if fill <= 0:
            continue
        proceeds = sell_qty * fill
        released = positions[symbol]["cost"] * (sell_qty / have)
        cash += proceeds - cost
        pnl = proceeds - cost - released
        positions[symbol]["cost"] -= released
        positions[symbol]["qty"] = want
        traded += proceeds
        fills += 1
        trades.append({
            "book": "RESEARCH", "symbol": symbol, "opened": today, "closed": today,
            "qty": sell_qty, "pnl": round(pnl, 2), "commission": cost, "side": "sell",
        })
        if positions[symbol]["qty"] <= 1e-8:
            del positions[symbol]
    for symbol, want in targets.items():
        have = positions[symbol]["qty"] if symbol in positions else 0.0
        buy_qty = want - have
        if buy_qty <= 1e-8:
            continue
        px = opens[symbol]
        fill = round_px(px * (1.0 + STOCK_HALF_SPREAD) * (1.0 + slip))
        spend = buy_qty * fill + cost
        if spend > cash + 1e-9:
            buy_qty = _qty_for(max(0.0, cash - cost), fill, fractional)
            spend = buy_qty * fill + cost if buy_qty else 0.0
        if buy_qty <= 0 or spend > cash + 1e-9:
            skips[symbol] = skips.get(symbol, 0) + 1
            continue
        cash -= spend
        traded += buy_qty * fill
        fills += 1
        if symbol not in positions:
            factor = 1.0
            hist = histories.get(symbol)
            if hist is not None and hist.has_raw:
                factor = hist.split_factor(today, "o")
            positions[symbol] = {"qty": 0.0, "cost": 0.0, "split_factor": factor}
        positions[symbol]["qty"] += buy_qty
        positions[symbol]["cost"] += spend
        trades.append({
            "book": "RESEARCH", "symbol": symbol, "opened": today, "closed": None,
            "qty": buy_qty, "pnl": None, "commission": cost, "side": "buy",
        })
    # The caller passes `fills` as a running total. Return the increment.
    return cash, fills, traded


def _mark(cash, positions, histories, today):
    invested = 0.0
    for symbol, pos in positions.items():
        px = _close_px(histories.get(symbol), today)
        if px is None:
            px = pos["cost"] / pos["qty"] if pos["qty"] else 0.0
        invested += pos["qty"] * px
    return round(cash + invested, 2), invested
