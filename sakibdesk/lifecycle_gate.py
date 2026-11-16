"""Local record that lifecycle checks passed on the mechanics paper account.

`state/lifecycle_gate.json` is gitignored. Each check stores a sha256 prefix of
the mechanics expected account id, never the raw id, plus the runner git
commit, the config hash, and a short evidence note.

Missing or unreadable contents are not a pass. Main-account AUTO_PAPER stays
refused until `stock_lifecycle` is recorded. Wheel entries are a separate
decision: they need `wheel_sell_buyback` and the mock wheel paths, and they
stay PROVISIONAL until `wheel_lifecycle` is recorded from an observed paper
assignment.
"""
import hashlib
import json
import os
import re
import subprocess

from . import wheel as W
from .journal import iso, utcnow

GATE_FILENAME = "lifecycle_gate.json"
CHECKS = ("stock_lifecycle", "wheel_sell_buyback", "wheel_lifecycle")
CLI_CHECKS = {
    "stock": "stock_lifecycle",
    "wheel": "wheel_lifecycle",
    "wheel-sell-buyback": "wheel_sell_buyback",
}
PREFIX_LEN = 16
REQUIRED = (
    "passed_at_utc",
    "mechanics_account_id_sha256_prefix",
    "runner_git_commit",
    "config_hash",
    "evidence",
)
OBSERVED_ASSIGNMENT = "OBSERVED_PAPER_ASSIGNMENT"
MOCK_EVIDENCE = "MOCK"
_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


def gate_path(root):
    """Absolute path of the gitignored gate file."""
    return os.path.join(root, "state", GATE_FILENAME)


def account_id_prefix(expected_account_id):
    """First 16 hex chars of sha256(stripped expected id). Not reversible here."""
    raw = str(expected_account_id).strip().encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:PREFIX_LEN]


