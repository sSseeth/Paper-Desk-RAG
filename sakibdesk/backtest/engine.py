"""Daily simulator for the shipped stock book and wheel book.

Rules are the live functions: signal, size, bracket, exit_due, rank,
screen_contracts, put_collateral, tick rounding, management_actions, and the
wheel state machine. The daily loop follows cycle.stock_entries,
cycle.wheel_entries, manage_stock_exits, and manage_wheel, with one decision
inside each book's entry window.

The simulator does not construct a broker client and does not open a journal.
Option prices are Black-Scholes estimates. Fills are a daily approximation.
"""

import math
from datetime import date, timedelta

from sakibdesk import accounting, stock, wheel
from sakibdesk.backtest.data import stock_quote
from sakibdesk.backtest.pricing import (
    ENTRY_HALF_SPREAD,
    STOCK_HALF_SPREAD,
    bs_price,
    early_exercise,
    quoted,
    realized_vol,
    year_fraction,
)
from sakibdesk.calendar_util import Session, session_offset
from sakibdesk.indicators import pct_return, sma

# Live stock and wheel indicator calls see bars from today minus this many
# calendar days (cycle._bars_start). ATR is path-dependent, so the window
# must match or the stop distance will not.
BAR_LOOKBACK_DAYS = 420
EARNINGS_CLEAR_DAYS = 400
IN_SAMPLE_FRACTION = 0.6


def sleeve_capitals(policy, capital):
    """Policy stock/wheel split applied to a scenario's starting capital.

    The percents stay the policy percents. policy.json is not modified.
    """
    initial = float(policy["capital"]["initial"])
    stock_cap = capital * (float(policy["capital"]["stock"]) / initial)
    wheel_cap = capital * (float(policy["capital"]["wheel"]) / initial)
    return stock_cap, wheel_cap


class RiskBook:
    """Same inequalities as risk.RiskState.update, without a journal.

    The paper journal is not opened. `equity_high` starts at the scenario
    capital, which is the experiment's starting equity.
    """

    def __init__(self, policy, start_equity):
        self.daily_pct = policy["risk"]["daily_loss_pct"]
        self.dd_pct = policy["risk"]["experiment_drawdown_pct"]
        self.high = float(start_equity)
        self.prior = float(start_equity)
        self.experiment = False
        self.daily_session = None
        self.tripped_on = None
        self.daily_blocks = 0

    def blocks(self, equity, session):
        self.high = max(self.high, equity)
        blocked = []
        if self.prior and equity <= self.prior * (1.0 - self.daily_pct):
            self.daily_session = session
        if self.daily_session == session:
            blocked.append("DAILY_LOSS_2PCT")
        if equity <= self.high * (1.0 - self.dd_pct):
            self.experiment = True
            if self.tripped_on is None:
                self.tripped_on = session
        if self.experiment:
            blocked.append("EXPERIMENT_DRAWDOWN_10PCT")
        return blocked

    def set_prior(self, equity):
        self.prior = equity
        self.high = max(self.high, equity)


def build_calendar(session_dates):
    """Alpaca-shaped calendar: real sessions, then extra weekdays after the end.

    The extra weekdays exist so a synthetic earnings date past the sample
    still resolves in session_offset. They are not trading sessions. No
    weekday is inserted between real sessions, so holiday gaps stay intact.
    """
    dates = sorted(set(session_dates))
    if not dates:
        return []
    last = date.fromisoformat(dates[-1])
    extra_until = last + timedelta(days=EARNINGS_CLEAR_DAYS + 120)
    cursor = last + timedelta(days=1)
    extra = []
    while cursor <= extra_until:
        if cursor.weekday() < 5:
            extra.append(cursor.isoformat())
        cursor += timedelta(days=1)
    rows = dates + extra
    return [{"date": day, "open": "09:30", "close": "16:00"} for day in rows]


def research_fields(today, calendar):
    """Synthetic research inputs.

    Historical verified research cards and earnings dates are not in the
    cache. Passing UNKNOWN to the live helpers blocks every entry, which
    would measure the missing file rather than the price rules. These fields
    are outside the blackout: earnings_blocked returns false, and
    screen_contracts does not reject for earnings. The report labels this.
    Dividend VERIFIED_NONE is the live value that does not block a call.
    """
    earliest = (date.fromisoformat(today) + timedelta(days=EARNINGS_CLEAR_DAYS)).isoformat()
    release = session_offset(calendar, today, -10) or "1900-01-01"
    return {
        "earnings_earliest": earliest,
        "earnings_latest": earliest,
        "latest_release_date": release,
        "dividend": "VERIFIED_NONE",
        "distress_flag": False,
        "wheel_ok": True,
    }


def strike_step(spot):
    """Approximate listed-strike increment. Not the historical chain."""
    if spot < 25:
        return 0.5
    if spot < 100:
        return 1.0
    if spot < 200:
        return 2.5
    if spot < 500:
        return 5.0
    return 10.0


def _grid(low, high, step, limit=60):
    if step <= 0 or high < low:
        return []
    start = int(math.ceil((low - 1e-6) / step))
    end = int(math.floor((high + 1e-6) / step))
    out = []
    for i in range(start, end + 1):
        out.append(round(i * step, 2))
        if len(out) >= limit:
            break
    return out


