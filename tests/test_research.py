"""Schema hash gate (signed v1.5), v1.5 file/record validation (version, wrapper, stale/UNKNOWN/RUMOUR, wheel card
created-at clock, both Wheel statuses), polling interval + ETag 304."""
import base64, copy, json, os
from datetime import datetime, timedelta, timezone
import pytest
from conftest import make_root
from sakibdesk import research as R
from sakibdesk.config import Config

NOW = datetime(2026, 10, 28, 14, 15, tzinfo=timezone.utc)


def fact(v, kind="FILING"):
    return {"value": v, "source_url": "https://sec.gov/x", "source_type": "SEC_FILING", "confidence": "HIGH",
            "evidence_kind": kind, "notes": "", "published_or_as_of_utc": "2026-10-01T00:00:00Z",
            "retrieved_at_utc": "2026-10-28T12:00:00Z"}


def record(**over):
    r = {"schema_version": "news-research-v1.5", "symbol": "MU", "exchange": "NASDAQ", "currency": "USD", "universe": "WHEEL",
         "next_earnings": fact({"earliest": "2026-12-17", "latest": "2026-12-17"}),
         "latest_release_date": fact("2026-09-23"), "risk_flags": [], "verification_status": "VERIFIED",
         "verification_reasons": [], "verified_by": "Skeptic", "checked_at_utc": "2026-10-28T12:00:00Z",
         "valid_until_utc": "2026-10-29T12:00:00Z", "dividend": "VERIFIED_NONE",
         "verified_at_utc": "2026-10-28T12:30:00Z",
         "latest_annual_operating_cash_flow": fact({"amount": 1.7e10, "currency": "USD", "fiscal_year_end": "2026-08-28"}),
         "ocf_filing_date": fact("2026-10-03"),
         "thesis_evidence": [], "paper_universe_eligible": fact(True), "going_concern_flag": fact("FALSE"),
         "bankruptcy_flag": fact("FALSE"), "ownership_card_status": "VERIFIED",
         "card_created_at_utc": "2026-10-28T12:00:00Z", "card_valid_until_utc": "2026-10-31T12:00:00Z"}
    r.update(over)
    return r


def wrap(*recs, **over):
    f = {"schema_version": "news-research-v1.5", "published_at_utc": "2026-10-28T12:40:00Z", "publisher": "News",
         "records": list(recs)}
    f.update(over)
    return f


def v(rec):
    return R.validate_record(rec, {"MU", "SPY"}, {"MU"}, NOW)


def test_valid_record():
    ok, errs, out = v(record())
    assert ok, errs
    assert out["wheel_ok"], out["wheel_reasons"]


@pytest.mark.parametrize("over,needle", [
    ({"valid_until_utc": "2026-10-28T13:00:00Z"}, "STALE"),
    ({"valid_until_utc": "2026-10-30T12:00:00Z"}, "VALIDITY_OVER_24H"),
    ({"verification_status": "PENDING"}, "NOT_VERIFIED:PENDING"),
    ({"verification_status": "INVALID"}, "NOT_VERIFIED:INVALID"),
    ({"verified_by": "News"}, "VERIFIER_NOT_SKEPTIC"),
    ({"verified_at_utc": None}, "VERIFIED_AT_MISSING"),
    ({"verified_at_utc": "2026-10-28T11:00:00Z"}, "VERIFIED_BEFORE_CHECK"),
    ({"next_earnings": fact("UNKNOWN")}, "FACT_UNKNOWN:next_earnings"),
    ({"next_earnings": fact(None)}, "FACT_UNKNOWN:next_earnings"),
    ({"latest_release_date": "UNKNOWN"}, "FACT_UNKNOWN:latest_release_date"),
    ({"next_earnings": fact("2026-12-17", "RUMOUR")}, "RUMOUR_NOT_ELIGIBLE:next_earnings"),
    ({"next_earnings": dict(fact("2026-12-17"), source_type=None)}, "SCHEMA_INVALID:next_earnings.source_type"),
    ({"next_earnings": dict(fact("2026-12-17"), evidence_kind=None)}, "SCHEMA_INVALID:next_earnings.evidence_kind"),
    ({"universe": "CRYPTO"}, "SCHEMA_INVALID:universe"),
    ({"checked_at_utc": "2026-10-28T15:00:00Z", "verified_at_utc": "2026-10-28T15:00:00Z"}, "FUTURE_TIMESTAMP:checked_at_utc"),
    ({"test": True}, "TEST_FILE"),
    ({"checked_at_utc": "2026-10-28T12:00:00+10:00"}, "SCHEMA_INVALID:checked_at_utc"),
    ({"symbol": "BHP.AX"}, "SYMBOL_NOT_ALLOWED"),
])
def test_record_rejections(over, needle):
    ok, errs, out = v(record(**over))
    assert not ok and needle in errs and not out["wheel_ok"], errs


