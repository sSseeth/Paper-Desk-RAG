"""Signal/ATR/size/rank, stale feed, earnings exclusion, early close and DST from broker calendar."""
import json
from datetime import datetime, timezone
from conftest import bars_trend, make_calendar, ROOT
from sakibdesk import stock as S
from sakibdesk.indicators import atr_wilder
from sakibdesk.calendar_util import Session, current_session

P = json.load(open(ROOT + "/policy.json"))["stock"]


def test_signal_and_min_bars():
    sig, why = S.signal(bars_trend(), P)
    assert why == "ok" and sig["R"] == 2 * sig["atr14"]
    assert S.signal(bars_trend(n=219), P)[0] is None
    assert S.signal(bars_trend(breakout=False), P)[1] == "no 20-session breakout"


def test_wilder_atr():
    bars = [{"h": 11, "l": 9, "c": 10}] * 15
    assert abs(atr_wilder(bars, 14) - 2.0) < 1e-9


def test_completed_bars_excludes_today():
    b = bars_trend(end_date="2026-10-28")
    assert S.completed_bars(b, "2026-10-28")[-1]["t"][:10] == "2026-10-27"


def test_size_and_rank():
    assert S.size(20000, 100, 4, 20000, P) == 10          # 5% cap = 10 shares; risk 0.25%*20000/4 = 12.5
    assert S.size(20000, 100, 10, 20000, P) == 5          # risk binds
    assert S.size(20000, 100, 4, 300, P) == 3             # free cash binds
    r = S.rank([{"symbol": "B", "ret63": 0.1}, {"symbol": "A", "ret63": 0.1}, {"symbol": "C", "ret63": 0.3}])
    assert [x["symbol"] for x in r] == ["C", "A", "B"]


def test_stale_and_wide_quotes():
    now = datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc)
    assert S.quote_ok({"bp": 99.98, "ap": 100.02, "t": "2026-10-28T14:14:30.123456789Z"}, now, 60, 0.005)[0]
    assert S.quote_ok({"bp": 99.98, "ap": 100.02, "t": "2026-10-28T14:13:00Z"}, now, 60, 0.005)[1] == "quote stale"
    assert S.quote_ok({"bp": 99, "ap": 101, "t": "2026-10-28T14:14:59Z"}, now, 60, 0.005)[1] == "spread too wide"
    assert S.quote_ok({"bp": 0, "ap": 101, "t": "2026-10-28T14:14:59Z"}, now, 60, 0.005)[0] is False


def test_earnings_block_window():
    cal = make_calendar("2026-10-01", "2026-12-31")
    f = lambda today, e=None, l=None: S.earnings_blocked(cal, today, e, l, P)
    assert f("2026-10-28", "UNKNOWN")
    assert f("2026-11-03", "2026-11-05", "2026-11-05")       # 2 sessions before
    assert not f("2026-11-02", "2026-11-05", "2026-11-05")
    assert f("2026-11-09", "2026-11-05", "2026-11-05")       # 2 sessions after
    assert not f("2026-11-10", "2026-11-05", "2026-11-05")


def test_early_close_window():
    s = Session({"date": "2026-11-27", "open": "09:30", "close": "13:00"})   # day after Thanksgiving
    assert s.close == datetime(2026, 11, 27, 18, 0, tzinfo=timezone.utc)
    late = datetime(2026, 11, 27, 17, 30, tzinfo=timezone.utc)                # 12:30 ET
    assert not s.in_window(late, 60, stop_before_close_min=60)


def test_dst_transition_uses_named_zone():
    before = Session({"date": "2026-10-30", "open": "09:30", "close": "16:00"})
    after = Session({"date": "2026-11-02", "open": "09:30", "close": "16:00"})
    assert before.open.hour == 13 and after.open.hour == 14
    cal = [{"date": "2026-11-02", "open": "09:30", "close": "16:00"}]
    assert current_session(cal, datetime(2026, 11, 2, 13, 45, tzinfo=timezone.utc)) is None
    assert current_session(cal, datetime(2026, 11, 2, 14, 45, tzinfo=timezone.utc)) is not None


def test_bracket_body_paper_fields():
    b, stop, tgt = S.bracket_body("MU", 3, 100.0, 4.0, P)
    assert b["order_class"] == "bracket" and b["position_intent"] == "buy_to_open" and (stop, tgt) == (96.0, 112.0)


def test_exit_due():
    cal = make_calendar("2025-06-01", "2026-12-31")
    bars = bars_trend(n=60, breakout=False)
    assert S.exit_due(bars, "2026-09-25", "2026-10-28", cal, P) == "MAX_HOLDING_20_SESSIONS"
    bars[-1]["c"] = 1
    assert S.exit_due(bars, "2026-10-20", "2026-10-28", cal, P) == "CLOSE_BELOW_SMA50"
