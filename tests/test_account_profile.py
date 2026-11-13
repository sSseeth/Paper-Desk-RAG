"""Account profiles and the AUTO_PAPER lifecycle gate. Offline: no broker, no keyring writes."""
import json
import os

import pytest

import desk
from conftest import make_root
from sakibdesk import account_profile as AP
from sakibdesk import cycle as C
from sakibdesk import lifecycle_gate as LG
from sakibdesk import secrets_store as SS
from sakibdesk.config import Config

MAIN_KEYS = (
    "ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET", "ALPACA_EXPECTED_ACCOUNT_ID",
    "GITHUB_READONLY_TOKEN", "GITHUB_RESEARCH_REPO", "GROK_WEBHOOK_URL", "GROK_WEBHOOK_BEARER",
)
MECH_KEYS = (
    "MECH_ALPACA_PAPER_KEY_ID", "MECH_ALPACA_PAPER_SECRET", "MECH_ALPACA_EXPECTED_ACCOUNT_ID",
)
PREFIX = "0123456789abcdef"


def _root(monkeypatch, tmp_path):
    root = make_root(tmp_path)
    monkeypatch.setattr(desk, "ROOT", root)
    return root, Config(root)


def _payloads(root, account, kind):
    import sqlite3
    path = AP.journal_path(root, account)
    db = sqlite3.connect(path)
    rows = db.execute("SELECT payload FROM events WHERE kind=? ORDER BY seq", (kind,)).fetchall()
    db.close()
    return [json.loads(r[0]) for r in rows]


def _write_check(root, name):
    path = LG.gate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    data[name] = {
        "passed_at_utc": "2026-11-13T00:00:00Z",
        "mechanics_account_id_sha256_prefix": PREFIX,
        "runner_git_commit": "UNKNOWN",
        "config_hash": "2df03f83726ac139",
        "evidence": "observed",
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)


class _Reader:
    def __init__(self, account_id):
        self.account_id = account_id

    def account(self):
        return {"id": self.account_id, "account_number": "PA-TEST", "status": "ACTIVE"}


def _install_cycle(monkeypatch, account_id, calls):
    def build(cfg, mode, account="main"):
        calls["build"] = (mode, account)
        writer = object() if mode == "AUTO_PAPER" else None
        return C.Deps(reader=_Reader(account_id), writer=writer, expected_account_id=account_id)

    def run(cfg, journal, mode, deps, root):
        calls["ran"] = (mode, deps.writer is not None)
        return {"entry_blocks": [], "actions": [], "orders": []}

    monkeypatch.setattr(desk, "build_deps", build)
    monkeypatch.setattr(C, "run_cycle", run)


def test_profile_secret_names_and_main_names_unchanged():
    assert SS.SERVICE == "SakibPaperDesk"
    assert SS.KEYS == MAIN_KEYS
    assert SS.alpaca_keys("main") == MAIN_KEYS[:3]
    assert SS.profile_keys("main") == MAIN_KEYS
    assert SS.alpaca_keys("mechanics") == MECH_KEYS
    assert SS.profile_keys("mechanics") == MECH_KEYS
    assert SS.alpaca_keys("main") != SS.alpaca_keys("mechanics")


def test_expected_ids_equal_is_trimmed_and_case_insensitive():
    assert AP.expected_ids_equal("AbC", " abc ")
    assert not AP.expected_ids_equal("AbC", "abd")
    assert not AP.expected_ids_equal("", "abc")
    assert not AP.expected_ids_equal(None, "abc")
    assert not AP.expected_ids_equal("   ", "abc")


def test_main_setup_keeps_historical_secret_names(monkeypatch, tmp_path):
    prompts = []
    monkeypatch.setattr(SS, "_kr", lambda: None)
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(("hidden", prompt)) or "sek")
    monkeypatch.setattr("builtins.input", lambda prompt: prompts.append(("visible", prompt)) or "expected-main")
    SS.setup_interactive(str(tmp_path), "main")
    assert [prompt.split(":")[0] for _kind, prompt in prompts] == list(MAIN_KEYS)
    visible = [prompt for kind, prompt in prompts if kind == "visible"]
    assert visible == ["ALPACA_EXPECTED_ACCOUNT_ID: ", "GITHUB_RESEARCH_REPO: "]
    assert not any("MECH_" in prompt for _kind, prompt in prompts)
    stored = json.load(open(tmp_path / "data" / "secrets.local.json", encoding="utf-8"))
    assert set(stored) == set(MAIN_KEYS)
    assert "MECH_ALPACA_PAPER_KEY_ID" not in stored