def git_commit(root):
    """HEAD of `root` when `root` itself is the work tree, else UNKNOWN.

    A temp copy of the config files must not inherit a parent repository's
    commit. The hash is public; it is not an account id.
    """
    try:
        top = subprocess.run(
            ["git", "-C", root, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=5, check=False)
        if top.returncode != 0:
            return "UNKNOWN"
        if os.path.realpath(top.stdout.strip()) != os.path.realpath(root):
            return "UNKNOWN"
        head = subprocess.run(
            ["git", "-C", root, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return "UNKNOWN"
    if head.returncode != 0:
        return "UNKNOWN"
    line = head.stdout.strip()
    if line and all(c in "0123456789abcdef" for c in line):
        return line
    return "UNKNOWN"


def _valid_record(rec, name=None):
    if not isinstance(rec, dict):
        return False
    for key in REQUIRED:
        value = rec.get(key)
        if not isinstance(value, str) or not value.strip():
            return False
    prefix = rec["mechanics_account_id_sha256_prefix"].strip()
    if len(prefix) != PREFIX_LEN:
        return False
    if any(c not in "0123456789abcdef" for c in prefix):
        return False
    if name == "wheel_lifecycle":
        ok, _why = _evidence_ok("wheel_lifecycle", rec.get("evidence") or "")
        if not ok:
            return False
    return True


def load_gate(root):
    """Return (data, error). `error` is 'missing', 'corrupt', or None.

    A parsed JSON object is returned even when individual checks are incomplete.
    A missing file, unreadable file, or non-object JSON is not a pass.
    """
    path = gate_path(root)
    if not os.path.exists(path):
        return {}, "missing"
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return {}, "corrupt"
    if not isinstance(data, dict):
        return {}, "corrupt"
    return data, None


def gate_passed(root):
    """Whether main AUTO_PAPER may run at all.

    Requires a readable gate file and a valid `stock_lifecycle` record.
    Missing, corrupt, or absent stock_lifecycle refuses the whole cycle.
    Wheel entries are decided separately.

    Returns (allowed, reason).
    """
    data, err = load_gate(root)
    if err:
        return False, err
    if _valid_record(data.get("stock_lifecycle"), "stock_lifecycle"):
        return True, "stock_lifecycle"
    return False, "stock_lifecycle_missing"


class WheelDecision:
    """Wheel-entry decision for one main-account AUTO_PAPER cycle."""

    def __init__(self, wheel_entries, provisional, reason, label):
        self.wheel_entries = wheel_entries
        self.provisional = provisional
        self.reason = reason
        self.label = label


def wheel_entry_decision(root):
    """Wheel entries need sell/buyback evidence and the mock state paths.

    Until `wheel_lifecycle` is recorded the book is PROVISIONAL. A missing or
    corrupt file blocks wheel entries. The caller has already refused the
    cycle when stock_lifecycle itself is missing.
    """
    data, err = load_gate(root)
    mock_ok, _detail = W.mock_paths_pass()
    if err or not _valid_record((data or {}).get("wheel_sell_buyback"), "wheel_sell_buyback"):
        return WheelDecision(False, False, "wheel_sell_buyback_missing", "BLOCKED")
    if not mock_ok:
        return WheelDecision(False, False, "wheel_mock_failed", "BLOCKED")
    if not _valid_record(data.get("wheel_lifecycle"), "wheel_lifecycle"):
        return WheelDecision(True, True, "PROVISIONAL", "PROVISIONAL")
    return WheelDecision(True, False, "wheel_lifecycle", "RECORDED")


def _check_label(data, err, name):
    if err == "corrupt":
        return "UNAVAILABLE|gate file is corrupt"
    if err == "missing" or data.get(name) is None:
        return "NOT_RECORDED|not recorded"
    if _valid_record(data.get(name), name):
        return "RECORDED|recorded"
    return "INVALID|record is incomplete"


def doctor_rows(root):
    """One doctor row per lifecycle check, plus the summary line.

    Rows are (name, ok, detail). `detail` is `STATE|text`. The summary does
    not fail doctor. A failed mock wheel path does.
    """
    data, err = load_gate(root)
    mock_ok, mock_detail = W.mock_paths_pass()
    states = {name: _check_label(data, err, name) for name in CHECKS}
    sell_ok = states["wheel_sell_buyback"].startswith("RECORDED")
    stock_ok = states["stock_lifecycle"].startswith("RECORDED")
    full_ok = states["wheel_lifecycle"].startswith("RECORDED")
    if states["wheel_lifecycle"].startswith("NOT_RECORDED") and sell_ok and stock_ok and mock_ok and err is None:
        states["wheel_lifecycle"] = (
            "PROVISIONAL|not recorded; wheel entries stay PROVISIONAL until observed expiry/assignment")
    rows = [(name, True, states[name]) for name in CHECKS]
    mock_state = "PASS" if mock_ok else "FAIL"
    rows.append(("wheel_mock_tests", mock_ok, f"{mock_state}|{mock_detail}"))
    if err == "corrupt":
        summary = "CORRUPT|gate file is corrupt; main AUTO_PAPER stays refused"
    elif err == "missing" or not stock_ok:
        summary = "BLOCKED|main AUTO_PAPER refused until stock_lifecycle is recorded"
    elif full_ok and sell_ok and mock_ok:
        summary = "PASS|stock_lifecycle recorded; wheel_lifecycle recorded"
    elif sell_ok and mock_ok:
        summary = "PASS|stock book allowed; wheel entries PROVISIONAL until wheel_lifecycle"
    else:
        summary = "PASS|stock book allowed; wheel entries blocked until wheel_sell_buyback and mock tests"
    rows.append(("lifecycle_gate", True, summary))
    return rows


def _leaks_account_id(evidence, expected_account_id):
    raw = str(expected_account_id or "").strip()
    if len(raw) < 4:
        return False
    return raw.lower() in (evidence or "").lower()


def _evidence_tokens(note):
    return {token.upper() for token in _TOKEN.findall(note or "")}


def _evidence_ok(key, note):
    """wheel_lifecycle is only an observed paper assignment, never a mock."""
    if key != "wheel_lifecycle":
        return True, ""
    tokens = _evidence_tokens(note)
    if MOCK_EVIDENCE in tokens:
        return False, "wheel_lifecycle cannot be recorded from MOCK evidence"
    if OBSERVED_ASSIGNMENT not in tokens:
        return False, "wheel_lifecycle requires OBSERVED_PAPER_ASSIGNMENT evidence"
    return True, ""


def record_check(root, check, expected_account_id, config_hash, evidence):
    """Record one mechanics lifecycle check. Never writes the raw account id.

    Returns (ok, message). A corrupt existing file is left untouched.
    `wheel_lifecycle` requires OBSERVED_PAPER_ASSIGNMENT and rejects MOCK.
    """
    key = CLI_CHECKS.get(check)
    if key is None:
        return False, "unknown check"
    note = (evidence or "").strip()
    if not note:
        return False, "evidence is required"
    ok_evidence, why = _evidence_ok(key, note)
    if not ok_evidence:
        return False, why
    if _leaks_account_id(note, expected_account_id):
        return False, "evidence must not contain the account id"
    if not expected_account_id or not str(expected_account_id).strip():
        return False, "mechanics expected account id is missing"
    data, err = load_gate(root)
    if err == "corrupt":
        return False, "gate file is corrupt"
    if err == "missing":
        data = {}
    kept = {}
    for name in CHECKS:
        if name == key or name not in data:
            continue
        previous = data.get(name)
        if not isinstance(previous, dict):
            continue
        if _leaks_account_id(str(previous.get("evidence", "")), expected_account_id):
            return False, "existing gate record contains the account id"
        kept[name] = {field: previous.get(field, "") for field in REQUIRED}
    rec = {
        "passed_at_utc": iso(utcnow()),
        "mechanics_account_id_sha256_prefix": account_id_prefix(expected_account_id),
        "runner_git_commit": git_commit(root),
        "config_hash": config_hash,
        "evidence": note[:500],
    }
    for field, value in rec.items():
        if field == "mechanics_account_id_sha256_prefix":
            continue
        if _leaks_account_id(str(value), expected_account_id):
            return False, "refusing to write the raw account id"
    kept[key] = rec
    path = gate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(kept, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)
    return True, key
