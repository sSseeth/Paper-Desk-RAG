"""Research bridge: private GitHub repo, research/*.json only, read through the authenticated contents API
with a read-only token and conditional requests (ETag / If-None-Match), polled at most every 15 minutes.
Files are parsed as JSON data only; nothing fetched is ever executed or imported.

Runner research contract = News schema v1.7 (schemas/news_schema_v1.7.json, Skeptic-signed sha256
a3b4c2264350807286308af60613d106e021eedcb6e0d6b51acd3a4c9e31c2d0).
The schema file hash must equal policy.research.signed_schema_sha256 (Skeptic sign-off); if it is empty or
different, ALL new entries are blocked with reason SCHEMA_NOT_SIGNED / SCHEMA_HASH_MISMATCH.

v1.7 usable_rule (quoted from the schema; unchanged from v1.6): "Runner may use a record for new exposure only when
verification_status == VERIFIED, verified_at_utc >= checked_at_utc, and now < valid_until_utc. For Wheel cards,
additionally ownership_card_status == VERIFIED and now < card_valid_until_utc. An evidence_kind RUMOUR fact can
never satisfy an eligibility fact; it may only add a risk flag. Any UNKNOWN eligibility fact fails the gate."

v1.7 file format: {"schema_version", "published_at_utc", "publisher": "News", "records": [...]}; every record
carries its own schema_version, which must equal the file's and the pinned version. Anything else is rejected.
Fields not in the schema are never invented: absent/UNKNOWN eligibility facts fail the gate.

v1.6 null rules, unchanged in v1.7: a common_fact's source_url, source_type, evidence_kind and
published_or_as_of_utc may be null only when its value is "UNKNOWN". verified_by may be null only while
verified_at_utc is null; otherwise it must be "Skeptic".

v1.7 additions: source_type INTERNAL_GUIDE and evidence_kind INTERNAL_POLICY may back only
paper_universe_eligible (internal_guide_rule). source_url must still be https:// for every source type,
including INTERNAL_GUIDE; there is no file:// exception. verified_by is required for any Skeptic verdict
(VERIFIED, INVALID, or UNKNOWN). dividend is a common_fact {amount, ex_date, payment_date} for the next
declared dividend, or NOT_YET_DECLARED, VERIFIED_NONE, or UNKNOWN. NOT_YET_DECLARED and VERIFIED_NONE may
be a bare string or a sourced common_fact; a valid wrapped VERIFIED_NONE is no dividend. A past ex_date
is SCHEMA_INVALID. NOT_YET_DECLARED is unresolved dividend risk: it blocks a covered-call entry
(DIVIDEND_NOT_YET_DECLARED) and is otherwise handled like UNKNOWN. Notes are never parsed for an
expected declaration date.
"""
import base64, json, os, time
from datetime import date, datetime, timedelta, timezone
import requests
from .config import sha256_file
from .journal import iso

GITHUB_API = "https://api.github.com"
PINNED_SCHEMA_VERSION = "news-research-v1.7"   # must equal schema_version inside the pinned schema file
PUBLISHER = "News"
CARD_MAX_HOURS = 72                             # guide p8; v1.5: card_valid_until_utc <= card_created_at_utc + 72h
FACT_REQ = ("value", "source_url", "source_type", "confidence", "evidence_kind", "published_or_as_of_utc",
            "retrieved_at_utc")
SOURCE_TYPES = {"SEC_FILING", "COMPANY_IR", "EVENT_FEED", "MARKET_DATA", "INTERNAL_GUIDE"}
NULLABLE_IF_UNKNOWN = ("source_url", "source_type", "evidence_kind", "published_or_as_of_utc")   # v1.6 null rule
# v1.7: these source/evidence values may back paper_universe_eligible only.
GUIDE_ONLY_SOURCE = "INTERNAL_GUIDE"
GUIDE_ONLY_EVIDENCE = "INTERNAL_POLICY"
GUIDE_ONLY_FACT = "paper_universe_eligible"
DIVIDEND_DECLARED_KEYS = ("amount", "ex_date", "payment_date")
SKEPTIC_VERDICTS = {"VERIFIED", "INVALID", "UNKNOWN"}   # v1.7: UNKNOWN joined VERIFIED and INVALID
REC_REQ = ("schema_version", "symbol", "exchange", "currency", "universe", "next_earnings", "latest_release_date",
           "risk_flags", "verification_status", "verification_reasons", "verified_by", "checked_at_utc",
           "valid_until_utc", "dividend", "verified_at_utc")
