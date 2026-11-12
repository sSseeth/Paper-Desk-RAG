"""desk.py research-check: GET-only fetch of research/*.json pinned to a commit, per-file/per-record reason codes,
no trading, no GitHub writes, no journal writes. Mocked GitHub; no network."""
import base64, importlib.util, json, os
from datetime import datetime, timezone

spec = importlib.util.spec_from_file_location("desk", os.path.join(os.path.dirname(__file__), "..", "desk.py"))
desk = importlib.util.module_from_spec(spec); spec.loader.exec_module(desk)
FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "news_bridge_test_v17.json")
NOW = datetime(2026, 11, 11, 17, 40, tzinfo=timezone.utc)


class Resp:
    def __init__(self, code, js=None, headers=None):
        self.status_code, self._js, self.headers = code, js, headers or {}
    def json(self):
        return self._js


class GetOnlyGH:
    """Only .get exists: any attempt to POST/PUT/PATCH/DELETE raises AttributeError."""
    def __init__(self, files, commit="c0ffee1234"):
        self.files, self.commit, self.calls = files, commit, []

    def get(self, url, params=None, headers=None, **kw):
        assert kw.get("allow_redirects") is False and url.startswith("https://api.github.com/repos/o/r/")
        assert headers["Authorization"] == "Bearer tok"
        self.calls.append((url, dict(params or {})))
        if url.endswith("/commits"):
            return Resp(200, [{"sha": self.commit}])
        if url.endswith("/contents/research"):
            return Resp(200, [{"name": n, "type": "file"} for n in self.files] + [{"name": "x.py", "type": "file"},
                                                                                 {"name": "d", "type": "dir"}])
        name = url.rsplit("/", 1)[1]
        if name not in self.files:
            return Resp(404)
        text = self.files[name]
        return Resp(200, {"encoding": "base64", "size": len(text), "sha": f"blob-{name}",
                          "content": base64.b64encode(text.encode()).decode()}, {"ETag": f'"etag-{name}"'})


def test_research_check_reports_bridge_file_blocked_with_codes(cfg, journal, capsys):
    gh = GetOnlyGH({"bridge_test.json": open(FIXTURE).read(), "junk.json": "{nope"})
    n_events = len(journal.events())
    res = desk.run_research_check(cfg, "o/r", "tok", NOW, session=gh)
    assert res["read_only"] and res["commit"] == "c0ffee1234" and res["error"] is None
    assert res["schema"]["status"] == "OK" and res["schema"]["pinned_version"] == "news-research-v1.7"
    assert all(p.get("ref") == "c0ffee1234" for u, p in gh.calls if "/contents/" in u)   # pinned to the commit
    assert not any("x.py" in u for u, _ in gh.calls)
    files = {f["path"]: f for f in res["files"]}
    bt = files["research/bridge_test.json"]
    assert bt["file_status"] == "ACCEPTED" and bt["test_file"] and bt["etag"] == '"etag-bridge_test.json"'
    assert bt["blob_sha"] == "blob-bridge_test.json"
    (r,) = bt["records"]
    assert r["stock_use"] == "BLOCKED" and r["wheel_use"] == "BLOCKED"
    assert r["codes"][:3] == ["TEST_FILE", "NOT_VERIFIED:PENDING", "FACT_UNKNOWN:next_earnings"]
    assert "OWNERSHIP_NOT_VERIFIED:DRAFT" in r["wheel_codes"]
    assert files["research/junk.json"]["file_status"] == "REJECTED"
    assert files["research/junk.json"]["file_codes"] == ["INVALID_JSON"]
    assert len(journal.events()) == n_events                       # journal untouched
    desk.print_research_check(res)
    out = capsys.readouterr().out
    assert "commit=c0ffee1234" in out and "record 0 MU: stock=BLOCKED wheel=BLOCKED" in out
    assert "NOT_VERIFIED:PENDING" in out and "wheel: OWNERSHIP_NOT_VERIFIED:DRAFT" in out
    assert "Nothing was traded and nothing was written to GitHub." in out and "tok" not in out


def test_research_check_listing_failure_reported(cfg):
    class Denied(GetOnlyGH):
        def get(self, url, params=None, headers=None, **kw):
            return Resp(404)
    res = desk.run_research_check(cfg, "o/r", "tok", NOW, session=Denied({}))
    assert res["files"] == [] and "HTTP 404" in res["error"] and res["commit"] is None


def test_research_check_rejects_bad_repo_name(cfg):
    res = desk.run_research_check(cfg, "https://evil/x/y", "tok", NOW, session=GetOnlyGH({}))
    assert res["error"] == "GITHUB_RESEARCH_REPO is not owner/name"


def test_research_check_refuses_without_secrets(monkeypatch, capsys):
    monkeypatch.setattr(desk, "_secret_safe", lambda n: None)
    assert desk.cmd_research_check(desk._cfg()) == 2
    assert "secrets missing" in capsys.readouterr().out


def test_research_check_code_has_no_write_calls():
    import inspect
    from sakibdesk import research as R
    src = inspect.getsource(R.fetch_research_readonly) + inspect.getsource(desk.run_research_check)
    for bad in (".post(", ".put(", ".patch(", ".delete(", "PaperWriter", "Journal(", ".event("):
        assert bad not in src
