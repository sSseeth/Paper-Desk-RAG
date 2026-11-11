"""Offline fixtures: temp project copy, controllable clock, synthetic calendar, mocked broker/data. No network."""
import json, os, shutil, sys
from datetime import date, datetime, timedelta, timezone
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from sakibdesk.config import Config, sha256_file   # noqa: E402
from sakibdesk.journal import Journal              # noqa: E402
from sakibdesk.alpaca import UncertainResponse     # noqa: E402


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    import socket
    def guard(*a, **k):
        raise RuntimeError("network disabled in tests")
    monkeypatch.setattr(socket, "create_connection", guard)
    monkeypatch.setattr(socket.socket, "connect", guard)


class Clock:
    def __init__(self, t):
        self.t = t
    def __call__(self):
        return self.t
    def advance(self, **kw):
        self.t += timedelta(**kw)


def make_root(tmp_path, signed=True, **policy_over):
    for f in ("policy.json", "watchlist.json"):
        shutil.copy(os.path.join(ROOT, f), tmp_path / f)
    shutil.copytree(os.path.join(ROOT, "schemas"), tmp_path / "schemas")
    p = json.load(open(tmp_path / "policy.json"))
    # signed=False simulates an unsigned schema (shipped policy now pins signed v1.5)
    p["research"]["signed_schema_sha256"] = (sha256_file(str(tmp_path / p["research"]["schema_file"]))
                                             if signed else "")
    for k, v in policy_over.items():
        sec, key = k.split("__")
        p[sec][key] = v
    json.dump(p, open(tmp_path / "policy.json", "w"), indent=1)
    return str(tmp_path)


def make_calendar(start="2025-06-01", end="2027-03-01", early=None, holidays=()):
    out, d = [], date.fromisoformat(start)
    while d.isoformat() <= end:
        if d.weekday() < 5 and d.isoformat() not in holidays:
            close = "13:00" if early and d.isoformat() in early else "16:00"
            out.append({"date": d.isoformat(), "open": "09:30", "close": close})
        d += timedelta(days=1)
    return out


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


@pytest.fixture
def cfg(root):
    return Config(root)


@pytest.fixture
def clock():
    return Clock(datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc))   # 10:15 New York (EDT)


@pytest.fixture
def journal(tmp_path, cfg, clock):
    return Journal(str(tmp_path / "j.sqlite3"), cfg, clock=clock)


def ts(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class FakeBroker:
    """Mock of PaperReader + PaperWriter + MarketData. Records every write."""
    def __init__(self, clock):
        self.clock = clock
        self.orders_by_cid, self.submits, self.cancels = {}, [], []
        self.positions_list, self.activities_list = [], []
        self.fail_next_submit_uncertain_after_accept = False
        self.fail_next_submit_uncertain_before_accept = False
        self.quotes, self.bars, self.snapshots, self.contracts = {}, {}, {}, []
        self.acct = {"id": "ACC-1", "status": "ACTIVE", "equity": "100000", "last_equity": "100000"}
        self.cal = make_calendar()
        self.n = 0

    # reader
    def account(self): return dict(self.acct)
    def positions(self): return list(self.positions_list)
    def clock_(self): return {"timestamp": ts(self.clock())}
    def calendar(self, s, e): return [d for d in self.cal if s <= d["date"] <= e]
    def asset(self, sym): return {"symbol": sym, "tradable": True}
    def order_by_client_id(self, cid): return self.orders_by_cid.get(cid)
    def order(self, oid):
        for o in self.orders_by_cid.values():
            if o["id"] == oid:
                return o
            for l in o.get("legs") or []:
                if l["id"] == oid:
                    return l
    def orders(self, status="open"):
        return [o for o in self.orders_by_cid.values() if o["status"] in ("new", "accepted", "partially_filled")]
    def option_contracts(self, u, a, b, t):
        return [c for c in self.contracts if c["underlying_symbol"] == u and c["type"] == t and a <= c["expiration_date"] <= b]
    def activities(self, *a, **k): return list(self.activities_list)

    # writer
    def submit(self, body):
        self.submits.append(body)
        if self.fail_next_submit_uncertain_before_accept:
            self.fail_next_submit_uncertain_before_accept = False
            raise UncertainResponse("timeout")
        self.n += 1
        o = {"id": f"OID-{self.n}", "client_order_id": body["client_order_id"], "status": "accepted",
             "filled_qty": "0", "filled_avg_price": None, "side": body["side"], "symbol": body["symbol"],
             "qty": body["qty"], "asset_class": "us_option" if len(body["symbol"]) > 10 else "us_equity",
             "legs": None}
        if body.get("order_class") == "bracket":
            o["legs"] = [{"id": f"OID-{self.n}-tp", "type": "limit", "qty": body["qty"], "status": "held", "filled_qty": "0"},
                         {"id": f"OID-{self.n}-sl", "type": "stop", "qty": body["qty"], "status": "held", "filled_qty": "0"}]
        if body["client_order_id"] in self.orders_by_cid:
            raise AssertionError("broker would reject duplicate client_order_id")
        self.orders_by_cid[body["client_order_id"]] = o
        if self.fail_next_submit_uncertain_after_accept:
            self.fail_next_submit_uncertain_after_accept = False
            raise UncertainResponse("HTTP 504 after accept")
        return dict(o)

    def cancel(self, oid):
        self.cancels.append(oid)
        o = self.order(oid)
        if o and o["status"] not in ("filled",):
            o["status"] = "canceled"

    # market data
    def daily_bars(self, syms, start): return {s: self.bars.get(s, []) for s in syms}
    def latest_quotes(self, syms): return {s: self.quotes[s] for s in syms if s in self.quotes}
    def option_snapshots(self, syms): return {s: self.snapshots[s] for s in syms if s in self.snapshots}

    def fill(self, cid, qty, px, status=None):
        o = self.orders_by_cid[cid]
        o["filled_qty"], o["filled_avg_price"] = str(qty), str(px)
        o["status"] = status or ("filled" if float(qty) >= float(o["qty"]) else "partially_filled")


class ReaderView:
    """GET-only view of FakeBroker (as PaperReader)."""
    def __init__(self, b):
        self._b = b
    def __getattr__(self, n):
        if n in ("submit", "cancel"):
            raise AttributeError(n)
        if n == "clock":
            return self._b.clock_
        return getattr(self._b, n)


@pytest.fixture
def broker(clock):
    return FakeBroker(clock)


def bars_trend(n=260, start=50.0, step=0.2, breakout=True, end_date="2026-10-27"):
    """Synthetic completed daily bars, rising trend, last bar a 20-session breakout."""
    cal = [d["date"] for d in make_calendar("2025-06-01", end_date)][-n:]
    out = []
    for i, d in enumerate(cal):
        c = start + step * i
        out.append({"t": f"{d}T04:00:00Z", "o": c - 0.1, "h": c + 0.3, "l": c - 0.4, "c": c, "v": 1000})
    if breakout:
        out[-1]["c"] = out[-2]["h"] + 1.0
        out[-1]["h"] = out[-1]["c"] + 0.1
    return out