WHEEL_REQ = ("latest_annual_operating_cash_flow", "ocf_filing_date", "thesis_evidence", "paper_universe_eligible",
             "going_concern_flag", "bankruptcy_flag", "ownership_card_status", "card_created_at_utc",
             "card_valid_until_utc")
FILE_REQ = ("schema_version", "published_at_utc", "publisher", "records")


def parse_ts(s):
    if not isinstance(s, str) or not (s.endswith("Z") or "+00:00" in s):
        raise ValueError(f"timestamp must be UTC: {s!r}")
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def schema_status(root, policy):
    path = os.path.join(root, policy["research"]["schema_file"])
    actual = sha256_file(path)
    signed = policy["research"].get("signed_schema_sha256") or ""
    if not signed:
        return False, "SCHEMA_NOT_SIGNED", actual
    if signed != actual:
        return False, "SCHEMA_HASH_MISMATCH", actual
    try:
        with open(path) as fh:
            ver = json.load(fh).get("schema_version")
    except (OSError, ValueError):
        ver = None
    if ver != PINNED_SCHEMA_VERSION:
        return False, "SCHEMA_VERSION_MISMATCH", actual
    return True, "OK", actual


# ---------------- reason codes (one specific code per block; journalled and reported) ----------------
# SCHEMA_INVALID:<field>        field missing, wrong type, null where the schema gives no null, bad enum, non-UTC time
# VERSION_MISMATCH              file or record schema_version != pinned news-research-v1.7
# DIVIDEND_NOT_YET_DECLARED     dividend is NOT_YET_DECLARED; blocks covered-call entry only
# TEST_FILE                     file (or record) carries "test": true; never usable for exposure
# SYMBOL_NOT_ALLOWED            symbol not in the 19-name US universe
# NOT_VERIFIED:<status>         verification_status != VERIFIED (PENDING / INVALID / UNKNOWN / ...)
# VERIFIER_NOT_SKEPTIC          verified_by is a string other than "Skeptic"
# VERIFIED_AT_MISSING           status VERIFIED but verified_at_utc null
# VERIFIED_BEFORE_CHECK         verified_at_utc < checked_at_utc (record edited after verification)
# STALE                         now >= valid_until_utc
# VALIDITY_OVER_24H             valid_until_utc more than 24h after checked_at_utc
# FUTURE_TIMESTAMP:<field>      checked_at_utc / card_created_at_utc more than 5 min in the future
# FACT_UNKNOWN:<field>          an eligibility fact is UNKNOWN or null (UNKNOWN fails every gate)
# RUMOUR_NOT_ELIGIBLE:<field>   evidence_kind RUMOUR used for an eligibility fact
# Wheel card only (block Wheel use, not stock use):
# OCF_NOT_POSITIVE, NOT_PAPER_UNIVERSE_ELIGIBLE, DISTRESS_FLAG:<field>, OWNERSHIP_NOT_VERIFIED:<status>,
# CARD_VALIDITY_OVER_72H, CARD_EXPIRED, CARD_OLDER_THAN_72H, CARD_CREATED_MOVED
# Loader: DUPLICATE_SYMBOL, INVALID_JSON
CONFIDENCE = {"HIGH", "MEDIUM", "LOW"}
EVIDENCE_KINDS = {"FILING", "ANNOUNCEMENT", "RUMOUR", "INTERNAL_POLICY"}
UNIVERSES = {"US_STOCK", "WHEEL", "ALERT_ONLY"}
VERIFICATION = {"PENDING", "VERIFIED", "INVALID", "UNKNOWN"}
CARD_STATUS = {"DRAFT", "SENT_TO_SKEPTIC", "VERIFIED", "INVALID", "EXPIRED"}


def _add(errs, code):
    if code not in errs:
        errs.append(code)


