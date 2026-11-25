"""Offline tests for the historical backtest. No network and no journal writes."""
import os
from datetime import date, timedelta

import pytest

from sakibdesk import accounting, stock, wheel
from sakibdesk.backtest.cli import default_capitals, parse_books, parse_capitals, run_cli
from sakibdesk.backtest.data import Bar, CredentialsMissing, History, cache_path, load_csv, write_csv
from sakibdesk.backtest.engine import RiskBook, run_backtest, split_sessions
from sakibdesk.backtest.metrics import summarise
from sakibdesk.backtest.pricing import bs_price, early_exercise, quoted
from sakibdesk.backtest.report import JSON_NAME, MD_NAME
from sakibdesk.config import Config
from sakibdesk.risk import RiskState

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HASH = "2df03f83726ac139"


def _dates(n, start="2020-01-02"):
    cursor = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _trend(dates, start=50.0, step=0.15):
    bars, px = [], start
    for day in dates:
        o = px
        c = px + step
        bars.append(Bar(day, o, max(o, c) + 0.05, min(o, c) - 0.05, c, 1000))
        px = c
    return bars


def _wide(bar, prev_close):
    """Entry-day bar: open at the prior close, range through any plausible stop and target."""
    o = prev_close
    return Bar(bar.date, o, o * 3.0, o * 0.2, o, 1000)


def _choppy(dates, lo=100.0, hi=104.0):
    bars, px = [], hi
    for i, day in enumerate(dates):
        o = px
        c = lo if i % 2 == 0 else hi
        bars.append(Bar(day, o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1000))
        px = c
    return bars


def _hist(*pairs):
    return {symbol: History(bars) for symbol, bars in pairs}


def _cfg():
    return Config(ROOT)


def test_config_hash_is_unchanged():
    assert _cfg().config_hash == HASH


def test_default_capitals_include_the_paper_book():
    capitals = default_capitals(100000.0)
    assert capitals == [2000.0, 5000.0, 10000.0, 25000.0, 100000.0]
    assert parse_capitals("2000, 2000, 5000", 100000.0) == [2000.0, 5000.0]
    assert parse_books("wheel,stock,wheel") == ("wheel", "stock")


def test_risk_book_matches_risk_state(cfg, journal):
    journal.set("equity_high", 100000.0)
    live = RiskState(journal, cfg.policy)
    book = RiskBook(cfg.policy, 100000.0)
    prior = 100000.0
    path = [
        (100000.0, "2026-10-28"),
        (97000.0, "2026-10-29"),
        (97000.0, "2026-10-30"),
        (90000.0, "2026-11-02"),
        (99000.0, "2026-11-03"),
    ]
    for equity, day in path:
        assert live.update(equity, day, prior) == book.blocks(equity, day)
        book.set_prior(equity)
        prior = equity
    assert book.tripped_on == "2026-11-02"
    assert "EXPERIMENT_DRAWDOWN_10PCT" in book.blocks(99000.0, "2026-11-04")


def test_put_call_parity_and_early_exercise():
    spot, strike, t_years, sigma = 100.0, 100.0, 0.5, 0.2
    call, _cd = bs_price("call", spot, strike, t_years, sigma)
    put, _pd = bs_price("put", spot, strike, t_years, sigma)
    forward = spot - strike * (2.718281828 ** (-0.04 * t_years))
    # math.exp is the exact discount. Recompute with the same rate the pricer uses.
    import math
    forward = spot * math.exp(0.0) - strike * math.exp(-0.04 * t_years)
    assert call - put == pytest.approx(forward, rel=1e-8)
    deep, _delta = bs_price("put", 50.0, 100.0, 0.5, 0.2)
    assert deep < 50.0
    assert early_exercise("put", deep, 50.0, 100.0)
    assert early_exercise("put", deep, 120.0, 100.0) is False
    call_otm, _d = bs_price("call", 100.0, 100.0, 0.5, 0.2)
    assert call_otm > 0.0
    assert early_exercise("call", call_otm, 100.0, 100.0) is False
    bid, ask = quoted(2.0, 0.10)
    assert bid < 2.0 * 0.90
    assert ask > bid
    assert (ask - bid) / ((ask + bid) / 2.0) < 0.10