@pytest.mark.parametrize("over,needle", [
    ({"going_concern_flag": fact("UNKNOWN")}, "FACT_UNKNOWN:going_concern_flag"),
    ({"bankruptcy_flag": fact("TRUE")}, "DISTRESS_FLAG:bankruptcy_flag"),
    ({"paper_universe_eligible": fact(False)}, "NOT_PAPER_UNIVERSE_ELIGIBLE"),
    ({"card_valid_until_utc": "2026-10-31T12:00:01Z"}, "CARD_VALIDITY_OVER_72H"),
    ({"latest_annual_operating_cash_flow": fact({"amount": -5, "currency": "USD"})}, "OCF_NOT_POSITIVE"),
    ({"ocf_filing_date": "UNKNOWN"}, "FACT_UNKNOWN:ocf_filing_date"),
    ({"ocf_filing_date": fact("2026-10-03", "RUMOUR")}, "RUMOUR_NOT_ELIGIBLE:ocf_filing_date"),
    ({"card_created_at_utc": "2026-10-28T12:00:00+10:00"}, "SCHEMA_INVALID:card_created_at_utc"),
    ({"ownership_card_status": "DRAFT"}, "OWNERSHIP_NOT_VERIFIED:DRAFT"),
    ({"ownership_card_status": "SENT_TO_SKEPTIC"}, "OWNERSHIP_NOT_VERIFIED:SENT_TO_SKEPTIC"),
])
def test_wheel_card_rejections(over, needle):
    ok, errs, out = v(record(**over))
    assert ok, errs                       # Wheel card problems block Wheel use only, not the stock record
    assert not out["wheel_ok"] and needle in out["wheel_reasons"], out["wheel_reasons"]


def test_schema_hash_states(tmp_path):
    root = make_root(tmp_path, signed=False)
    cfg = Config(root)
    assert R.schema_status(root, cfg.policy)[1] == "SCHEMA_NOT_SIGNED"
    cfg.policy["research"]["signed_schema_sha256"] = "0" * 64
    assert R.schema_status(root, cfg.policy)[1] == "SCHEMA_HASH_MISMATCH"
    ok, st, actual = R.schema_status(root, cfg.policy)
    cfg.policy["research"]["signed_schema_sha256"] = actual
    assert R.schema_status(root, cfg.policy)[:2] == (True, "OK")
    assert actual == "08cf202d4a719f505711e8902e5bf747bfa6c1f9b3cd80e835db2dc798b0cc30"


def test_shipped_policy_pins_signed_v15():
    from conftest import ROOT
    p = json.load(open(os.path.join(ROOT, "policy.json")))
    assert p["research"]["schema_file"] == "schemas/news_schema_v1.5.json"
    assert p["research"]["signed_schema_sha256"] == "08cf202d4a719f505711e8902e5bf747bfa6c1f9b3cd80e835db2dc798b0cc30"
    assert R.schema_status(ROOT, p)[:2] == (True, "OK")
    sch = json.load(open(os.path.join(ROOT, p["research"]["schema_file"])))
    assert sch["schema_version"] == R.PINNED_SCHEMA_VERSION
    assert "ownership_card_status == VERIFIED" in sch["usable_rule"]
    for old in ("news_schema_v1.2.json", "news_schema_v1.3.json"):        # history kept
        assert os.path.exists(os.path.join(ROOT, "schemas", old))