def _ts(rec, field, errs, prefix=""):
    try:
        return parse_ts(rec.get(field))
    except (ValueError, TypeError):
        _add(errs, f"SCHEMA_INVALID:{prefix}{field}")
        return None


def _fact(f, name, errs, eligibility=False):
    """Validate a common_fact. v1.6 null rule (unchanged in v1.7): source_url, source_type, evidence_kind and
    published_or_as_of_utc may be null only when the fact's value is exactly "UNKNOWN". A known value (or a
    null value) with any null source field is SCHEMA_INVALID:<name>.<key>. retrieved_at_utc is never null.
    v1.7: INTERNAL_GUIDE / INTERNAL_POLICY may back only paper_universe_eligible. source_url must be https://
    for every source type, including INTERNAL_GUIDE (no file:// exception).
    Returns the value, or "UNKNOWN" for an UNKNOWN/null value (FACT_UNKNOWN is added by the caller's gate)."""
    if f is None or f == "UNKNOWN":
        if f is None:
            _add(errs, f"SCHEMA_INVALID:{name}")
        return "UNKNOWN"
    if not isinstance(f, dict):
        _add(errs, f"SCHEMA_INVALID:{name}"); return None
    for k in FACT_REQ:
        if k not in f:
            _add(errs, f"SCHEMA_INVALID:{name}.{k}")
    # v1.6: these keys are skipped (allowed) only when they are null AND value == "UNKNOWN"
    null_ok = {k for k in NULLABLE_IF_UNKNOWN if f.get("value") == "UNKNOWN" and k in f and f[k] is None}
    if "source_type" in f and "source_type" not in null_ok and f.get("source_type") not in SOURCE_TYPES:
        _add(errs, f"SCHEMA_INVALID:{name}.source_type")
    if "evidence_kind" in f and "evidence_kind" not in null_ok and f.get("evidence_kind") not in EVIDENCE_KINDS:
        _add(errs, f"SCHEMA_INVALID:{name}.evidence_kind")
    # v1.7 internal_guide_rule: these values are SCHEMA_INVALID on any fact except paper_universe_eligible.
    if name != GUIDE_ONLY_FACT:
        if f.get("source_type") == GUIDE_ONLY_SOURCE:
            _add(errs, f"SCHEMA_INVALID:{name}.source_type")
        if f.get("evidence_kind") == GUIDE_ONLY_EVIDENCE:
            _add(errs, f"SCHEMA_INVALID:{name}.evidence_kind")
    if "confidence" in f and f.get("confidence") not in CONFIDENCE:
        _add(errs, f"SCHEMA_INVALID:{name}.confidence")
    # https:// for every source type. INTERNAL_GUIDE does not get a file:// exception.
    if "source_url" in f and "source_url" not in null_ok and not (
            isinstance(f.get("source_url"), str) and f["source_url"].startswith("https://")):
        _add(errs, f"SCHEMA_INVALID:{name}.source_url")
    for k in ("published_or_as_of_utc", "retrieved_at_utc"):
        if k in f and k not in null_ok:
            _ts(f, k, errs, f"{name}.")
    if eligibility and f.get("evidence_kind") == "RUMOUR":
        _add(errs, f"RUMOUR_NOT_ELIGIBLE:{name}")
    if f.get("value") in ("UNKNOWN", None):
        return "UNKNOWN"
    return f.get("value")


def _ex_date_is_past(ex, now):
    """True when ex_date is before now, False when it is not, None when it cannot be read.

    A YYYY-MM-DD date is past only when that calendar day is before now's UTC date, so today's
    ex_date is still the next dividend. A UTC timestamp is past when the instant is strictly before now.
    """
    if not isinstance(ex, str) or not ex:
        return None
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if len(ex) == 10:
        try:
            return date.fromisoformat(ex) < now_utc.date()
        except ValueError:
            return None
    try:
        return parse_ts(ex) < now_utc
    except (ValueError, TypeError):
        return None