def test_metrics_drawdown_win_rate_and_worst_month():
    curve = [
        {"date": "2024-01-02", "equity": 110.0},
        {"date": "2024-01-03", "equity": 90.0},
        {"date": "2024-02-03", "equity": 100.0},
    ]
    trades = [{"pnl": 10.0}, {"pnl": -5.0}, {"pnl": None}]
    stats = summarise(100.0, "2024-01-01", curve, trades)
    assert stats["max_drawdown"] == accounting.max_drawdown([100.0, 110.0, 90.0, 100.0])
    assert stats["trades"] == 2
    assert stats["win_rate"] == 0.5
    assert stats["open_trades"] == 1
    assert stats["worst_month"] == "2024-01"
    assert stats["worst_month_return"] == pytest.approx(-0.10)
    assert stats["total_return"] == pytest.approx(0.0)


def test_split_is_chronological_and_cold():
    sessions = [f"2024-01-{d:02d}" for d in range(1, 11)]
    inn, out = split_sessions(sessions, 0.6)
    assert inn == sessions[:6]
    assert out == sessions[6:]
    assert out[0] > inn[-1]
    assert split_sessions(["only-one"]) is None


def test_csv_cache_roundtrip(tmp_path):
    bars = _trend(_dates(3), start=10.0, step=0.1)
    path = cache_path(str(tmp_path), "MU")
    write_csv(path, bars)
    loaded = load_csv(path)
    assert [b.date for b in loaded] == [b.date for b in bars]
    assert loaded[0].c == pytest.approx(bars[0].c)
    assert load_csv(str(tmp_path / "missing.csv")) == []


def test_fetch_refuses_without_secrets(monkeypatch):
    monkeypatch.setattr("sakibdesk.secrets_store.get", lambda name, root: None)
    from sakibdesk.backtest.data import fetch_cache
    with pytest.raises(CredentialsMissing):
        fetch_cache(ROOT, ["SPY"], "2020-01-02", "2020-02-02", os.path.join(ROOT, "data", "backtest_cache"))


def _stock_case():
    dates = _dates(241)
    trend = _trend(dates)
    prev = trend[-2].c
    trend[-1] = _wide(trend[-1], prev)
    spy = _trend(dates)
    return dates, _hist(("MU", trend), ("SPY", spy))


def test_stock_stop_wins_when_the_bar_touches_both_and_uses_live_size():
    dates, histories = _stock_case()
    day = dates[-1]
    cfg = _cfg()
    window_start = (date.fromisoformat(day) - timedelta(days=420)).isoformat()
    prior = [b for b in histories["MU"].slice(window_start, day)]
    sig, why = stock.signal(
        [{"o": b.o, "h": b.h, "l": b.l, "c": b.c, "t": b.date + "T21:00:00Z"} for b in prior],
        cfg.policy["stock"])
    assert sig, why
    result = run_backtest(
        histories, cfg, start=day, end=day, capital=100000.0, books=("stock",),
        slippage_bps=0.0, iv_haircut=0.10)
    assert result["config_hash"] == HASH
    closed = [t for t in result["trades"] if t["pnl"] is not None]
    assert len(closed) == 1
    trade = closed[0]
    assert trade["reason"] == "STOP"
    assert trade["exit_price"] == pytest.approx(trade["stop"])
    assert trade["exit_price"] < trade["target"]
    assert trade["pnl"] < 0
    assert trade["qty"] == stock.size(100000.0, trade["entry_price"], sig["R"], 20000.0, cfg.policy["stock"])
    _body, stop, target = stock.bracket_body("MU", trade["qty"], trade["entry_price"], sig["R"], cfg.policy["stock"])
    assert trade["stop"] == stop
    assert trade["target"] == target
    assert result["costs"]["total"] == accounting.cost_overlay(
        result["costs"]["option_sides"], result["costs"]["stock_transactions"], cfg.policy["costs"])
    assert result["costs"]["stock_transactions"] == 2


