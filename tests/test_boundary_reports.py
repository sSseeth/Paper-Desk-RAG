"""Paper boundary, redirects, redaction, config rejection, webhook outbox dedup/retry, local reports, stamps."""
import json, logging
import pytest
from conftest import make_root
from sakibdesk import alpaca as A, reports as RP, config as CF
from sakibdesk.secrets_store import redact
from sakibdesk.config import Config


def test_hosts_hard_coded():
    assert A.PAPER_TRADING_HOST == "https://paper-api.alpaca.markets" and A.DATA_HOST == "https://data.alpaca.markets"
    import inspect
    assert "PaperWriter" in dir(A) and "LiveWriter" not in dir(A)
    sig = inspect.signature(A.PaperReader.__init__)
    assert "host" not in sig.parameters


def test_http_rejected():
    with pytest.raises(A.BrokerError):
        A._Http("http://paper-api.alpaca.markets", "k", "s")


class R:
    def __init__(self, code, url="https://paper-api.alpaca.markets/v2/account"):
        self.status_code, self.url, self.headers, self.text, self.content = code, url, {}, "", b"{}"
    def json(self):
        return {}


class S:
    def __init__(self, resp):
        self.resp, self.headers = resp, {}
    def request(self, *a, **k):
        assert k["allow_redirects"] is False
        return self.resp


def test_redirect_and_host_mismatch_refused():
    with pytest.raises(A.BrokerError, match="redirect"):
        A.PaperReader("k", "s", session=S(R(302))).account()
    with pytest.raises(A.BrokerError, match="host mismatch"):
        A.PaperReader("k", "s", session=S(R(200, "https://api.alpaca.markets/v2/account"))).account()


def test_write_5xx_is_uncertain_not_failure():
    with pytest.raises(A.UncertainResponse):
        A.PaperWriter("k", "s", session=S(R(503))).submit({"client_order_id": "x"})


def test_redaction():
    t = redact("Authorization: Bearer abc123def APCA-API-SECRET-KEY: zzzz token=ghp_ABCDEFGHIJKLMNOP")
    assert "abc123def" not in t and "zzzz" not in t and "ghp_ABCDEFGHIJKLMNOP" not in t


@pytest.mark.parametrize("over", [{"capital__initial": 50000.0}, {"feed_profile__x": 1}])
def test_config_rejections(tmp_path, over):
    root = make_root(tmp_path)
    p = json.load(open(f"{root}/policy.json"))
    if "capital__initial" in over:
        p["capital"]["initial"] = 50000.0
    else:
        p["feed_profile"] = "AUTO"
    json.dump(p, open(f"{root}/policy.json", "w"))
    with pytest.raises(Exception):
        Config(root)


def test_watchlist_rejects_non_us(tmp_path):
    root = make_root(tmp_path)
    wl = json.load(open(f"{root}/watchlist.json"))
    wl["names"].append({"symbol": "BHP", "company": "x", "theme": "x", "market": "ASX", "currency": "AUD",
                        "stock_eligible": True, "wheel_eligible": False, "alerts_only": False, "report_priority": False})
    json.dump(wl, open(f"{root}/watchlist.json", "w"))
    with pytest.raises(Exception):
        Config(root)


def test_stamp_has_profile_and_indicative(cfg):
    s = RP.stamp(cfg)
    assert s["feed_profile"] == "FREE_MECHANICS" and s["option_quotes"].startswith("INDICATIVE")
    assert s["config_hash"] == cfg.config_hash


class Hook:
    def __init__(self, codes):
        self.codes, self.posts = list(codes), []
    def post(self, url, data=None, headers=None, **kw):
        self.posts.append(headers)
        class X: pass
        x = X(); x.status_code = self.codes.pop(0); return x


def test_outbox_dedup_and_retry_independent(journal, clock):
    RP.enqueue(journal, "ev1", {"a": 1}); RP.enqueue(journal, "ev1", {"a": 2})
    assert journal.db.execute("SELECT COUNT(*) n FROM outbox").fetchone()["n"] == 1
    h = Hook([503, 200])
    RP.deliver(journal, "https://hook.example/x", "tkn", session=h)
    assert h.posts[0]["Authorization"] == "Bearer tkn" and h.posts[0]["Idempotency-Key"] == "ev1"
    RP.deliver(journal, "https://hook.example/x", "tkn", session=h)       # backoff not elapsed
    assert len(h.posts) == 1
    clock.advance(minutes=2)
    assert RP.deliver(journal, "https://hook.example/x", "tkn", session=h)["sent"] == 1
    assert journal.db.execute("SELECT COUNT(*) n FROM delivery_attempts").fetchone()["n"] == 2