def _dividend(rec, errs, notes, now):
    """Resolve dividend. Returns the parsed value stored on the record.

    Shape: common_fact {amount, ex_date, payment_date} for the next declared dividend, or the sentinels
    NOT_YET_DECLARED, VERIFIED_NONE, and UNKNOWN. NOT_YET_DECLARED and VERIFIED_NONE may be a bare string
    or a sourced common_fact whose value is that sentinel. A wrapped sentinel is checked with the
    common_fact rules (https source, source type, timestamps); a bad wrapper is SCHEMA_INVALID. A valid
    wrapped VERIFIED_NONE is no dividend, the same as the bare sentinel. A past ex_date is SCHEMA_INVALID.
    NOT_YET_DECLARED does not fail the record; it is unresolved risk, like UNKNOWN, and blocks covered
    calls only. The schema has no expected-declaration date, so notes are not parsed.
    """
    if "dividend" not in rec:
        notes.append("FACT_UNKNOWN:dividend (blocks covered calls only)")
        return "UNKNOWN"
    dv = rec.get("dividend")
    if dv == "VERIFIED_NONE" or (isinstance(dv, dict) and dv.get("value") == "VERIFIED_NONE"):
        if isinstance(dv, dict):
            _fact(dv, "dividend", errs, True)
        return None
    if dv == "NOT_YET_DECLARED" or (isinstance(dv, dict) and dv.get("value") == "NOT_YET_DECLARED"):
        if isinstance(dv, dict):
            _fact(dv, "dividend", errs, True)
        notes.append("DIVIDEND_NOT_YET_DECLARED (blocks covered calls only)")
        return "NOT_YET_DECLARED"
    v = _fact(dv, "dividend", errs, True)
    if v in (None, "UNKNOWN"):
        notes.append("FACT_UNKNOWN:dividend (blocks covered calls only)")
        return "UNKNOWN"
    if not isinstance(v, dict) or any(k not in v for k in DIVIDEND_DECLARED_KEYS):
        _add(errs, "SCHEMA_INVALID:dividend")
        return None
    past = _ex_date_is_past(v.get("ex_date"), now)
    if past is None or past:
        _add(errs, "SCHEMA_INVALID:dividend.ex_date")
        return None
    return v


def _gate_fact(rec, field, errs, label=None):
    """Eligibility fact: UNKNOWN/null fails the gate with FACT_UNKNOWN:<field>."""
    if field not in rec:
        _add(errs, f"SCHEMA_INVALID:{field}")
        return None
    v = _fact(rec.get(field), label or field, errs, True)
    if v in (None, "UNKNOWN", ""):
        _add(errs, f"FACT_UNKNOWN:{field}")
        return None
    return v