def test_signed_hash_but_wrong_schema_version_blocks(tmp_path):
    root = make_root(tmp_path, signed=False)
    cfg = Config(root)
    cfg.policy["research"]["schema_file"] = "schemas/news_schema_v1.3.json"
    cfg.policy["research"]["signed_schema_sha256"] = R.schema_status(root, cfg.policy)[2]
    assert R.schema_status(root, cfg.policy)[1] == "SCHEMA_VERSION_MISMATCH"


# ---- v1.5-specific rules ----
def _load(journal, cfg, data, path="research/t.json"):
    journal.db.execute("INSERT INTO research_cache(path,etag,content,fetched_utc) VALUES(?,'',?,'') ON CONFLICT(path) "
                       "DO UPDATE SET content=excluded.content", (path, json.dumps(data)))
    return R.load_records(journal, cfg, NOW)


def test_valid_v15_file_passes(journal, cfg):
    recs = _load(journal, cfg, wrap(record()))
    assert recs["MU"][0] is True and recs["MU"][2]["wheel_ok"] is True


@pytest.mark.parametrize("over", [{"schema_version": "news-research-v1.4"}, {"schema_version": "news-research-v1.3"},
                                  {"publisher": "Scout"}, {"published_at_utc": "2026-10-28T12:40:00+10:00"},
                                  {"records": {"MU": 1}}])
def test_wrong_file_version_or_wrapper_rejected(journal, cfg, over):
    assert "MU" not in _load(journal, cfg, wrap(record(), **over))
    assert journal.events("RESEARCH_FILE_REJECTED")


def test_bare_record_or_list_rejected(journal, cfg):
    assert _load(journal, cfg, record(), "a") == {}
    assert _load(journal, cfg, [record()], "b") == {}


@pytest.mark.parametrize("ver", ["news-research-v1.4", None])
def test_wrong_record_version_rejected(ver):
    r = record(schema_version=ver)
    ok, errs, out = v(r)
    assert not ok and not out["wheel_ok"] and errs == ["VERSION_MISMATCH"], errs


def test_missing_record_version_rejected():
    r = record(); del r["schema_version"]
    ok, errs, _ = v(r)
    assert not ok and errs == ["SCHEMA_INVALID:schema_version"], errs


def test_pending_record_blocked_like_news_first_bridge_file(journal, cfg):
    recs = _load(journal, cfg, wrap(record(verification_status="PENDING", verified_at_utc=None)))
    ok, errs, out = recs["MU"]
    assert ok is False and out["wheel_ok"] is False and errs == ["NOT_VERIFIED:PENDING"], errs
    (ev,) = journal.events("RESEARCH_RECORD_BLOCKED")
    assert json.loads(ev["payload"])["codes"] == ["NOT_VERIFIED:PENDING"] and ev["symbol"] == "MU"


def test_wheel_card_expired_status_blocked_even_if_verified():
    ok, errs, out = v(record(ownership_card_status="EXPIRED"))
    assert ok is True          # the research record itself is fine ...
    assert out["wheel_ok"] is False and out["wheel_reasons"] == ["OWNERSHIP_NOT_VERIFIED:EXPIRED"]   # ... card is not


def test_card_age_from_creation_not_refresh():
    # created 73h before NOW, refreshed/verified 2h ago with a fresh valid_until: still blocked
    r = record(card_created_at_utc="2026-10-25T13:15:00Z", card_valid_until_utc="2026-10-28T13:15:00Z")
    ok, _, out = v(r)
    assert ok is True and out["wheel_ok"] is False
    assert out["wheel_reasons"] == ["CARD_EXPIRED", "CARD_OLDER_THAN_72H"]


def test_card_older_than_72h_blocked_even_if_valid_until_claims_later():
    # valid_until 72h+ after creation is itself rejected; age from creation also fails
    r = record(card_created_at_utc="2026-10-25T14:00:00Z", card_valid_until_utc="2026-10-29T00:00:00Z")
    _, _, out = v(r)
    assert not out["wheel_ok"]
    assert out["wheel_reasons"] == ["CARD_VALIDITY_OVER_72H", "CARD_OLDER_THAN_72H"]