def test_mechanics_setup_uses_getpass_and_mech_names_only(monkeypatch, tmp_path):
    prompts = []
    monkeypatch.setattr(SS, "_kr", lambda: None)
    monkeypatch.setattr("getpass.getpass", lambda prompt: prompts.append(prompt) or "hidden-value")

    def echoed(prompt):
        raise AssertionError("input() would echo: " + prompt)

    monkeypatch.setattr("builtins.input", echoed)
    SS.setup_interactive(str(tmp_path), "mechanics")
    assert [prompt.split(":")[0] for prompt in prompts] == list(MECH_KEYS)
    stored = json.load(open(tmp_path / "data" / "secrets.local.json", encoding="utf-8"))
    assert set(stored) == set(MECH_KEYS)
    assert "ALPACA_PAPER_KEY_ID" not in stored


def test_journal_and_report_paths_stay_separate():
    assert AP.journal_path("/r", "main") == os.path.join("/r", "data", "journal.sqlite3")
    assert AP.journal_path("/r", "mechanics") == os.path.join("/r", "data", "mechanics", "journal.sqlite3")
    assert AP.reports_dir("/r", "main") == os.path.join("/r", "reports")
    assert AP.reports_dir("/r", "mechanics") == os.path.join("/r", "reports", "mechanics")
    assert AP.lock_path("/r", "main") != AP.lock_path("/r", "mechanics")
    assert os.path.basename(LG.gate_path("/r")) == "lifecycle_gate.json"
    assert os.path.basename(os.path.dirname(LG.gate_path("/r"))) == "state"