def validate_record(rec, allowed_symbols, wheel_universe, now, test_file=False):
    """Returns (usable_for_new_stock_entries: bool, reason_codes: list, parsed: dict).
    parsed["wheel_ok"] / parsed["wheel_reasons"] cover Wheel use (record codes + Wheel card codes)."""
    errs, out = [], {}
    if not isinstance(rec, dict):
        return False, ["SCHEMA_INVALID:record"], {"wheel_ok": False, "wheel_reasons": ["SCHEMA_INVALID:record"]}
    if test_file or rec.get("test") is True:
        _add(errs, "TEST_FILE")
    for k in REC_REQ:
        if k not in rec:
            _add(errs, f"SCHEMA_INVALID:{k}")
    if "schema_version" in rec and rec.get("schema_version") != PINNED_SCHEMA_VERSION:
        _add(errs, "VERSION_MISMATCH")
    sym = rec.get("symbol")
    out["symbol"] = sym if isinstance(sym, str) else None
    if not isinstance(sym, str):
        _add(errs, "SCHEMA_INVALID:symbol")
    elif sym not in allowed_symbols:
        _add(errs, "SYMBOL_NOT_ALLOWED")
    for k in ("exchange", "currency"):
        if k in rec and not isinstance(rec.get(k), str):
            _add(errs, f"SCHEMA_INVALID:{k}")
    if "universe" in rec and rec.get("universe") not in UNIVERSES:
        _add(errs, "SCHEMA_INVALID:universe")
    checked = _ts(rec, "checked_at_utc", errs) if "checked_at_utc" in rec else None
    valid_until = _ts(rec, "valid_until_utc", errs) if "valid_until_utc" in rec else None
    if checked and valid_until and valid_until - checked > timedelta(hours=24):
        _add(errs, "VALIDITY_OVER_24H")
    if valid_until and now >= valid_until:
        _add(errs, "STALE")
    if checked and checked > now + timedelta(minutes=5):
        _add(errs, "FUTURE_TIMESTAMP:checked_at_utc")
    vs = rec.get("verification_status")
    if "verification_status" in rec and vs not in VERIFICATION:
        _add(errs, "SCHEMA_INVALID:verification_status")
    if vs != "VERIFIED":
        _add(errs, f"NOT_VERIFIED:{vs if isinstance(vs, str) else 'null'}")
    vb = rec.get("verified_by")
    if "verified_by" in rec:
        # Null only while verified_at_utc is null. v1.7 requires "Skeptic" for any Skeptic verdict
        # (VERIFIED, INVALID, or UNKNOWN), not only VERIFIED and INVALID.
        if vb is None:
            if rec.get("verified_at_utc") is not None or vs in SKEPTIC_VERDICTS:
                _add(errs, "SCHEMA_INVALID:verified_by")
        elif vb != "Skeptic":
            _add(errs, "SCHEMA_INVALID:verified_by" if not isinstance(vb, str) else "VERIFIER_NOT_SKEPTIC")
    if not isinstance(rec.get("verification_reasons", []), list):
        _add(errs, "SCHEMA_INVALID:verification_reasons")
    va_raw = rec.get("verified_at_utc")
    if va_raw is None:
        if vs == "VERIFIED":
            _add(errs, "VERIFIED_AT_MISSING")
    else:
        va = _ts(rec, "verified_at_utc", errs)
        if va and checked and va < checked:
            _add(errs, "VERIFIED_BEFORE_CHECK")
    ne = _gate_fact(rec, "next_earnings", errs)
    if isinstance(ne, dict):
        out["earnings_earliest"], out["earnings_latest"] = ne.get("earliest"), ne.get("latest")
    elif ne is not None:
        out["earnings_earliest"] = out["earnings_latest"] = ne
    out["latest_release_date"] = _gate_fact(rec, "latest_release_date", errs) or "UNKNOWN"
    # UNKNOWN and NOT_YET_DECLARED do not block the record; cycle.py blocks covered calls from them.
    out["notes"] = []
    out["dividend"] = _dividend(rec, errs, out["notes"], now)
    flags = rec.get("risk_flags")
    if "risk_flags" in rec and not isinstance(flags, list):
        _add(errs, "SCHEMA_INVALID:risk_flags"); flags = []
    for i, f in enumerate(flags or []):
        _fact(f, f"risk_flags[{i}]", errs)
    out["risk_flags"] = [f.get("value") for f in (flags or []) if isinstance(f, dict)]
    # wheel card
    if sym in wheel_universe and rec.get("universe") == "WHEEL":
        werrs = []
        for k in WHEEL_REQ:
            if k not in rec:
                _add(werrs, f"SCHEMA_INVALID:{k}")
        ocf = _gate_fact(rec, "latest_annual_operating_cash_flow", werrs) if "latest_annual_operating_cash_flow" in rec else None
        if ocf is not None:
            amt = ocf.get("amount") if isinstance(ocf, dict) else None
            if not isinstance(amt, (int, float)) or isinstance(amt, bool) or amt <= 0:
                _add(werrs, "OCF_NOT_POSITIVE")
        # v1.5: OCF filing date is its own fact. Required, as the runner required it before.
        out["ocf_filing_date"] = _gate_fact(rec, "ocf_filing_date", werrs) if "ocf_filing_date" in rec else None
        if "paper_universe_eligible" in rec:
            pue = _gate_fact(rec, "paper_universe_eligible", werrs)
            if pue is not None and pue is not True:
                _add(werrs, "NOT_PAPER_UNIVERSE_ELIGIBLE")
        for fl in ("going_concern_flag", "bankruptcy_flag"):
            v = _gate_fact(rec, fl, werrs) if fl in rec else None
            out[fl] = v if v is not None else "UNKNOWN"
            if v is not None and v not in ("FALSE", False):
                _add(werrs, f"DISTRESS_FLAG:{fl}")
        cs = rec.get("ownership_card_status")
        if "ownership_card_status" in rec and cs not in CARD_STATUS:
            _add(werrs, "SCHEMA_INVALID:ownership_card_status")
        if cs != "VERIFIED":
            _add(werrs, f"OWNERSHIP_NOT_VERIFIED:{cs if isinstance(cs, str) else 'null'}")
        # v1.5: card age is measured from the immutable card_created_at_utc, never from checked_at_utc.
        created = _ts(rec, "card_created_at_utc", werrs) if "card_created_at_utc" in rec else None
        cv = _ts(rec, "card_valid_until_utc", werrs) if "card_valid_until_utc" in rec else None
        if created and cv:
            out["card_created_at_utc"], out["card_valid_until_utc"] = iso(created), iso(cv)
            if cv - created > timedelta(hours=CARD_MAX_HOURS):
                _add(werrs, "CARD_VALIDITY_OVER_72H")
            if cv <= created:
                _add(werrs, "SCHEMA_INVALID:card_valid_until_utc")
        if created and created > now + timedelta(minutes=5):
            _add(werrs, "FUTURE_TIMESTAMP:card_created_at_utc")
        if cv and now >= cv:
            _add(werrs, "CARD_EXPIRED")
        if created and now - created >= timedelta(hours=CARD_MAX_HOURS):
            _add(werrs, "CARD_OLDER_THAN_72H")
        out["wheel_ok"] = not errs and not werrs
        out["wheel_reasons"] = werrs
        out["distress_flag"] = out.get("going_concern_flag") in ("TRUE", True) or out.get("bankruptcy_flag") in ("TRUE", True)
    else:
        out["wheel_ok"] = False
        out["wheel_reasons"] = ["NO_WHEEL_CARD"]
    return (not errs), errs, out