def test_card_valid_until_exactly_72h_ok():
    _, _, out = v(record(card_created_at_utc="2026-10-28T12:00:00Z", card_valid_until_utc="2026-10-31T12:00:00Z"))
    assert out["wheel_ok"], out["wheel_reasons"]


def test_missing_card_created_blocks():
    r = record(); del r["card_created_at_utc"]
    _, _, out = v(r)
    assert not out["wheel_ok"] and out["wheel_reasons"] == ["SCHEMA_INVALID:card_created_at_utc"]


def test_card_created_moved_by_refresh_blocks(journal, cfg):
    assert _load(journal, cfg, wrap(record()))["MU"][2]["wheel_ok"]
    moved = record(card_created_at_utc="2026-10-28T13:00:00Z", card_valid_until_utc="2026-10-31T13:00:00Z")
    out = _load(journal, cfg, wrap(moved))["MU"][2]
    assert not out["wheel_ok"] and out["wheel_reasons"] == ["CARD_CREATED_MOVED"]
    assert journal.events("RESEARCH_CARD_CREATED_MOVED")


class Resp:
    def __init__(self, code, js=None, headers=None):
        self.status_code, self._js, self.headers = code, js, headers or {}
    def json(self):
        return self._js


class FakeGH:
    def __init__(self, payload):
        self.calls, self.payload = [], payload
    def get(self, url, headers=None, **kw):
        assert kw.get("allow_redirects") is False and url.startswith("https://api.github.com/")
        self.calls.append((url, dict(headers)))
        if url.endswith("/contents/research"):
            return Resp(200, [{"name": "mu.json", "type": "file"}, {"name": "evil.py", "type": "file"},
                              {"name": "sub", "type": "dir"}])
        if headers.get("If-None-Match") == '"e1"':
            return Resp(304)
        return Resp(200, {"encoding": "base64", "size": 10, "sha": "s",
                          "content": base64.b64encode(self.payload.encode()).decode()}, {"ETag": '"e1"'})


def test_poll_interval_etag_and_no_execution(journal, clock, cfg):
    payload = json.dumps(dict(wrap(record()), __import__="os.system('x')"))
    gh = FakeGH(payload)
    b = R.ResearchBridge(journal, "o/r", "tok", session=gh)
    assert b.poll() == "OK"
    assert not any("evil.py" in u for u, _ in gh.calls)
    assert b.poll() == "SKIPPED_INTERVAL"
    clock.advance(minutes=15)
    n = len(gh.calls)
    assert b.poll() == "OK"
    assert gh.calls[-1][1]["If-None-Match"] == '"e1"' and len(gh.calls) == n + 2
    recs = R.load_records(journal, cfg, clock())
    assert recs["MU"][0] is True


def test_duplicate_records_block(journal, cfg):
    journal.db.execute("INSERT INTO research_cache(path,etag,content,fetched_utc) VALUES('a','',?,'')",
                       (json.dumps(wrap(record(), record())),))
    assert R.load_records(journal, cfg, NOW)["MU"][0] is False


def test_v15_closed_the_page18_gaps():
    assert not hasattr(R, "PAGE18_GAPS")
    assert "card_created_at_utc" in R.WHEEL_REQ and "ocf_filing_date" in R.WHEEL_REQ and "schema_version" in R.REC_REQ


# ---- News's real bridge test file (copied verbatim: tests/fixtures/news_bridge_test_v15.json) ----
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "news_bridge_test_v15.json")
FACTS_REC = ("next_earnings", "latest_release_date", "dividend")
FACTS_CARD = ("latest_annual_operating_cash_flow", "ocf_filing_date", "paper_universe_eligible",
              "going_concern_flag", "bankruptcy_flag")
NULL_KEYS = ("source_type", "evidence_kind", "source_url", "published_or_as_of_utc")


def _expected_bridge_codes():
    rec = ["TEST_FILE", "NOT_VERIFIED:PENDING", "SCHEMA_INVALID:verified_by"]
    for f in FACTS_REC:
        rec += [f"SCHEMA_INVALID:{f}.{k}" for k in NULL_KEYS] + ([f"FACT_UNKNOWN:{f}"] if f != "dividend" else [])
    card = []
    for f in FACTS_CARD:
        card += [f"SCHEMA_INVALID:{f}.{k}" for k in NULL_KEYS] + [f"FACT_UNKNOWN:{f}"]
    return rec, card + ["OWNERSHIP_NOT_VERIFIED:DRAFT"]