def test_webhook_unconfigured_or_http_skipped(journal):
    RP.enqueue(journal, "e", {})
    assert RP.deliver(journal, "http://x", "t")["skipped"]
    assert RP.deliver(journal, "", "")["skipped"]


def test_local_reports_always_written(root, cfg, journal):
    paths = RP.write_local(root, cfg, journal, {"actions": [], "orders": []}, "2026-10-28")
    txt = "".join(open(p).read() for p in (paths if isinstance(paths, (list, tuple)) else paths.values()))
    assert "FREE_MECHANICS" in txt and "INDICATIVE" in txt


class Rec:
    """Scripted HTTP session. The response URL is the request URL so the host check passes."""

    def __init__(self, fn):
        self.fn, self.headers, self.calls = fn, {}, []

    def request(self, method, url, params=None, json=None, timeout=None, allow_redirects=True):
        self.calls.append((method, url, params))
        code, payload = self.fn(url, params or {})
        return JsonResp(code, payload, url)


class JsonResp:
    def __init__(self, code, payload, url):
        self.status_code, self.url, self.headers = code, url, {}
        self.content = json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return json.loads(self.content)


def test_order_lookup_requests_nested_legs():
    seen = {}

    def fn(url, params):
        seen["url"] = url
        seen["params"] = params
        return 200, {"id": "1", "status": "filled", "legs": []}

    A.PaperReader("k", "s", session=Rec(fn)).order_by_client_id("SPD-1")
    assert seen["url"].endswith("/v2/orders:by_client_order_id")
    assert seen["params"]["client_order_id"] == "SPD-1"
    assert seen["params"]["nested"] == "true"


def test_option_contracts_page_and_send_strike_type_and_status():
    pages = {
        None: {"option_contracts": [{"symbol": "HI", "strike_price": "30"}], "next_page_token": "N2"},
        "N2": {"option_contracts": [{"symbol": "LO", "strike_price": "8"}], "next_page_token": None},
    }

    def fn(url, params):
        assert url.endswith("/v2/options/contracts")
        assert params["underlying_symbols"] == "F"
        assert params["type"] == "put" and params["status"] == "active"
        assert params["expiration_date_gte"] == "2026-11-17"
        assert params["expiration_date_lte"] == "2026-12-07"
        assert params["limit"] == 1000
        assert params["strike_price_lte"] == 9.9
        assert "page_token" not in params or params["page_token"] == "N2"
        return 200, pages[params.get("page_token")]

    out = A.PaperReader("k", "s", session=Rec(fn)).option_contracts(
        "F", "2026-11-17", "2026-12-07", "put", strike_lte=9.9)
    assert [row["symbol"] for row in out] == ["HI", "LO"]


def test_option_snapshots_batch_paginate_and_pass_indicative_feed():
    symbols = [f"S{i:03d}" for i in range(150)]

    def fn(url, params):
        assert url.endswith("/v1beta1/options/snapshots")
        assert params["feed"] == "indicative"
        assert params["limit"] == 1000
        batch = params["symbols"].split(",")
        assert len(batch) <= 100
        tok = params.get("page_token")
        if tok is None and len(batch) == 100:
            return 200, {"snapshots": {batch[0]: {"latestQuote": {"bp": 0.1}}}, "next_page_token": "P2"}
        if tok == "P2":
            assert len(batch) == 100
            return 200, {"snapshots": {sym: {"latestQuote": {"bp": 0.2}} for sym in batch},
                         "next_page_token": None}
        assert len(batch) == 50 and tok is None
        return 200, {"snapshots": {sym: {"latestQuote": {"bp": 0.3}} for sym in batch},
                     "next_page_token": None}

    out = A.MarketData("k", "s", "iex", "indicative", session=Rec(fn)).option_snapshots(symbols)
    assert len(out) == 150
    assert out["S000"]["latestQuote"]["bp"] == 0.2


def test_account_matches_id_or_number():
    from sakibdesk.cycle import account_matches
    a = {"id": "904837e3-3b76-47ec-b432-046db621571b", "account_number": "PA3ABCDEF123"}
    assert account_matches(a, "PA3ABCDEF123")
    assert account_matches(a, " pa3abcdef123 ")
    assert account_matches(a, "904837e3-3b76-47ec-b432-046db621571b")
    assert not account_matches(a, "3ABCDEF123")
    assert not account_matches(a, "PA3OTHER")
    assert not account_matches(a, "")
    assert not account_matches(None, "PA3ABCDEF123")
    assert not account_matches({"id": "", "account_number": ""}, "")