class ResearchBridge:
    def __init__(self, journal, repo, token, session=None, clock=None):
        if repo and not isinstance(repo, str):
            raise ValueError("repo")
        self.j, self.repo, self.token = journal, repo, token
        self.s = session or requests.Session()
        self.clock = clock or (journal.clock if journal else None)

    def poll(self, min_interval_sec=900, force=False):
        last = self.j.get("research_last_poll")
        now = self.clock()
        if last and not force and (now - datetime.fromisoformat(last)).total_seconds() < min_interval_sec:
            return "SKIPPED_INTERVAL"
        self.j.set("research_last_poll", now.isoformat())
        h = {"Authorization": f"Bearer {self.token}", "Accept": "application/vnd.github+json",
             "X-GitHub-Api-Version": "2022-11-28"}
        r = self.s.get(f"{GITHUB_API}/repos/{self.repo}/contents/research", headers=h, timeout=15,
                       allow_redirects=False)
        if r.status_code != 200:
            self.j.event("RESEARCH_POLL_FAILED", {"http": r.status_code})
            return "FAILED"
        for item in r.json():
            name = item.get("name", "")
            if item.get("type") != "file" or not name.endswith(".json") or "/" in name or ".." in name:
                continue
            path = f"research/{name}"
            row = self.j.db.execute("SELECT etag FROM research_cache WHERE path=?", (path,)).fetchone()
            hh = dict(h)
            if row and row["etag"]:
                hh["If-None-Match"] = row["etag"]
            fr = self.s.get(f"{GITHUB_API}/repos/{self.repo}/contents/{path}", headers=hh, timeout=15,
                            allow_redirects=False)
            if fr.status_code == 304:
                continue
            if fr.status_code != 200:
                self.j.event("RESEARCH_FILE_FAILED", {"path": path, "http": fr.status_code}); continue
            body = fr.json()
            if body.get("encoding") != "base64" or body.get("size", 0) > 1_000_000:
                continue
            text = base64.b64decode(body["content"]).decode("utf-8")
            self.j.db.execute("INSERT INTO research_cache(path,etag,content,fetched_utc) VALUES(?,?,?,?) "
                              "ON CONFLICT(path) DO UPDATE SET etag=excluded.etag, content=excluded.content, "
                              "fetched_utc=excluded.fetched_utc", (path, fr.headers.get("ETag"), text, iso(now)))
            self.j.event("RESEARCH_FILE_FETCHED", {"path": path, "sha": body.get("sha")})
        return "OK"