def test_news_bridge_test_file_exact_reasons(cfg):
    raw = json.load(open(FIXTURE))
    assert raw["test"] is True and raw["records"][0]["next_earnings"]["source_type"] is None
    now = datetime(2026, 11, 11, 17, 40, tzinfo=timezone.utc)        # 5 min after News published it
    rep = R.check_text(open(FIXTURE).read(), cfg, now)
    assert rep["file_status"] == "ACCEPTED" and rep["file_codes"] == [] and rep["test_file"] is True
    (r,) = rep["records"]
    rec, card = _expected_bridge_codes()
    assert r["symbol"] == "MU" and r["stock_use"] == "BLOCKED" and r["wheel_use"] == "BLOCKED"
    assert r["codes"] == rec
    assert r["wheel_codes"] == card
    assert r["notes"] == ["FACT_UNKNOWN:dividend (blocks covered calls only)"]


def test_news_bridge_test_file_after_expiry_adds_stale_and_card_expired(cfg):
    rep = R.check_text(open(FIXTURE).read(), cfg, datetime(2026, 11, 11, 18, 40, tzinfo=timezone.utc))
    (r,) = rep["records"]
    assert "STALE" in r["codes"] and "CARD_EXPIRED" in r["wheel_codes"]


def test_news_bridge_test_file_blocked_and_journalled_in_loader(journal, cfg):
    journal.db.execute("INSERT INTO research_cache(path,etag,content,fetched_utc) VALUES('research/bridge_test.json','',?,'')",
                       (open(FIXTURE).read(),))
    report = []
    now = datetime(2026, 11, 11, 17, 40, tzinfo=timezone.utc)
    recs = R.load_records(journal, cfg, now, report)
    ok, codes, parsed = recs["MU"]
    assert ok is False and parsed["wheel_ok"] is False and "TEST_FILE" in codes
    (ev,) = journal.events("RESEARCH_RECORD_BLOCKED")
    pl = json.loads(ev["payload"])
    assert pl["codes"] == _expected_bridge_codes()[0] and pl["wheel_codes"] == _expected_bridge_codes()[1]
    assert report[0]["symbol"] == "MU" and report[0]["stock_use"] == "BLOCKED"
    R.load_records(journal, cfg, now)                                   # same content: journalled once
    assert len(journal.events("RESEARCH_RECORD_BLOCKED")) == 1


def test_test_flag_blocks_an_otherwise_valid_record(journal, cfg):
    recs = _load(journal, cfg, wrap(record(), test=True))
    assert recs["MU"][0] is False and recs["MU"][1] == ["TEST_FILE"] and not recs["MU"][2]["wheel_ok"]


def test_bad_record_does_not_reject_other_records_in_file(journal, cfg):
    bad = record(symbol="AMD", next_earnings=fact("UNKNOWN"))
    recs = _load(journal, cfg, wrap(record(), bad))
    assert recs["MU"][0] is True and recs["AMD"][0] is False and recs["AMD"][1] == ["FACT_UNKNOWN:next_earnings"]
    assert not journal.events("RESEARCH_FILE_REJECTED")


def test_file_level_codes(journal, cfg):
    assert R.validate_file(wrap(record(), schema_version="news-research-v1.4"))[1] == ["VERSION_MISMATCH"]
    assert R.validate_file(wrap(record(), publisher="Scout"))[1] == ["SCHEMA_INVALID:file.publisher"]
    f = wrap(record()); del f["published_at_utc"]
    assert R.validate_file(f)[1] == ["SCHEMA_INVALID:file.published_at_utc"]
    assert R.check_text("{not json", cfg, NOW)["file_codes"] == ["INVALID_JSON"]
    _load(journal, cfg, wrap(record(), schema_version="news-research-v1.3"))
    (ev,) = journal.events("RESEARCH_FILE_REJECTED")
    assert json.loads(ev["payload"])["codes"] == ["VERSION_MISMATCH"]