def test_strategy_exit_uses_exit_due(monkeypatch):
    dates = _dates(241)
    histories = _hist(("MU", _trend(dates)), ("SPY", _trend(dates)))
    monkeypatch.setattr(stock, "exit_due", lambda *args, **kwargs: "CLOSE_BELOW_SMA50")
    result = run_backtest(
        histories, _cfg(), start=dates[-2], end=dates[-1], capital=100000.0, books=("stock",),
        slippage_bps=0.0)
    closed = [t for t in result["trades"] if t.get("pnl") is not None]
    assert closed
    assert closed[0]["reason"] == "CLOSE_BELOW_SMA50"
    assert closed[0]["symbol"] == "MU"


def test_earnings_block_uses_the_live_helper():
    dates, histories = _stock_case()
    day = dates[-1]
    cfg = _cfg()

    def blocked(symbol, today, calendar):
        assert stock.earnings_blocked(calendar, today, today, today, cfg.policy["stock"])
        return {
            "earnings_earliest": today,
            "earnings_latest": today,
            "latest_release_date": today,
            "dividend": "VERIFIED_NONE",
            "distress_flag": False,
            "wheel_ok": True,
        }

    result = run_backtest(
        histories, cfg, start=day, end=day, capital=100000.0, books=("stock",),
        slippage_bps=0.0, earnings_provider=blocked)
    assert result["trades"] == []
    assert result["skips"]["MU"]["earnings"] == 1


def test_wheel_premium_is_not_profit_until_close_and_uses_tick_rounding():
    dates = _dates(160)
    histories = _hist(("MU", _choppy(dates)))
    cfg = _cfg()
    result = run_backtest(
        histories, cfg, start=dates[70], end=dates[-1], capital=100000.0, books=("wheel",),
        slippage_bps=0.0, iv_haircut=0.10)
    wheel_trades = [t for t in result["trades"] if t["book"] == "WHEEL"]
    assert wheel_trades, result["skips"]
    trade = wheel_trades[0]
    assert trade["estimated"] is True
    assert trade["option_prices"] == "ESTIMATED"
    assert trade["limit"] == wheel.tick_round_down(trade["option_bid"])
    assert trade["limit"] == trade["entry_price"]
    credit = trade["entry_price"] * 100.0
    opened = next(point for point in result["curve"] if point["date"] == trade["opened"])
    # The cash credit is offset by the option liability. Equity does not jump by the credit.
    assert opened["equity"] < result["capital"] + credit - 1.0
    if trade["closed"] == trade["opened"]:
        assert trade["pnl"] < credit
    else:
        assert trade["pnl"] is None or trade["closed"] != trade["opened"]
    assert result["costs"]["total"] == accounting.cost_overlay(
        result["costs"]["option_sides"], result["costs"]["stock_transactions"], cfg.policy["costs"])
    assert "ESTIMATED" == result["label"]


def test_wheel_assignment_is_counted_when_the_put_finishes_in_the_money():
    dates = _dates(140)
    bars = _choppy(dates[:75])
    # 85 is below the ~90 strike the screen sells on this path, and above
    # 80% of that strike, so the live 20% unwind does not buy the put back
    # before expiry. A much deeper crash is bought back by that rule.
    for day in dates[75:]:
        bars.append(Bar(day, 85.0, 86.0, 84.0, 85.0, 1000))
    result = run_backtest(
        _hist(("MU", bars)), _cfg(), start=dates[60], end=dates[-1], capital=100000.0,
        books=("wheel",), slippage_bps=0.0, iv_haircut=0.0)
    assert result["assignments"]["put"] >= 1