def occ_symbol(root, expiry, kind, strike):
    """OCC symbol. screen_contracts does not parse it; the state machine can."""
    ymd = expiry[2:4] + expiry[5:7] + expiry[8:10]
    cp = "C" if kind == "call" else "P"
    return f"{root}{ymd}{cp}{int(round(strike * 1000)):08d}"


def _dicts(bars):
    return [{"o": b.o, "h": b.h, "l": b.l, "c": b.c, "t": b.date + "T21:00:00Z"} for b in bars]


class Simulator:
    """One capital, one cost assumption, one date window."""

    def __init__(self, histories, calendar, sessions, cfg, *, capital, books,
                 slippage_bps, iv_haircut, earnings_provider=None):
        self.hist = histories
        self.calendar = calendar
        self.sessions = list(sessions)
        self.real_sessions = set(sessions) if not histories else set(
            b.date for h in histories.values() for b in h.bars)
        # Trading sessions are the ones the caller named. `real_sessions` is
        # every loaded bar date so an expiry inside the sample can be told
        # from a holiday. The caller passes the market calendar's dates as
        # `sessions` for the window only; keep the full set separately.
        self.cfg = cfg
        self.policy = cfg.policy
        self.stock_p = cfg.policy["stock"]
        self.wheel_p = cfg.policy["wheel"]
        self.capital = float(capital)
        self.stock_capital, self.wheel_capital = sleeve_capitals(cfg.policy, self.capital)
        self.books = frozenset(books)
        self.slip = float(slippage_bps) / 10000.0
        self.slippage_bps = float(slippage_bps)
        self.iv_haircut = float(iv_haircut)
        self.earnings_provider = earnings_provider
        self.stock_comm = float(cfg.policy["costs"]["overlay_per_stock_transaction"])
        self.option_comm = float(cfg.policy["costs"]["overlay_per_option_contract_per_side"])
        # Both sleeves exist even when one book is idle, matching the policy
        # split. An idle sleeve is cash. It earns nothing.
        self.stock_cash = self.stock_capital
        self.wheel_cash = self.wheel_capital
        self.stock_positions = []
        self.cycles = []
        self.trades = []
        self.curve = []
        self.risk = RiskBook(cfg.policy, self.capital)
        self.last_equity = self.capital
        self.wheel_blocked = set()
        self.bought_today = set()
        self.exited_today = set()
        self.screen_cache = {}
        self.skip_counts = {}
        self.notional_obs = {s: [] for s in cfg.wheel_symbols}
        self.monthly_min = {s: {} for s in cfg.wheel_symbols}
        self.option_sides = 0
        self.stock_txns = 0
        self.put_assignments = 0
        self.call_assignments = 0
        self.spy_blocked = 0
        self.block_sessions = 0
        self.today = None
        self._stock_n = 0
        self._wheel_n = 0

    def run(self):
        if not self.sessions:
            raise ValueError("no trading sessions in the requested window")
        for today in self.sessions:
            self.today = today
            self.bought_today = set()
            self.exited_today = set()
            self.screen_cache = {}
            sess = Session({"date": today, "open": "09:30", "close": "16:00"})
            stock_now = sess.open + timedelta(minutes=45)
            wheel_now = sess.open + timedelta(minutes=120)
            stock_window = sess.in_window(
                stock_now, self.stock_p["entry_window_start_min"], self.stock_p["entry_window_end_min"])
            wheel_window = sess.in_window(
                wheel_now, self.wheel_p["entry_start_min_after_open"],
                stop_before_close_min=self.wheel_p["entry_stop_min_before_close"])

            open_equity, _, _ = self._mark("open")
            blocks = self.risk.blocks(open_equity, today)
            if blocks:
                self.block_sessions += 1
                if "DAILY_LOSS_2PCT" in blocks:
                    self.risk.daily_blocks += 1

            if stock_window:
                for pos in list(self.stock_positions):
                    self._strategy_exit(pos, today)
            for pos in list(self.stock_positions):
                self._stop_or_target(pos, today)

            marked, _, _ = self._mark("open")
            self.last_equity = marked
            base = min(self.capital, self.last_equity)

            if "wheel" in self.books:
                self._survey(today, wheel_now)

            if not blocks and stock_window and "stock" in self.books:
                self._stock_entries(today, stock_now, base)
                for pos in list(self.stock_positions):
                    if pos["entry_date"] == today:
                        self._stop_or_target(pos, today)

            if "wheel" in self.books:
                for cyc in list(self._active()):
                    self._manage_cycle(cyc, today, wheel_now)
                if not blocks and wheel_window:
                    self._wheel_entries(today, wheel_now, base)
                for cyc in list(self._active()):
                    self._settle_close(cyc, today)

            total, stock_eq, wheel_eq = self._mark("close")
            self.risk.set_prior(total)
            self.last_equity = total
            self.curve.append({
                "date": today,
                "equity": total,
                "stock_equity": stock_eq,
                "wheel_equity": wheel_eq,
            })
        return self._result()

    # ----------------------------------------------------------------- data
    def _window(self, symbol, today):
        hist = self.hist.get(symbol)
        if hist is None:
            return []
        start = (date.fromisoformat(today) - timedelta(days=BAR_LOOKBACK_DAYS)).isoformat()
        bars = hist.slice(start, today)
        return stock.completed_bars(_dicts(bars), today)

    def _spot(self, symbol, today, field):
        hist = self.hist.get(symbol)
        if hist is None:
            return None
        bar = hist.on(today)
        if bar is None:
            return None
        return bar.o if field == "o" else bar.c

    def _research(self, symbol, today):
        if self.earnings_provider is not None:
            return self.earnings_provider(symbol, today, self.calendar)
        return research_fields(today, self.calendar)

    def _count(self, symbol, reason):
        bucket = self.skip_counts.setdefault(symbol, {})
        bucket[reason] = bucket.get(reason, 0) + 1

    def _active(self):
        return [c for c in self.cycles if c["state"] != "CLOSED"]

    def _held_stock(self):
        return {p["symbol"] for p in self.stock_positions}

    # ----------------------------------------------------------------- cash
    def _cash(self, book, amount, reason, cyc=None):
        amount = round(float(amount), 2)
        if book == "STOCK":
            self.stock_cash = round(self.stock_cash + amount, 2)
        else:
            self.wheel_cash = round(self.wheel_cash + amount, 2)
        if cyc is not None:
            cyc["cashflows"].append({"amount": amount, "reason": reason})
        return amount

    def _charge_stock(self, book, cyc=None):
        self.stock_txns += 1
        self._cash(book, -self.stock_comm, "stock_commission", cyc)

    def _charge_option(self, cyc, reason):
        self.option_sides += 1
        self._cash("WHEEL", -self.option_comm, "option_commission_" + reason, cyc)

    # --------------------------------------------------------------- equity
    def _option_ask(self, cyc, spot, today, inclusive):
        if cyc.get("kind") not in ("put", "call") or not cyc.get("expiry"):
            return None
        hist = self.hist.get(cyc["symbol"])
        closes = []
        if hist is not None:
            end = today
            if inclusive:
                # Include today's close by slicing to the next date.
                nxt = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
                start = (date.fromisoformat(today) - timedelta(days=BAR_LOOKBACK_DAYS)).isoformat()
                closes = [b.c for b in hist.slice(start, nxt)]
            else:
                closes = [b["c"] for b in self._window(cyc["symbol"], today)]
        vol = realized_vol(closes) or cyc.get("sigma")
        if vol is None or spot is None or spot <= 0:
            return cyc.get("last_ask")
        intraday = not inclusive
        theo, _delta = bs_price(cyc["kind"], spot, cyc["strike"], year_fraction(today, cyc["expiry"], intraday), vol)
        _bid, ask = quoted(theo, self.iv_haircut)
        if ask <= 0:
            ask = cyc.get("last_ask") or 0.0
        cyc["last_ask"] = ask
        return ask

    def _mark(self, when):
        field = "o" if when == "open" else "c"
        inclusive = when == "close"
        stock_rows, wheel_rows, options = [], [], []
        for pos in self.stock_positions:
            px = self._spot(pos["symbol"], self.today, field) or pos.get("last_px")
            if px is None:
                continue
            pos["last_px"] = px
            stock_rows.append({"qty": pos["qty"], "bid": px * (1.0 - STOCK_HALF_SPREAD)})
        for cyc in self._active():
            if cyc["shares"]:
                px = self._spot(cyc["symbol"], self.today, field) or cyc.get("last_px")
                if px is not None:
                    cyc["last_px"] = px
                    wheel_rows.append({"qty": cyc["shares"], "bid": px * (1.0 - STOCK_HALF_SPREAD)})
            if cyc["state"] in ("PUT_OPEN", "CALL_OPEN"):
                px = self._spot(cyc["symbol"], self.today, field) or cyc.get("last_px")
                ask = self._option_ask(cyc, px, self.today, inclusive)
                if ask is None:
                    ask = max(cyc.get("last_ask") or 0.0, cyc.get("open_premium") or 0.0)
                options.append({"qty": 1, "ask": ask})
        total = accounting.conservative_equity(
            self.stock_cash + self.wheel_cash, stock_rows + wheel_rows, options)
        stock_eq = accounting.conservative_equity(self.stock_cash, stock_rows, [])
        wheel_eq = accounting.conservative_equity(self.wheel_cash, wheel_rows, options)
        return total, stock_eq, wheel_eq

    # ---------------------------------------------------------------- stock
    def _close_stock(self, pos, today, fill, reason):
        fill = stock.round_px(fill)
        self._cash("STOCK", round(fill * pos["qty"], 2), "stock_sell")
        self._charge_stock("STOCK")
        pnl = round((fill - pos["entry"]) * pos["qty"] - pos["entry_commission"] - self.stock_comm, 2)
        for trade in self.trades:
            if trade["book"] == "STOCK" and trade["id"] == pos["id"]:
                trade["closed"] = today
                trade["exit_price"] = fill
                trade["reason"] = reason
                trade["pnl"] = pnl
                break
        self.exited_today.add(pos["symbol"])
        self.stock_positions.remove(pos)

    def _strategy_exit(self, pos, today):
        bars = self._window(pos["symbol"], today)
        if not bars:
            return False
        why = stock.exit_due(bars, pos["entry_date"], today, self.calendar, self.stock_p)
        if not why:
            return False
        px = self._spot(pos["symbol"], today, "o")
        if px is None:
            return False
        # A gap through the resting stop fills at the open, not at the stop.
        bid = px * (1.0 - STOCK_HALF_SPREAD)
        fill = bid * (1.0 - self.slip)
        reason = "STOP" if px <= pos["stop"] else why
        self._close_stock(pos, today, fill, reason)
        return True

    def _stop_or_target(self, pos, today):
        hist = self.hist.get(pos["symbol"])
        bar = hist.on(today) if hist is not None else None
        if bar is None:
            return False
        hit_stop = bar.l <= pos["stop"]
        hit_target = bar.h >= pos["target"]
        # Both in one daily bar: the stop is taken. The path is unknown.
        if hit_stop:
            if bar.o <= pos["stop"]:
                px = bar.o * (1.0 - STOCK_HALF_SPREAD)
            else:
                px = pos["stop"]
            self._close_stock(pos, today, px * (1.0 - self.slip), "STOP")
            return True
        if hit_target:
            # Sell limit. No favourable gap fill.
            self._close_stock(pos, today, pos["target"], "TARGET")
            return True
        return False

    def _stock_entries(self, today, now, base):
        p = self.stock_p
        spy = self._window("SPY", today)
        spy_ok, _spy = stock.market_filter(spy, p)
        if not spy_ok:
            self.spy_blocked += 1
            return
        held = self._held_stock()
        wheel_syms = {c["symbol"] for c in self._active()}
        cands = []
        for symbol in self.cfg.us_symbols:
            if symbol not in self.hist or symbol in self.exited_today:
                # A position closed earlier today is not re-opened. Live stores
                # stock_exit_session and skips that symbol with no extra event.
                continue
            rec = self._research(symbol, today)
            if stock.earnings_blocked(
                    self.calendar, today, rec.get("earnings_earliest"), rec.get("earnings_latest"), p):
                self._count(symbol, "earnings")
                continue
            sig, _why = stock.signal(self._window(symbol, today), p)
            if not sig:
                continue
            sig["symbol"] = symbol
            cands.append(sig)
        ranked = stock.rank(cands)
        for cand in ranked:
            if cand["symbol"] in wheel_syms:
                self._count(cand["symbol"], "CROSS_BOOK_OVERLAP:WHEEL")
        picks = [c for c in ranked if c["symbol"] not in held and c["symbol"] not in wheel_syms]
        room = p["max_new_entries_per_session"]
        slots = p["max_open_or_pending_names"] - len(held)
        picks = picks[:max(0, min(room, slots))]
        for cand in picks:
            self._open_stock(cand, today, now, base)

    def _open_stock(self, cand, today, now, base):
        symbol = cand["symbol"]
        px = self._spot(symbol, today, "o")
        if px is None:
            return
        _bid, ask, _mid, quote = stock_quote(px, now)
        ok, _why = stock.quote_ok(
            quote, now, self.stock_p["quote_max_age_sec"], self.stock_p["max_spread_pct_of_mid"])
        if not ok:
            self._count(symbol, "quote")
            return
        if abs(ask / cand["signal_close"] - 1) > self.stock_p["ask_band_pct"]:
            self._count(symbol, "ask_band")
            return
        fill = stock.round_px(ask * (1.0 + self.slip))
        if fill <= 0 or cand["R"] is None or cand["R"] <= 0:
            self._count(symbol, "zero_size")
            return
        qty = stock.size(base, fill, cand["R"], self.stock_cash, self.stock_p)
        while qty > 0 and qty * fill + self.stock_comm > self.stock_cash + 1e-9:
            qty -= 1
        if qty <= 0:
            self._count(symbol, "zero_size")
            return
        _body, stop, target = stock.bracket_body(symbol, qty, fill, cand["R"], self.stock_p)
        self._stock_n += 1
        self._cash("STOCK", -round(qty * fill, 2), "stock_buy")
        self._charge_stock("STOCK")
        self.stock_positions.append({
            "id": f"S{self._stock_n:04d}",
            "symbol": symbol,
            "qty": qty,
            "entry": fill,
            "entry_date": today,
            "entry_commission": self.stock_comm,
            "stop": stop,
            "target": target,
            "last_px": px,
        })
        self.bought_today.add(symbol)
        self.trades.append({
            "book": "STOCK",
            "id": f"S{self._stock_n:04d}",
            "symbol": symbol,
            "opened": today,
            "closed": None,
            "qty": qty,
            "entry_price": fill,
            "exit_price": None,
            "stop": stop,
            "target": target,
            "reason": None,
            "pnl": None,
            "estimated": False,
            "option_prices": None,
        })

    # ---------------------------------------------------------------- wheel
    def _expiries(self, today):
        start = date.fromisoformat(today)
        last = self.sessions[-1]
        out = []
        for n in range(self.wheel_p["dte_min"], self.wheel_p["dte_max"] + 1):
            day = start + timedelta(days=n)
            iso = day.isoformat()
            if iso > last or iso not in self.real_sessions:
                continue
            if day.weekday() == 4:
                out.append(iso)
            elif day.weekday() == 3 and (day + timedelta(days=1)).isoformat() not in self.real_sessions:
                out.append(iso)
        return out

    def _chain(self, kind, symbol, today, spot, vol, now, acq, sma50):
        step = strike_step(spot)
        if kind == "put":
            ceiling = min(self.wheel_p["put_ceiling_mid_pct"] * spot, sma50)
            strikes = _grid(max(step, spot * 0.50), min(ceiling, spot), step)
        else:
            floor = max(spot, acq or 0.0)
            strikes = _grid(floor + step, max(floor, spot) * 1.25, step, limit=40)
        contracts, snaps = [], {}
        bid_s, ask_s, _mid, quote = stock_quote(spot, now)
        del bid_s, ask_s
        stamp = quote["t"]
        oi = self.wheel_p["min_open_interest"]
        for expiry in self._expiries(today):
            t_years = year_fraction(today, expiry, intraday=True)
            for strike in strikes:
                theo, delta = bs_price(kind, spot, strike, t_years, vol)
                bid, ask = quoted(theo, self.iv_haircut)
                occ = occ_symbol(symbol, expiry, kind, strike)
                contracts.append({
                    "symbol": occ,
                    "type": kind,
                    "expiration_date": expiry,
                    "strike_price": strike,
                    "multiplier": "100",
                    "size": "100",
                    "style": "american",
                    "root_symbol": symbol,
                    "underlying_symbol": symbol,
                    "tradable": True,
                    "open_interest": oi,
                    "open_interest_date": today,
                    "deliverables": [{
                        "type": "equity",
                        "symbol": symbol,
                        "amount": 100,
                        "settlement_method": "CCC",
                    }],
                })
                snaps[occ] = {
                    "latestQuote": {"bp": bid, "ap": ask, "t": stamp},
                    "greeks": {"delta": delta},
                }
        return contracts, snaps

    def _evaluate(self, kind, symbol, today, now, acq=None):
        key = (today, symbol, kind, None if acq is None else round(acq, 4))
        if key in self.screen_cache:
            return self.screen_cache[key]
        px = self._spot(symbol, today, "o")
        bars = self._window(symbol, today)
        sma50 = sma([b["c"] for b in bars], self.stock_p["sma_fast"]) if bars else None
        if px is None:
            result = (None, "no_bar")
        elif kind == "put" and sma50 is None:
            result = (None, "no_sma")
        else:
            vol = realized_vol([b["c"] for b in bars])
            if vol is None:
                result = (None, "no_vol")
            else:
                bid, ask, mid, quote = stock_quote(px, now)
                # Live _sell_option calls quote_ok with max spread 1.0.
                ok, why = stock.quote_ok(quote, now, self.wheel_p["quote_max_age_sec"], 1.0)
                if not ok:
                    result = (None, why)
                else:
                    contracts, snaps = self._chain(kind, symbol, today, px, vol, now, acq, sma50)
                    rec = self._research(symbol, today)
                    best, _rej = wheel.screen_contracts(
                        kind, symbol, contracts, snaps, spot_mid=mid, spot_bid=bid, spot_ask=ask,
                        sma50=sma50, today=today, now=now, calendar=self.calendar,
                        earnings_earliest=rec.get("earnings_earliest"),
                        latest_release=rec.get("latest_release_date"), p=self.wheel_p,
                        acq_price=acq)
                    if best is None:
                        result = (None, "no_contract")
                    else:
                        best["sigma"] = vol
                        result = (best, None)
        self.screen_cache[key] = result
        return result

    def _survey(self, today, now):
        for symbol in self.cfg.wheel_symbols:
            if symbol not in self.hist:
                continue
            best, _why = self._evaluate("put", symbol, today, now)
            if not best:
                continue
            notional = best["strike"] * 100.0
            self.notional_obs[symbol].append(notional)
            month = today[:7]
            prev = self.monthly_min[symbol].get(month)
            if prev is None or notional < prev:
                self.monthly_min[symbol][month] = notional

    def _combined_used(self):
        allowance = self.wheel_p["put_reserve_cost_allowance_per_contract"]
        used = 0.0
        for cyc in self._active():
            if cyc["state"] in ("PUT_PENDING", "PUT_OPEN", "PUT_CLOSING"):
                used += cyc["strike"] * 100.0 + allowance
            elif cyc["state"] not in ("CASH", "ASSIGNMENT_PENDING"):
                used += 100.0 * (cyc.get("acquisition_price") or 0.0)
        return used

    def _wheel_free(self):
        reserved = 0.0
        for cyc in self._active():
            if cyc["state"] in ("PUT_PENDING", "PUT_OPEN", "PUT_CLOSING"):
                reserved += wheel.put_collateral(cyc["strike"], self.wheel_p)
        return self.wheel_cash - reserved

    def _move(self, cyc, event):
        cyc["state"] = wheel.transition(cyc["state"], event)

    def _finish(self, cyc, today):
        cyc["closed"] = today
        cyc["pnl"] = round(sum(flow["amount"] for flow in cyc["cashflows"]), 2)
        for trade in self.trades:
            if trade["id"] == cyc["id"] and trade["book"] == "WHEEL":
                trade["closed"] = today
                trade["pnl"] = cyc["pnl"]
                trade["reason"] = cyc["state"]
                trade["put_assignments"] = cyc["put_assignments"]
                trade["call_assignments"] = cyc["call_assignments"]
                trade["assignments"] = cyc["put_assignments"] + cyc["call_assignments"]
                return

    def _open_put(self, symbol, today, best, base):
        limit = wheel.tick_round_down(best["bid"])
        if limit <= 0:
            self._count(symbol, "zero_premium")
            return False
        notional = best["strike"] * 100.0
        need = wheel.put_collateral(best["strike"], self.wheel_p)
        if notional > self.wheel_p["max_pct_per_name"] * base:
            self._count(symbol, "notional_cap")
            return False
        if self._combined_used() + notional > self.wheel_p["max_pct_combined"] * base:
            self._count(symbol, "combined_cap")
            return False
        if self._wheel_free() + 1e-9 < need:
            self._count(symbol, "insufficient_cash")
            return False
        self._wheel_n += 1
        cyc = {
            "id": f"W{self._wheel_n:04d}",
            "symbol": symbol,
            "state": "CASH",
            "opened": today,
            "closed": None,
            "shares": 0,
            "kind": "put",
            "strike": best["strike"],
            "expiry": best["expiry"],
            "open_premium": limit,
            "acquisition_price": None,
            "option_symbol": best["symbol"],
            "sigma": best["sigma"],
            "last_ask": best["ask"],
            "last_px": None,
            "cashflows": [],
            "put_assignments": 0,
            "call_assignments": 0,
            "pnl": None,
        }
        self._move(cyc, "PUT_SUBMITTED")
        self._move(cyc, "PUT_FILLED")
        self._cash("WHEEL", round(limit * 100.0, 2), "sell_put", cyc)
        self._charge_option(cyc, "open")
        self.cycles.append(cyc)
        self.trades.append({
            "book": "WHEEL",
            "id": cyc["id"],
            "symbol": symbol,
            "opened": today,
            "closed": None,
            "qty": 1,
            "entry_price": limit,
            "exit_price": None,
            "strike": best["strike"],
            "expiry": best["expiry"],
            "option_bid": best["bid"],
            "limit": limit,
            "notional": notional,
            "reason": None,
            "pnl": None,
            "estimated": True,
            "option_prices": "ESTIMATED",
            "assignments": 0,
        })
        return True

    def _wheel_entries(self, today, now, base):
        for cyc in list(self._active()):
            if cyc["state"] == "SHARES_HELD":
                self._sell_call(cyc, today, now)
        if len(self._active()) >= self.wheel_p["max_active_cycles"]:
            return
        look = self.stock_p["rank_lookback"]

        def rank_key(symbol):
            closes = [b["c"] for b in self._window(symbol, today)]
            ret = pct_return(closes, look)
            return (-(ret if ret is not None else -1e9), symbol)

        ranked = sorted([s for s in self.cfg.wheel_symbols if s in self.hist], key=rank_key)
        active = {c["symbol"] for c in self._active()}
        held = self._held_stock()
        for symbol in ranked:
            if len(self._active()) >= self.wheel_p["max_active_cycles"]:
                break
            if symbol in active:
                continue
            if symbol in self.wheel_blocked:
                self._count(symbol, "wheel_blocked")
                continue
            if symbol in held or symbol in self.bought_today:
                self._count(symbol, "CROSS_BOOK_OVERLAP:STOCK")
                continue
            best, why = self._evaluate("put", symbol, today, now)
            if best is None:
                if why:
                    self._count(symbol, why)
                continue
            if self._open_put(symbol, today, best, base):
                break

    def _sell_call(self, cyc, today, now):
        symbol = cyc["symbol"]
        if symbol in self.wheel_blocked:
            self._count(symbol, "wheel_blocked")
            return False
        rec = self._research(symbol, today)
        dividend = rec.get("dividend")
        if dividend in ("UNKNOWN", "NOT_YET_DECLARED") or not rec.get("wheel_ok"):
            self._count(symbol, "call_blocked")
            return False
        if cyc["shares"] < 100:
            return False
        best, why = self._evaluate("call", symbol, today, now, acq=cyc.get("acquisition_price"))
        if best is None:
            if why:
                self._count(symbol, "call_" + why)
            return False
        limit = wheel.tick_round_down(best["bid"])
        if limit <= 0:
            self._count(symbol, "zero_premium")
            return False
        self._move(cyc, "CALL_SUBMITTED")
        self._move(cyc, "CALL_FILLED")
        cyc["kind"] = "call"
        cyc["strike"] = best["strike"]
        cyc["expiry"] = best["expiry"]
        cyc["open_premium"] = limit
        cyc["option_symbol"] = best["symbol"]
        cyc["sigma"] = best["sigma"]
        cyc["last_ask"] = best["ask"]
        self._cash("WHEEL", round(limit * 100.0, 2), "sell_call", cyc)
        self._charge_option(cyc, "open")
        return True

    def _buy_to_close(self, cyc, today, ask):
        limit = wheel.tick_round_up(float(ask))
        if limit <= 0:
            return False
        self._cash("WHEEL", -round(limit * 100.0, 2), "buy_to_close", cyc)
        self._charge_option(cyc, "close")
        self._move(cyc, "BUYBACK_SUBMITTED")
        self._move(cyc, "BUYBACK_FILLED")
        cyc["kind"] = None
        cyc["open_premium"] = None
        cyc["option_symbol"] = None
        if cyc["state"] == "CLOSED":
            self._finish(cyc, today)
        return True

    def _sell_shares(self, cyc, today, bid):
        fill = stock.round_px(bid * (1.0 - self.slip))
        if fill <= 0 or cyc["shares"] <= 0:
            return False
        self._move(cyc, "UNWIND_SUBMITTED")
        self._cash("WHEEL", round(fill * cyc["shares"], 2), "unwind_sell", cyc)
        self._charge_stock("WHEEL", cyc)
        cyc["shares"] = 0
        self._move(cyc, "SHARES_SOLD")
        self._finish(cyc, today)
        return True

    def _manage_cycle(self, cyc, today, now):
        rec = self._research(cyc["symbol"], today)
        px = self._spot(cyc["symbol"], today, "o")
        if px is None:
            return
        bid, ask, mid, _quote = stock_quote(px, now)
        opt_bid = opt_ask = None
        if cyc["state"] in ("PUT_OPEN", "CALL_OPEN"):
            opt_ask = self._option_ask(cyc, px, today, inclusive=False)
            # Bid is the haircut bid of the same model. Management uses the ask
            # for the 50% test; the bid is required by the dividend branch.
            if opt_ask:
                # Reverse the symmetric spread to keep bid and ask consistent.
                mid_opt = opt_ask / (1.0 + ENTRY_HALF_SPREAD)
                opt_bid = mid_opt * (1.0 - ENTRY_HALF_SPREAD)
        data = {
            "expiry": cyc.get("expiry") or "9999-12-31",
            "open_premium": cyc.get("open_premium") or 0.0,
            "strike": cyc.get("strike") or 0.0,
            "acquisition_price": cyc.get("acquisition_price"),
        }
        actions = wheel.management_actions(
            {"state": cyc["state"], "data": data}, now_session_date=today, calendar=self.calendar,
            stock_mid=mid, opt_bid=opt_bid, opt_ask=opt_ask,
            earnings_earliest=rec.get("earnings_earliest"), dividend=rec.get("dividend"),
            distress=rec.get("distress_flag", False), p=self.wheel_p,
            closing_session_preceding_exdiv=False)
        for action, _why in actions:
            if action == "BLOCK_ADDITIONS":
                self.wheel_blocked.add(cyc["symbol"])
            if action == "BUY_TO_CLOSE" and cyc["state"] in ("PUT_OPEN", "CALL_OPEN") and opt_ask:
                self._buy_to_close(cyc, today, opt_ask)
                break
            if action == "SELL_SHARES" and cyc["state"] == "SHARES_HELD":
                self._sell_shares(cyc, today, bid)
                break

    def _assign(self, cyc, today):
        self._charge_option(cyc, "assignment")
        if cyc["kind"] == "put":
            self._cash("WHEEL", -round(cyc["strike"] * 100.0, 2), "put_assignment", cyc)
            self._charge_stock("WHEEL", cyc)
            self._move(cyc, "ASSIGNMENT_CONFIRMED")
            cyc["shares"] = 100
            cyc["acquisition_price"] = cyc["strike"]
            cyc["put_assignments"] += 1
            self.put_assignments += 1
        else:
            self._cash("WHEEL", round(cyc["strike"] * 100.0, 2), "call_assignment", cyc)
            self._charge_stock("WHEEL", cyc)
            self._move(cyc, "ASSIGNMENT_CONFIRMED")
            cyc["shares"] = 0
            cyc["call_assignments"] += 1
            self.call_assignments += 1
        cyc["kind"] = None
        cyc["open_premium"] = None
        cyc["option_symbol"] = None
        if cyc["state"] == "CLOSED":
            self._finish(cyc, today)

    def _expire(self, cyc, today):
        self._charge_option(cyc, "expiry")
        self._move(cyc, "EXPIRED_CONFIRMED")
        cyc["kind"] = None
        cyc["open_premium"] = None
        cyc["option_symbol"] = None
        if cyc["state"] == "CLOSED":
            self._finish(cyc, today)

    def _settle_close(self, cyc, today):
        if cyc["state"] not in ("PUT_OPEN", "CALL_OPEN"):
            return
        spot = self._spot(cyc["symbol"], today, "c")
        if spot is None:
            return
        if today >= cyc["expiry"]:
            itm = spot < cyc["strike"] if cyc["kind"] == "put" else spot > cyc["strike"]
            if itm:
                self._assign(cyc, today)
            else:
                self._expire(cyc, today)
            return
        hist = self.hist.get(cyc["symbol"])
        nxt = (date.fromisoformat(today) + timedelta(days=1)).isoformat()
        start = (date.fromisoformat(today) - timedelta(days=BAR_LOOKBACK_DAYS)).isoformat()
        closes = [b.c for b in hist.slice(start, nxt)] if hist is not None else []
        vol = realized_vol(closes) or cyc.get("sigma")
        if vol is None:
            return
        theo, _delta = bs_price(
            cyc["kind"], spot, cyc["strike"], year_fraction(today, cyc["expiry"], False), vol)
        if early_exercise(cyc["kind"], theo, spot, cyc["strike"]):
            self._assign(cyc, today)

    # -------------------------------------------------------------- result
    def _open_records(self):
        rows = []
        for pos in self.stock_positions:
            px = pos.get("last_px")
            bid = px * (1.0 - STOCK_HALF_SPREAD) if px else pos["entry"]
            mtm = round((bid - pos["entry"]) * pos["qty"] - pos["entry_commission"], 2)
            rows.append({
                "book": "STOCK", "id": pos["id"], "symbol": pos["symbol"], "opened": pos["entry_date"],
                "qty": pos["qty"], "entry_price": pos["entry"], "mtm_pnl": mtm, "estimated": False,
            })
        for cyc in self._active():
            px = cyc.get("last_px")
            bid = (px or 0.0) * (1.0 - STOCK_HALF_SPREAD)
            ask = 0.0
            if cyc["state"] in ("PUT_OPEN", "CALL_OPEN"):
                ask = cyc.get("last_ask") or 0.0
            mtm = round(sum(f["amount"] for f in cyc["cashflows"]) + cyc["shares"] * bid - ask * 100.0, 2)
            rows.append({
                "book": "WHEEL", "id": cyc["id"], "symbol": cyc["symbol"], "state": cyc["state"],
                "opened": cyc["opened"], "shares": cyc["shares"], "mtm_pnl": mtm, "estimated": True,
                "option_prices": "ESTIMATED",
                "put_assignments": cyc["put_assignments"],
                "call_assignments": cyc["call_assignments"],
            })
        return rows

    def _notional_summary(self):
        out = {}
        for symbol, values in self.notional_obs.items():
            if not values:
                out[symbol] = {"days": 0, "min": None, "median": None, "max": None}
                continue
            ordered = sorted(values)
            mid = ordered[len(ordered) // 2]
            out[symbol] = {
                "days": len(ordered),
                "min": ordered[0],
                "median": mid,
                "max": ordered[-1],
            }
        return out

    def _result(self):
        trades = list(self.trades)
        costs = accounting.cost_overlay(
            self.option_sides, self.stock_txns, self.policy["costs"])
        return {
            "capital": self.capital,
            "stock_capital": round(self.stock_capital, 2),
            "wheel_capital": round(self.wheel_capital, 2),
            "slippage_bps": self.slippage_bps,
            "iv_haircut": self.iv_haircut,
            "books": sorted(self.books),
            "start": self.sessions[0],
            "end": self.sessions[-1],
            "sessions": len(self.sessions),
            "curve": self.curve,
            "trades": trades,
            "open_positions": self._open_records(),
            "assignments": {"put": self.put_assignments, "call": self.call_assignments},
            "skips": self.skip_counts,
            "notionals": self._notional_summary(),
            "monthly_min_notional": self.monthly_min,
            "experiment_block_date": self.risk.tripped_on,
            "daily_loss_sessions": self.risk.daily_blocks,
            "entry_block_sessions": self.block_sessions,
            "spy_blocked_sessions": self.spy_blocked,
            "costs": {
                "option_sides": self.option_sides,
                "stock_transactions": self.stock_txns,
                "total": costs,
            },
            "config_hash": self.cfg.config_hash,
            "strategy_version": self.cfg.strategy_version,
            "label": "ESTIMATED",
        }


def run_backtest(histories, cfg, *, start, end, capital, books=("stock", "wheel"),
                 slippage_bps=10.0, iv_haircut=0.10, earnings_provider=None,
                 sessions=None):
    """Run one path.

    `histories` maps a symbol to a History, and must include the bars the
    books need plus SPY when the stock book is on. Sessions outside
    [start, end] are indicator warmup only.
    """
    if capital <= 0:
        raise ValueError("capital must be positive")
    if iv_haircut < 0 or iv_haircut >= 1:
        raise ValueError("iv_haircut must be in [0, 1)")
    if slippage_bps < 0:
        raise ValueError("slippage_bps must be >= 0")
    books = tuple(books)
    if not books or set(books) - {"stock", "wheel"}:
        raise ValueError("books must be stock, wheel, or both")
    if sessions is None:
        if "SPY" in histories:
            sessions = histories["SPY"].dates
        else:
            dates = set()
            for hist in histories.values():
                dates.update(hist.dates)
            sessions = sorted(dates)
    window = [day for day in sessions if start <= day <= end]
    # Full bar-date set so expiry checks can see sessions inside the window
    # that the caller built from SPY, including dates before `start`.
    full_dates = []
    if "SPY" in histories:
        full_dates = histories["SPY"].dates
    else:
        full_dates = sorted({d for h in histories.values() for d in h.dates})
    calendar = build_calendar(full_dates)
    sim = Simulator(
        histories, calendar, window, cfg, capital=capital, books=books,
        slippage_bps=slippage_bps, iv_haircut=iv_haircut,
        earnings_provider=earnings_provider)
    # Expiry must fall on a real bar date from the loaded history, not only
    # on the trading window. Replace the set the constructor derived.
    sim.real_sessions = set(full_dates)
    return sim.run()


def split_sessions(sessions, fraction=IN_SAMPLE_FRACTION):
    """Chronological split. Returns (in_sample, out_of_sample) or None.

    Parameters are not refit. The split only separates two regimes.
    """
    if len(sessions) < 2:
        return None
    cut = int(len(sessions) * fraction)
    cut = min(max(cut, 1), len(sessions) - 1)
    return sessions[:cut], sessions[cut:]
