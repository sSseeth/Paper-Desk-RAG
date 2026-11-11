"""Alpaca REST clients (official v2 trading API on the paper host, market data v2 / v1beta1 options).
Field names checked against docs.alpaca.markets on 2026-10-30 (see README).

Safety by construction:
- PaperReader has only GET methods. PaperWriter (subclass) adds submit/cancel and is only constructed in
  AUTO_PAPER/flatten. DRY_RUN never builds a PaperWriter, so it cannot submit.
- Host is the module constant PAPER_TRADING_HOST; redirects are refused (allow_redirects=False, 3xx = error).
"""
import time
from urllib.parse import urlparse
import requests
from .config import PAPER_TRADING_HOST, DATA_HOST


class BrokerError(Exception):
    pass


class UncertainResponse(BrokerError):
    """Timeout / connection loss / 5xx after a write: outcome unknown, never treat as failure."""


class FeedNotEntitled(BrokerError):
    pass


class _Http:
    def __init__(self, host, key_id, secret, session=None, sleep=time.sleep, max_retries=5):
        if urlparse(host).scheme != "https":
            raise BrokerError("https only")
        self.host, self.sleep, self.max_retries = host, sleep, max_retries
        self.s = session or requests.Session()
        self.s.headers.update({"APCA-API-KEY-ID": key_id, "APCA-API-SECRET-KEY": secret})
        self.s.max_redirects = 0

    def _req(self, method, path, params=None, json=None, write=False):
        url = self.host + path
        backoff = 1.0
        for attempt in range(self.max_retries):
            try:
                r = self.s.request(method, url, params=params, json=json, timeout=15, allow_redirects=False)
            except (requests.Timeout, requests.ConnectionError) as e:
                if write:
                    raise UncertainResponse(f"{method} {path}: {type(e).__name__}")
                self.sleep(backoff); backoff *= 2; continue
            if 300 <= r.status_code < 400:
                raise BrokerError(f"redirect refused ({r.status_code}) from {path}")
            final = urlparse(getattr(r, "url", url) or url)
            if f"{final.scheme}://{final.netloc}" != self.host:
                raise BrokerError("response host mismatch")
            if r.status_code == 429:
                wait = r.headers.get("Retry-After")
                reset = r.headers.get("X-RateLimit-Reset")
                delay = float(wait) if wait else (max(0.0, float(reset) - time.time()) if reset else backoff)
                self.sleep(min(delay, 60)); backoff *= 2; continue
            if r.status_code >= 500:
                if write:
                    raise UncertainResponse(f"{method} {path}: HTTP {r.status_code}")
                self.sleep(backoff); backoff *= 2; continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403) and "subscription" in (r.text or "").lower():
                raise FeedNotEntitled(f"{path}: feed not entitled")
            if r.status_code >= 400:
                raise BrokerError(f"{method} {path}: HTTP {r.status_code} {r.text[:200]}")
            return r.json() if r.content else {}
        raise BrokerError(f"{method} {path}: retries exhausted (rate limit/outage)")


class PaperReader:
    def __init__(self, key_id, secret, session=None, sleep=time.sleep):
        self.http = _Http(PAPER_TRADING_HOST, key_id, secret, session, sleep)

    def account(self):
        return self.http._req("GET", "/v2/account")

    def positions(self):
        return self.http._req("GET", "/v2/positions") or []

    def clock(self):
        return self.http._req("GET", "/v2/clock")

    def calendar(self, start, end):
        return self.http._req("GET", "/v2/calendar", {"start": start, "end": end}) or []

    def asset(self, symbol):
        return self.http._req("GET", f"/v2/assets/{symbol}")

    def order(self, order_id):
        return self.http._req("GET", f"/v2/orders/{order_id}", {"nested": "true"})

    def order_by_client_id(self, cid):
        return self.http._req("GET", "/v2/orders:by_client_order_id", {"client_order_id": cid})

    def orders(self, status="open"):
        """Paginate backwards by submitted time using `until` (max 500 per page)."""
        out, until = [], None
        while True:
            p = {"status": status, "limit": 500, "nested": "true", "direction": "desc"}
            if until:
                p["until"] = until
            page = self.http._req("GET", "/v2/orders", p) or []
            new = [o for o in page if o["id"] not in {x["id"] for x in out}]
            out += new
            if len(page) < 500 or not new:
                return out
            until = page[-1]["submitted_at"]

    def option_contracts(self, underlying, exp_gte, exp_lte, typ):
        out, tok = [], None
        while True:
            p = {"underlying_symbols": underlying, "expiration_date_gte": exp_gte, "expiration_date_lte": exp_lte,
                 "type": typ, "status": "active", "limit": 1000}
            if tok:
                p["page_token"] = tok
            r = self.http._req("GET", "/v2/options/contracts", p) or {}
            out += r.get("option_contracts", [])
            tok = r.get("next_page_token") or r.get("page_token")
            if not tok:
                return out

    def activities(self, types=("OPASN", "OPEXP", "OPXRC", "OPTRD", "FILL"), after=None):
        out, tok = [], None
        while True:
            p = {"activity_types": ",".join(types), "direction": "asc", "page_size": 100}
            if after:
                p["after"] = after
            if tok:
                p["page_token"] = tok
            page = self.http._req("GET", "/v2/account/activities", p) or []
            out += page
            if len(page) < 100:
                return out
            tok = page[-1]["id"]


class PaperWriter(PaperReader):
    def submit(self, body):
        if "client_order_id" not in body:
            raise BrokerError("client_order_id required")
        return self.http._req("POST", "/v2/orders", json=body, write=True)

    def cancel(self, order_id):
        return self.http._req("DELETE", f"/v2/orders/{order_id}", write=True)


class MarketData:
    """Feed is fixed by the explicit profile. A missing entitlement raises; no fallback to another feed."""
    def __init__(self, key_id, secret, stock_feed, option_feed, session=None, sleep=time.sleep):
        self.http = _Http(DATA_HOST, key_id, secret, session, sleep)
        self.stock_feed, self.option_feed = stock_feed, option_feed

    def daily_bars(self, symbols, start):
        out, tok = {}, None
        while True:
            p = {"symbols": ",".join(symbols), "timeframe": "1Day", "adjustment": "split", "feed": self.stock_feed,
                 "start": start, "limit": 10000, "sort": "asc"}
            if tok:
                p["page_token"] = tok
            r = self.http._req("GET", "/v2/stocks/bars", p) or {}
            for s, bars in (r.get("bars") or {}).items():
                out.setdefault(s, []).extend(bars)
            tok = r.get("next_page_token")
            if not tok:
                return out

    def latest_quotes(self, symbols):
        r = self.http._req("GET", "/v2/stocks/quotes/latest", {"symbols": ",".join(symbols), "feed": self.stock_feed})
        return (r or {}).get("quotes", {})

    def option_snapshots(self, symbols):
        out = {}
        for i in range(0, len(symbols), 100):
            tok = None
            while True:
                p = {"symbols": ",".join(symbols[i:i + 100]), "feed": self.option_feed}
                if tok:
                    p["page_token"] = tok
                r = self.http._req("GET", "/v1beta1/options/snapshots", p) or {}
                out.update(r.get("snapshots") or {})
                tok = r.get("next_page_token")
                if not tok:
                    break
        return out