def test_same_expected_account_id_refuses_before_broker(monkeypatch, tmp_path, capsys):
    root, cfg = _root(monkeypatch, tmp_path)

    def secret(name):
        if name.endswith("EXPECTED_ACCOUNT_ID"):
            return " Same-Account "
        return None

    monkeypatch.setattr(desk, "_secret", secret)
    monkeypatch.setattr(desk, "build_deps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("deps")))
    assert desk.cmd_cycle(cfg, "DRY_RUN", "mechanics") == 2
    out = capsys.readouterr().out
    assert "equals the main expected account id" in out
    assert "Same-Account" not in out
    assert _payloads(root, "mechanics", "ACCOUNT_PROFILE_REFUSED") == [{"reason": "SAME_EXPECTED_ACCOUNT_ID"}]
    assert not os.path.exists(AP.journal_path(root, "main"))


def test_broker_account_mismatch_refuses(monkeypatch, tmp_path, capsys):
    root, cfg = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(desk, "_secret", lambda name: None)

    def build(cfg, mode, account="main"):
        return C.Deps(reader=_Reader("broker-other"), writer=None, expected_account_id="expected-main")

    monkeypatch.setattr(desk, "build_deps", build)
    monkeypatch.setattr(C, "run_cycle", lambda *a, **k: (_ for _ in ()).throw(AssertionError("cycle")))
    assert desk.cmd_cycle(cfg, "DRY_RUN", "main") == 2
    out = capsys.readouterr().out
    assert "does not match the selected profile" in out
    assert "broker-other" not in out and "expected-main" not in out
    assert _payloads(root, "main", "ACCOUNT_MISMATCH") == [{"reason": "SELECTED_PROFILE_MISMATCH"}]


@pytest.mark.parametrize("kind", ["missing", "partial", "corrupt"])
def test_auto_paper_main_refused_until_both_checks(monkeypatch, tmp_path, capsys, kind):
    root, cfg = _root(monkeypatch, tmp_path)
    if kind == "partial":
        _write_check(root, "stock_lifecycle")
    elif kind == "corrupt":
        path = LG.gate_path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("{")
    monkeypatch.setattr(desk, "_secret", lambda name: None)
    monkeypatch.setattr(desk, "build_deps", lambda *a, **k: (_ for _ in ()).throw(AssertionError("deps")))
    assert desk.cmd_cycle(cfg, "AUTO_PAPER", "main") == 2
    out = capsys.readouterr().out
    assert "LIFECYCLE_GATE_NOT_PASSED" in out
    assert _payloads(root, "main", "AUTO_PAPER_REFUSED") == [{"reason": "LIFECYCLE_GATE_NOT_PASSED"}]


def test_auto_paper_main_allowed_when_both_checks_recorded(monkeypatch, tmp_path):
    root, cfg = _root(monkeypatch, tmp_path)
    _write_check(root, "stock_lifecycle")
    _write_check(root, "wheel_lifecycle")
    assert LG.gate_passed(root) == (True, "passed")
    calls = {}
    monkeypatch.setattr(desk, "_secret", lambda name: None)
    _install_cycle(monkeypatch, "expected-main", calls)
    assert desk.cmd_cycle(cfg, "AUTO_PAPER", "main") == 0
    assert calls["build"] == ("AUTO_PAPER", "main")
    assert calls["ran"] == ("AUTO_PAPER", True)
    assert _payloads(root, "main", "AUTO_PAPER_REFUSED") == []


def test_mechanics_auto_paper_is_not_gated(monkeypatch, tmp_path):
    root, cfg = _root(monkeypatch, tmp_path)
    path = LG.gate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("not-json")
    calls = {}

    def secret(name):
        if name == "ALPACA_EXPECTED_ACCOUNT_ID":
            return "main-expected"
        if name == "MECH_ALPACA_EXPECTED_ACCOUNT_ID":
            return "mech-expected"
        return None

    monkeypatch.setattr(desk, "_secret", secret)
    _install_cycle(monkeypatch, "mech-expected", calls)
    assert desk.cmd_cycle(cfg, "AUTO_PAPER", "mechanics") == 0
    assert calls["ran"][0] == "AUTO_PAPER"
    assert os.path.exists(AP.journal_path(root, "mechanics"))
    assert not os.path.exists(AP.journal_path(root, "main"))
    mech_report = os.path.join(AP.reports_dir(root, "mechanics"))
    assert os.path.isdir(mech_report)
    assert any(name.startswith("report_") for name in os.listdir(mech_report))
    assert not os.path.isdir(AP.reports_dir(root, "main")) or not any(
        name.startswith("report_") for name in os.listdir(AP.reports_dir(root, "main")))


def test_dry_run_is_not_gated(monkeypatch, tmp_path):
    root, cfg = _root(monkeypatch, tmp_path)
    path = LG.gate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("[]")
    calls = {}
    monkeypatch.setattr(desk, "_secret", lambda name: None)
    _install_cycle(monkeypatch, "expected-main", calls)
    assert desk.cmd_cycle(cfg, "DRY_RUN", "main") == 0
    assert calls["build"] == ("DRY_RUN", "main")
    assert calls["ran"] == ("DRY_RUN", False)
    assert _payloads(root, "main", "AUTO_PAPER_REFUSED") == []


def test_lifecycle_record_refused_on_main(monkeypatch, tmp_path, capsys):
    root, cfg = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(desk, "_cfg", lambda: cfg)
    monkeypatch.setattr(desk, "doctor", lambda *a, **k: (_ for _ in ()).throw(AssertionError("doctor")))
    assert desk.main(["lifecycle-record", "--check", "stock", "--evidence", "stock bracket observed"]) == 2
    out = capsys.readouterr().out
    assert "--account mechanics" in out
    assert not os.path.exists(LG.gate_path(root))


def test_lifecycle_record_does_not_write_raw_account_id(monkeypatch, tmp_path):
    root, cfg = _root(monkeypatch, tmp_path)
    raw = "Mech-Account-Expected-Value"

    def secret(name):
        if name == "MECH_ALPACA_EXPECTED_ACCOUNT_ID":
            return "  " + raw + "  "
        if name == "ALPACA_EXPECTED_ACCOUNT_ID":
            return "main-account-expected-value"
        return "k"

    monkeypatch.setattr(desk, "_cfg", lambda: cfg)
    monkeypatch.setattr(desk, "_secret", secret)
    monkeypatch.setattr(desk, "doctor", lambda *a, **k: (True, []))
    assert desk.main([
        "lifecycle-record", "--account", "mechanics", "--check", "wheel",
        "--evidence", "wheel assignment observed",
    ]) == 0
    text = open(LG.gate_path(root), encoding="utf-8").read()
    assert raw.lower() not in text.lower()
    assert "main-account-expected-value" not in text
    rec = json.loads(text)["wheel_lifecycle"]
    assert rec["mechanics_account_id_sha256_prefix"] == LG.account_id_prefix(raw)
    assert set(rec) == set(LG.REQUIRED)
    assert rec["evidence"] == "wheel assignment observed"
    assert rec["config_hash"] == cfg.config_hash
    leaked = desk.main([
        "lifecycle-record", "--account", "mechanics", "--check", "stock",
        "--evidence", "saw " + raw.lower() + " fill",
    ])
    assert leaked == 2
    text = open(LG.gate_path(root), encoding="utf-8").read()
    assert raw.lower() not in text.lower()
    assert "stock_lifecycle" not in json.loads(text)


def test_doctor_main_lifecycle_gate_does_not_fail_doctor(monkeypatch, tmp_path, capsys):
    root, cfg = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(SS, "missing", lambda root: [])
    monkeypatch.setattr(desk, "_secret", lambda name: None)
    ok, checks = desk.doctor(cfg, verbose=True, online=False, account="main")
    out = capsys.readouterr().out
    assert ok
    assert "[BLOCKED] lifecycle_gate:" in out
    assert [flag for name, flag, _detail in checks if name == "lifecycle_gate"] == [True]
    _write_check(root, "stock_lifecycle")
    _write_check(root, "wheel_lifecycle")
    ok, _checks = desk.doctor(cfg, verbose=True, online=False, account="main")
    assert ok
    assert "[PASS] lifecycle_gate:" in capsys.readouterr().out


def test_doctor_mechanics_checks_only_mech_secrets(monkeypatch, tmp_path, capsys):
    _root_path, cfg = _root(monkeypatch, tmp_path)
    seen = []

    def spy(root, names):
        seen.append(tuple(names))
        return []

    monkeypatch.setattr(SS, "missing_names", spy)
    monkeypatch.setattr(desk, "_secret", lambda name: None)
    ok, checks = desk.doctor(cfg, verbose=True, online=False, account="mechanics")
    assert ok
    assert seen == [MECH_KEYS]
    assert "lifecycle_gate" not in [name for name, _flag, _detail in checks]
    assert "ALPACA_PAPER_KEY_ID" not in capsys.readouterr().out


def test_doctor_fails_when_expected_ids_match(monkeypatch, tmp_path, capsys):
    _root_path, cfg = _root(monkeypatch, tmp_path)
    monkeypatch.setattr(SS, "missing_names", lambda root, names: [])

    def secret(name):
        if name.endswith("EXPECTED_ACCOUNT_ID"):
            return "Shared-Id"
        return None

    monkeypatch.setattr(desk, "_secret", secret)
    ok, checks = desk.doctor(cfg, verbose=True, online=False, account="main")
    assert not ok
    failed = [detail for name, flag, detail in checks if name == "distinct_expected_accounts"]
    assert failed and "Shared-Id" not in failed[0]
    assert "Shared-Id" not in capsys.readouterr().out


def test_cli_account_flag_defaults_to_main(monkeypatch, tmp_path):
    _root_path, cfg = _root(monkeypatch, tmp_path)
    got = {}
    monkeypatch.setattr(desk, "_cfg", lambda: cfg)
    monkeypatch.setattr(SS, "setup_interactive", lambda root, account="main": got.update(setup=account))
    assert desk.main(["setup-secrets"]) == 0
    assert got["setup"] == "main"
    assert desk.main(["setup-secrets", "--account", "mechanics"]) == 0
    assert got["setup"] == "mechanics"
    monkeypatch.setattr(desk, "doctor", lambda cfg, verbose=True, online=True, account="main": (True, []))
    monkeypatch.setattr(desk, "cmd_cycle", lambda cfg, mode, account="main": got.update(cycle=(mode, account)) or 0)
    assert desk.main(["cycle", "--mode", "DRY_RUN"]) == 0
    assert got["cycle"] == ("DRY_RUN", "main")
    assert desk.main(["cycle", "--account", "mechanics", "--mode", "AUTO_PAPER"]) == 0
    assert got["cycle"] == ("AUTO_PAPER", "mechanics")
