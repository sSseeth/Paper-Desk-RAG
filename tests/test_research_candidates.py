"""RESEARCH candidate tests. Synthetic bars only. No network and no journal."""
import json
import os
import shutil
from datetime import date, timedelta

import pytest

from sakibdesk.backtest.candidates import (
    BY_ID,
    GRID,
    SPLIT_ARTIFACT,
    formation_return,
    grid_counts,
    resolve_strategies,
    run_spec,
    select_winner,
)
from sakibdesk.backtest.compare import JSON_NAME, MD_NAME, run_comparison
from sakibdesk.backtest.data import Bar, History
from sakibdesk.config import Config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HASH = "2df03f83726ac139"

_RESEARCH_ARGV = [
    "backtest",
    "--start", "2021-01-04",
    "--end", "2026-01-02",
    "--output-dir", "reports\\research",
    "--strategies", "all",
    "--fractional",
    "--dd-reset",
    "--fetch",
]


def _dates(n, start="2020-01-02"):
    cursor = date.fromisoformat(start)
    out = []
    while len(out) < n:
        if cursor.weekday() < 5:
            out.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return out


def _trend(dates, start, step):
    bars, px = [], float(start)
    for day in dates:
        close = px + step
        bars.append(Bar(day, px, max(px, close) + 0.05, min(px, close) - 0.05, close, 1000))
        px = close
    return bars


def _cfg():
    return Config(ROOT)


def test_grid_is_predeclared_and_locked():
    candidates, robust = grid_counts()
    assert candidates == 19
    assert robust == 4
    assert len(GRID) == 23
    assert [spec.id for spec in GRID if spec.role == "candidate"] == [
        "shipped", "shipped_dd_reset",
        "momentum_3_6m", "momentum_3_12m", "momentum_5_6m", "momentum_5_12m",
        "trend_200", "trend_10m", "dual_momentum",
        "wheel_index", "wheel_cheap", "wheel_trend", "wheel_delta15", "wheel_dte45_tp25",
        "stock_weekly", "stock_hold60", "stock_lookback63",
        "bh_spy", "bh_equal_weight",
    ]
    assert resolve_strategies("all") == list(GRID)
    assert [spec.id for spec in resolve_strategies("momentum") if spec.role == "candidate"] == [
        "momentum_3_6m", "momentum_3_12m", "momentum_5_6m", "momentum_5_12m",
    ]
    with pytest.raises(ValueError):
        resolve_strategies("not-a-strategy")
    assert "100" in SPLIT_ARTIFACT
    assert "unadjusted" in SPLIT_ARTIFACT


def test_selection_uses_in_sample_sharpe_only():
    rows = [
        {"id": "slow", "sharpe": 1.0, "annualised_return": 0.05, "max_drawdown": 0.20,
         "eligible": True},
        {"id": "lucky", "sharpe": 0.2, "annualised_return": 0.80, "max_drawdown": 0.01,
         "eligible": True},
    ]
    assert select_winner(rows) == "slow"
    tied = [
        {"id": "b", "sharpe": 1.0, "annualised_return": 0.2, "max_drawdown": 0.1, "eligible": True},
        {"id": "a", "sharpe": 1.0, "annualised_return": 0.2, "max_drawdown": 0.1, "eligible": True},
    ]
    assert select_winner(tied) == "a"
    assert select_winner([
        {"id": "flat", "sharpe": None, "annualised_return": 0.04, "max_drawdown": 0.0,
         "eligible": False},
    ]) is None
    closes = list(range(1, 400))
    assert formation_return(closes, 126, 21) is not None
    assert formation_return(closes[:10], 126, 21) is None


def test_momentum_holds_the_leaders_and_sits_out_a_downtrend():
    dates = _dates(260)
    cfg = _cfg()
    histories = {
        "MU": History(_trend(dates, 20, 0.02)),
        "AMD": History(_trend(dates, 20, 0.15)),
        "NVDA": History(_trend(dates, 20, 0.40)),
        "AVGO": History(_trend(dates, 20, 0.80)),
        "SPY": History(_trend(dates, 100, 0.30)),
    }
    held = run_spec(
        BY_ID["momentum_3_6m"], histories, cfg, start=dates[0], end=dates[-1],
        capital=100000.0, fractional=False)
    assert held["status"] == "ok"
    assert held["label"] == "RESEARCH"
    buys = [trade["symbol"] for trade in held["trades"] if trade.get("side") == "buy"]
    assert "AVGO" in buys and "NVDA" in buys and "AMD" in buys
    assert "MU" not in buys
    down = dict(histories)
    down["SPY"] = History(_trend(dates, 400, -0.8))
    cash = run_spec(
        BY_ID["momentum_3_6m"], down, cfg, start=dates[0], end=dates[-1], capital=100000.0)
    assert cash["fills"] == 0
    assert cfg.config_hash == HASH


def test_trend_and_dual_are_labelled_and_do_not_touch_the_hash():
    dates = _dates(400)
    cfg = _cfg()
    histories = {
        "SPY": History(_trend(dates, 100, 0.2)),
        "QQQ": History(_trend(dates, 80, 0.3)),
    }
    trend = run_spec(
        BY_ID["trend_200"], histories, cfg, start=dates[220], end=dates[-1], capital=10000.0)
    dual = run_spec(
        BY_ID["dual_momentum"], histories, cfg, start=dates[0], end=dates[-1], capital=10000.0)
    assert trend["status"] == "ok" and trend["fills"] > 0
    assert dual["status"] == "ok"
    assert cfg.config_hash == HASH