def validate_file(data):
    """v1.7 file wrapper (same shape as v1.6). Returns (records or None, file_codes). Only wrapper problems reject the whole file;
    anything inside a record (null/UNKNOWN facts, bad fields) blocks that record only."""
    if not isinstance(data, dict):
        return None, ["SCHEMA_INVALID:file"]
    errs = [f"SCHEMA_INVALID:file.{k}" for k in FILE_REQ if k not in data]
    if "schema_version" in data and data.get("schema_version") != PINNED_SCHEMA_VERSION:
        errs.append("VERSION_MISMATCH")
    if "publisher" in data and data.get("publisher") != PUBLISHER:
        errs.append("SCHEMA_INVALID:file.publisher")
    if "published_at_utc" in data:
        try:
            parse_ts(data.get("published_at_utc"))
        except (ValueError, TypeError):
            errs.append("SCHEMA_INVALID:file.published_at_utc")
    if "records" in data and not isinstance(data.get("records"), list):
        errs.append("SCHEMA_INVALID:file.records")
    return (None if errs else data["records"]), errs


def check_text(text, cfg, now):
    """Pure validation of one research file's text -> report dict (no journal, no network).
    {"file_status": ACCEPTED|REJECTED, "file_codes": [...], "test_file": bool, "records": [
       {"index", "symbol", "stock_use": ACCEPTED|BLOCKED, "codes": [...], "wheel_use": ..., "wheel_codes": [...],
        "notes": [...], "_parsed": {...}}]}"""
    try:
        data = json.loads(text)
    except ValueError:
        return {"file_status": "REJECTED", "file_codes": ["INVALID_JSON"], "test_file": False, "records": []}
    recs, fcodes = validate_file(data)
    test_file = isinstance(data, dict) and data.get("test") is True
    rep = {"file_status": "REJECTED" if recs is None else "ACCEPTED", "file_codes": fcodes,
           "test_file": test_file, "records": []}
    for i, rec in enumerate(recs or []):
        ok, codes, parsed = validate_record(rec, set(cfg.us_symbols), set(cfg.wheel_symbols), now, test_file)
        is_card = parsed.get("wheel_reasons") != ["NO_WHEEL_CARD"]
        rep["records"].append({"index": i, "symbol": parsed.get("symbol"),
                               "stock_use": "ACCEPTED" if ok else "BLOCKED", "codes": codes,
                               "wheel_use": ("ACCEPTED" if parsed.get("wheel_ok") else "BLOCKED") if is_card else "NO_WHEEL_CARD",
                               "wheel_codes": parsed.get("wheel_reasons", []) if is_card else [],
                               "notes": parsed.get("notes", []), "_parsed": parsed})
    return rep


def _card_immutability(journal, sym, parsed, now):
    """card_created_at_utc must never move while the card it belongs to is still valid ("never moved by
    refreshes"). Returns True if it moved. Remembers the accepted card per symbol in the journal kv."""
    created, cv = parsed.get("card_created_at_utc"), parsed.get("card_valid_until_utc")
    if not created:
        return False
    key = f"wheel_card_seen:{sym}"
    seen = journal.get(key)
    if seen and seen["created"] != created and now < parse_ts(seen["valid_until"]):
        journal.event("RESEARCH_CARD_CREATED_MOVED", {"seen": seen, "new_created": created}, "WHEEL", sym)
        return True
    if not seen or seen["created"] != created:
        journal.set(key, {"created": created, "valid_until": cv})
    return False


def _eid(*parts):
    import hashlib
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:32]


