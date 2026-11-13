"""Risk blocks, resume refusal, accounting/benchmarks incl. adverse move despite premium."""
from sakibdesk.risk import RiskState
from sakibdesk import accounting as AC


def test_daily_and_experiment_blocks(cfg, journal):
    r = RiskState(journal, cfg.policy)
    assert r.update(100000, "2026-10-28", 100000) == []
    assert "DAILY_LOSS_2PCT" in r.update(98000, "2026-10-28", 100000)
    assert "DAILY_LOSS_2PCT" not in r.update(98000, "2026-10-29", 98000)
    assert "EXPERIMENT_DRAWDOWN_10PCT" in r.update(90000, "2026-10-30", 98000)
    assert "EXPERIMENT_DRAWDOWN_10PCT" in r.update(99000, "2026-11-02", 90000)   # sticky


def test_healthy_streak(cfg, journal):
    r = RiskState(journal, cfg.policy)
    assert [r.healthy(True) for _ in range(3)] == [False, False, True]
    assert r.healthy(False) is False


def test_resume_refused_without_streak_or_with_block(monkeypatch, cfg, journal):
    import desk
    monkeypatch.setattr(desk, "_doctor_ok", lambda cfg, account="main": True, raising=False)
    journal.set("paused", True); journal.set("healthy_streak", 1)
    assert desk.resume_allowed(journal, cfg)[0] is False
    journal.set("healthy_streak", 3); journal.set("experiment_block", True)
    assert desk.resume_allowed(journal, cfg)[0] is False


def test_premium_is_cash_plus_liability_and_adverse_move():
    cash, liab = AC.premium_open(80000, 0, 3.0, 1)
    assert (cash, liab) == (80300, 300)
    eq0 = AC.conservative_equity(cash, [], [{"qty": 1, "ask": 3.0}])
    assert eq0 == 80000                                         # equity unchanged at open
    eq1 = AC.conservative_equity(cash, [], [{"qty": 1, "ask": 25.0}])
    assert eq1 < 80000                                          # loses despite collected premium
    assert AC.conservative_equity(cash, [], [{"qty": 1, "ask": None}]) is None


def test_spy_benchmarks_and_overlay():
    b = AC.spy_benchmarks(500.0, {"initial": 100000, "stock": 20000, "wheel": 80000})
    assert b["COMBINED_vs_SPY"]["qty"] == 199 and b["STOCK_vs_SPY"]["qty"] == 39 and b["WHEEL_vs_SPY"]["qty"] == 159
    assert AC.cost_overlay(4, 2, {"overlay_per_option_contract_per_side": 1.0, "overlay_per_stock_transaction": 1.0}) == 6
    w = AC.wheel_vs_same_stock([{"cycle_id": "c", "reserved": 15002, "first_put_entry_px": 160, "end_px": 120}])
    assert w[0]["bh_pnl"] < 0
    assert AC.max_drawdown([100, 120, 90, 130]) == 0.25
