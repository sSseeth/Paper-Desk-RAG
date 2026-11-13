"""Local record that lifecycle checks passed on the mechanics paper account.

`state/lifecycle_gate.json` is gitignored. Each check stores a sha256 prefix of
the mechanics expected account id, never the raw id, plus the runner git
commit, the config hash, and a short evidence note.

Missing or unreadable contents are not a pass. AUTO_PAPER on the main account
stays refused until both checks are recorded.
"""
import hashlib
import json
import os
import subprocess

from .journal import iso, utcnow

GATE_FILENAME = "lifecycle_gate.json"
CHECKS = ("stock_lifecycle", "wheel_lifecycle")
CLI_CHECKS = {"stock": "stock_lifecycle", "wheel": "wheel_lifecycle"}
PREFIX_LEN = 16
REQUIRED = (
    "passed_at_utc",
    "mechanics_account_id_sha256_prefix",
    "runner_git_commit",
    "config_hash",
    "evidence",
)


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


def _valid_record(rec):
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
    """Both lifecycle checks recorded with the required fields.

    Returns (passed, reason). `reason` is 'passed', 'missing', 'corrupt',
    'partial', or 'incomplete'. Any reason other than 'passed' refuses
    main-account AUTO_PAPER.
    """
    data, err = load_gate(root)
    if err:
        return False, err
    valid = [name for name in CHECKS if _valid_record(data.get(name))]
    if len(valid) == len(CHECKS):
        return True, "passed"
    if valid:
        return False, "partial"
    return False, "incomplete"


def _leaks_account_id(evidence, expected_account_id):
    raw = str(expected_account_id or "").strip()
    if len(raw) < 4:
        return False
    return raw.lower() in (evidence or "").lower()


def record_check(root, check, expected_account_id, config_hash, evidence):
    """Record one mechanics lifecycle check. Never writes the raw account id.

    Returns (ok, message). A corrupt existing file is left untouched.
    """
    key = CLI_CHECKS.get(check)
    if key is None:
        return False, "unknown check"
    note = (evidence or "").strip()
    if not note:
        return False, "evidence is required"
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