def load_records(journal, cfg, now, report=None):
    """Parse cached research JSON (data only) -> {symbol: (ok, codes, parsed)}. Every rejected file and every
    blocked record is journalled with its reason codes (deduplicated per file content + codes). If `report`
    (a list) is given, per-record results are appended to it for the cycle summary."""
    out = {}
    for row in journal.db.execute("SELECT path, content FROM research_cache"):
        path, text = row["path"], row["content"]
        rep = check_text(text, cfg, now)
        csum = _eid(text)
        if rep["file_status"] == "REJECTED":
            journal.event("RESEARCH_FILE_REJECTED", {"path": path, "codes": rep["file_codes"]},
                          event_id="RFR-" + _eid(path, csum, rep["file_codes"]))
            if report is not None:
                report.append({"path": path, "file_status": "REJECTED", "codes": rep["file_codes"]})
            continue
        for r in rep["records"]:
            parsed, sym = r.pop("_parsed"), r["symbol"]
            if parsed.get("wheel_ok") and sym and _card_immutability(journal, sym, parsed, now):
                parsed["wheel_ok"] = False
                parsed["wheel_reasons"] = parsed.get("wheel_reasons", []) + ["CARD_CREATED_MOVED"]
                r["wheel_use"], r["wheel_codes"] = "BLOCKED", parsed["wheel_reasons"]
            ok = r["stock_use"] == "ACCEPTED"
            if sym in out:
                prev = out[sym]
                out[sym] = (False, ["DUPLICATE_SYMBOL"], dict(prev[2], wheel_ok=False, wheel_reasons=["DUPLICATE_SYMBOL"]))
                r = dict(r, stock_use="BLOCKED", codes=r["codes"] + ["DUPLICATE_SYMBOL"], wheel_use="BLOCKED",
                         wheel_codes=r["wheel_codes"] + ["DUPLICATE_SYMBOL"])
            elif sym:
                out[sym] = (ok, r["codes"], parsed)
            blocked = r["stock_use"] == "BLOCKED" or r["wheel_use"] == "BLOCKED"
            payload = {"path": path, "index": r["index"], "stock_use": r["stock_use"], "codes": r["codes"],
                       "wheel_use": r["wheel_use"], "wheel_codes": r["wheel_codes"]}
            journal.event("RESEARCH_RECORD_BLOCKED" if blocked else "RESEARCH_RECORD_ACCEPTED", payload,
                          "WHEEL" if r["wheel_use"] != "NO_WHEEL_CARD" else "STOCK", sym,
                          event_id="RR-" + _eid(path, csum, payload))
            if report is not None:
                report.append(dict(payload, symbol=sym))
    return out


def fetch_research_readonly(repo, token, session=None):
    """GET-only fetch of research/*.json for `desk.py research-check`. Never writes to GitHub and never
    touches the journal. Pins all reads to the latest commit that touched research/ when it can be read.
    Returns {"commit": sha|None, "files": [{"path","blob_sha","etag","text"|None,"http"}], "error": str|None}."""
    if not (isinstance(repo, str) and repo.count("/") == 1 and ".." not in repo and " " not in repo):
        return {"commit": None, "files": [], "error": "GITHUB_RESEARCH_REPO is not owner/name"}
    s = session or requests.Session()
    h = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
         "X-GitHub-Api-Version": "2022-11-28"}
    base = f"{GITHUB_API}/repos/{repo}"
    commit = None
    r = s.get(f"{base}/commits", params={"path": "research", "per_page": 1}, headers=h, timeout=15,
              allow_redirects=False)
    if r.status_code == 200 and isinstance(r.json(), list) and r.json():
        commit = r.json()[0].get("sha")
    ref = {"ref": commit} if commit else {}
    r = s.get(f"{base}/contents/research", params=ref, headers=h, timeout=15, allow_redirects=False)
    if r.status_code != 200:
        return {"commit": commit, "files": [], "error": f"listing research/ failed: HTTP {r.status_code}"}
    files = []
    for item in r.json() if isinstance(r.json(), list) else []:
        name = item.get("name", "")
        if item.get("type") != "file" or not name.endswith(".json") or "/" in name or ".." in name:
            continue
        path = f"research/{name}"
        fr = s.get(f"{base}/contents/{path}", params=ref, headers=h, timeout=15, allow_redirects=False)
        row = {"path": path, "blob_sha": None, "etag": fr.headers.get("ETag"), "text": None, "http": fr.status_code}
        if fr.status_code == 200:
            body = fr.json()
            row["blob_sha"] = body.get("sha")
            if body.get("encoding") == "base64" and body.get("size", 0) <= 1_000_000:
                row["text"] = base64.b64decode(body["content"]).decode("utf-8", "replace")
        files.append(row)
    return {"commit": commit, "files": files, "error": None}