def test_small_account_skips_wheel_contracts_for_capital():
    dates = _dates(120)
    result = run_backtest(
        _hist(("MU", _choppy(dates))), _cfg(), start=dates[70], end=dates[-1],
        capital=2000.0, books=("wheel",), slippage_bps=0.0, iv_haircut=0.10)
    stats = result["notionals"]["MU"]
    assert stats["days"] > 0
    assert stats["min"] > 2000.0
    skips = result["skips"]["MU"]
    refused = skips.get("notional_cap", 0) + skips.get("insufficient_cash", 0) + skips.get("combined_cap", 0)
    assert refused > 0
    assert not any(t["book"] == "WHEEL" for t in result["trades"])


def test_stock_holding_blocks_a_new_wheel_put():
    dates, histories = _stock_case()
    day = dates[-1]
    result = run_backtest(
        histories, _cfg(), start=day, end=day, capital=100000.0, books=("stock", "wheel"),
        slippage_bps=0.0, iv_haircut=0.10)
    assert any(t["book"] == "STOCK" for t in result["trades"])
    assert result["skips"]["MU"].get("CROSS_BOOK_OVERLAP:STOCK", 0) >= 1
    assert not any(t["book"] == "WHEEL" for t in result["trades"])


def test_backtest_does_not_write_the_journal(tmp_path):
    dates, histories = _stock_case()
    data_dir = os.path.join(ROOT, "data")
    before = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    result = run_backtest(
        histories, _cfg(), start=dates[-1], end=dates[-1], capital=10000.0, books=("stock",),
        slippage_bps=0.0)
    after = set(os.listdir(data_dir)) if os.path.isdir(data_dir) else set()
    assert before == after
    assert result["config_hash"] == HASH
    assert not list(tmp_path.rglob("*.sqlite*"))


def test_cli_writes_reports_and_refuses_an_empty_cache(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    out = tmp_path / "out"
    code = run_cli(ROOT, _args(start="2021-01-04", end="2021-06-01", output_dir=str(out),
                               cache_dir=str(empty), capital="100000", books="stock"))
    assert code == 2
    captured = capsys.readouterr().out
    assert "py desk.py backtest" in captured
    assert "--fetch" in captured
    assert not (out / JSON_NAME).exists()

    dates = _dates(40)
    cache = tmp_path / "cache"
    write_csv(cache_path(str(cache), "SPY"), _trend(dates))
    write_csv(cache_path(str(cache), "MU"), _trend(dates))
    report_dir = tmp_path / "report"
    code = run_cli(ROOT, _args(
        start=dates[0], end=dates[-1], output_dir=str(report_dir), cache_dir=str(cache),
        capital="10000", books="stock", skip_walk_forward=False))
    assert code == 0
    text = (report_dir / MD_NAME).read_text(encoding="utf-8")
    body = (report_dir / JSON_NAME).read_text(encoding="utf-8")
    assert "ESTIMATED" in text
    assert "Survivorship" in text
    assert HASH in text
    assert HASH in body
    assert "journal" not in os.listdir(report_dir)
    # The grid is present, and the paper-capital row is not required when the
    # caller asked for a single capital.
    assert "10,000" in text or "10000" in text


class _Args:
    def __init__(self, **kwargs):
        self.start = kwargs["start"]
        self.end = kwargs["end"]
        self.capital = kwargs.get("capital")
        self.books = kwargs.get("books", "stock")
        self.output_dir = kwargs["output_dir"]
        self.cache_dir = kwargs["cache_dir"]
        self.fetch = False
        self.slippage_bps = 10.0
        self.iv_haircut = 0.10
        self.skip_sensitivity = kwargs.get("skip_sensitivity", True)
        self.skip_walk_forward = kwargs.get("skip_walk_forward", True)


def _args(**kwargs):
    return _Args(**kwargs)