def test_walk_forward_does_not_promote_the_longer_lookback():
    """12-month momentum cannot trade in the short in-sample window.

    It can trade out of sample, once warmup exists. Selection must still
    refuse it. The headline is a 6-month variant.
    """
    dates = _dates(280)
    cfg = _cfg()
    histories = {
        "MU": History(_trend(dates, 20, 0.02)),
        "AMD": History(_trend(dates, 20, 0.15)),
        "NVDA": History(_trend(dates, 20, 0.40)),
        "AVGO": History(_trend(dates, 20, 0.80)),
        "SPY": History(_trend(dates, 100, 0.30)),
    }
    # Warmup covers the 200-day average and the 6-month return. The in-sample
    # slice ends before a 12-month return exists, so that variant cannot win.
    specs = [spec for spec in resolve_strategies("momentum") if spec.role == "candidate"]
    report = run_comparison(
        histories, cfg, start=dates[200], end=dates[-1], capitals=[100000.0], specs=specs)
    family = next(block for block in report["selection"] if block["family"] == "momentum")
    assert family["chosen"] in ("momentum_3_6m", "momentum_5_6m")
    headline = [row["id"] for row in report["ranked_oos"] if row["family"] == "momentum"]
    assert headline == [family["chosen"]]
    appendix = {row["id"] for row in report["appendix"]}
    assert "momentum_3_12m" in appendix and "momentum_5_12m" in appendix
    assert family["chosen"] not in appendix


def test_comparison_report_ranks_out_of_sample(tmp_path):
    dates = _dates(80, "2021-01-04")
    histories = {
        "SPY": History(_trend(dates, 100, 0.2)),
        "MU": History(_trend(dates, 20, 0.1)),
    }
    report = run_comparison(
        histories, _cfg(), start=dates[0], end=dates[-1], capitals=[1000.0, 10000.0],
        specs=resolve_strategies("benchmark"), fractional=False)
    assert report["label"] == "RESEARCH"
    assert report["config_hash"] == HASH
    assert report["variants_tried"] == 2
    assert report["predeclared_candidates"] == 19
    assert report["walk_forward"]["headline_is"] == "out_of_sample"
    ids = [row["id"] for row in report["ranked_oos"]]
    assert "bh_spy" in ids and "bh_equal_weight" in ids
    assert all("verdict" in row for row in report["ranked_oos"])
    text_ids = " ".join(ids)
    assert "Sakib" not in text_ids
    assert "Sakib" not in report["split_artifact"]


def _fake_bars(symbol):
    if symbol not in ("SPY", "MU"):
        return []
    rows, cursor, count, px = [], date(2021, 1, 4), 0, 100.0
    while count < 15:
        if cursor.weekday() < 5:
            rows.append({
                "t": cursor.isoformat() + "T05:00:00Z",
                "o": px, "h": px + 1, "l": px - 0.5, "c": px + 0.4, "v": 1000,
            })
            count += 1
            px += 0.4
        cursor += timedelta(days=1)
    return rows


class _Bars:
    instances = []

    def __init__(self, key_id, secret, stock_feed, option_feed):
        self.key_id = key_id
        self.secret = secret
        self.calls = []
        _Bars.instances.append(self)

    def daily_bars(self, symbols, start, adjustment="split"):
        self.calls.append(adjustment)
        return {symbols[0]: _fake_bars(symbols[0])}


def test_main_research_argv_matches_the_readme(monkeypatch, tmp_path, capsys):
    """The RESEARCH command printed in the README, through desk.main."""
    import desk
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    command = " ".join(_RESEARCH_ARGV)
    assert command.replace("backtest ", "py desk.py backtest ") in readme
    monkeypatch.chdir(tmp_path)
    _Bars.instances = []
    monkeypatch.setattr("sakibdesk.secrets_store.get", lambda name, root: {
        "ALPACA_PAPER_KEY_ID": "test-key-id",
        "ALPACA_PAPER_SECRET": "test-secret",
    }.get(name))
    monkeypatch.setattr("sakibdesk.alpaca.MarketData", _Bars)
    monkeypatch.setattr(
        "sakibdesk.alpaca.PaperReader",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no trading client")))
    monkeypatch.setattr(
        "sakibdesk.alpaca.PaperWriter",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("no trading client")))
    journal = os.path.join(ROOT, "data", "journal.sqlite3")
    before = os.path.exists(journal)
    cache = os.path.join(ROOT, "data", "backtest_cache")
    try:
        code = desk.main(list(_RESEARCH_ARGV))
    finally:
        if os.path.isdir(cache):
            shutil.rmtree(cache)
    out = capsys.readouterr().out
    assert code == 0, out
    report_dir = tmp_path / "reports\\research"
    body = json.loads((report_dir / JSON_NAME).read_text(encoding="utf-8"))
    text = (report_dir / MD_NAME).read_text(encoding="utf-8")
    assert body["label"] == "RESEARCH"
    assert body["config_hash"] == HASH
    assert body["variants_tried"] == 19
    assert body["predeclared_candidates"] == 19
    assert body["fractional"] is True
    assert body["dd_reset_flag"] is True
    assert "out of sample" in text.lower() or "Out of sample" in text or "out-of-sample" in text
    assert "Beat SPY" in text
    assert "RESEARCH" in text
    assert "100 times" in text or "100 x" in text or "100 times the" in text or "100 times" in body["split_artifact"]
    assert os.path.exists(journal) == before
    assert not list(tmp_path.rglob("*.sqlite*"))
    assert _Bars.instances
    assert {"split", "raw", "all"} <= set(_Bars.instances[0].calls)
    assert desk.main([
        "backtest", "--start", "2021-01-04", "--end", "2021-06-01",
        "--strategies", "not-a-strategy",
    ]) == 2
