"""Outbox + webhook (outbound only) + local HTML/CSV reports. The webhook never carries instructions in:
the runner only POSTs and ignores any response body."""
import csv, html, json, os, time
from datetime import timedelta
import requests
from .journal import iso
from .secrets_store import redact

MAX_ATTEMPTS = 12


def stamp(cfg):
    prof = cfg.feed_profile
    return {"experiment_id": cfg.policy["experiment_id"], "strategy_version": cfg.strategy_version,
            "config_hash": cfg.config_hash, "feed_profile": prof,
            "option_quotes": cfg.policy["data_settings"][prof]["option_quotes_label"],
            "series": f"{prof} (kept separate from the other profile)"}


def enqueue(journal, event_id, payload):
    journal.db.execute("INSERT OR IGNORE INTO outbox(event_id,created_utc,payload,status,attempts,next_attempt_utc)"
                       " VALUES(?,?,?,?,0,?)", (event_id, iso(journal.clock()), json.dumps(payload, default=str),
                                                "PENDING", iso(journal.clock())))


def deliver(journal, url, bearer, session=None, now=None):
    """Retry independently of orders; exponential backoff; dedup by event_id (receiver gets Idempotency-Key)."""
    if not url or not bearer or not url.startswith("https://"):
        return {"sent": 0, "skipped": "webhook not configured"}
    s = session or requests.Session()
    now = now or journal.clock()
    sent = 0
    rows = journal.db.execute("SELECT * FROM outbox WHERE status='PENDING' AND next_attempt_utc<=? ORDER BY created_utc",
                              (iso(now),)).fetchall()
    for r in rows:
        status, err = None, None
        try:
            resp = s.post(url, data=r["payload"], timeout=10, allow_redirects=False,
                          headers={"Authorization": f"Bearer {bearer}", "Content-Type": "application/json",
                                   "Idempotency-Key": r["event_id"], "X-Event-Id": r["event_id"]})
            status = resp.status_code
        except requests.RequestException as e:
            err = redact(str(e))
        n = r["attempts"] + 1
        journal.db.execute("INSERT INTO delivery_attempts(event_id,ts_utc,http_status,error) VALUES(?,?,?,?)",
                           (r["event_id"], iso(now), status, err))
        if status and 200 <= status < 300:
            journal.db.execute("UPDATE outbox SET status='DELIVERED', attempts=? WHERE event_id=?", (n, r["event_id"]))
            sent += 1
        else:
            final = n >= MAX_ATTEMPTS or (status is not None and status in (400, 401, 403, 404))
            nxt = now + timedelta(seconds=min(3600, 30 * 2 ** n))
            journal.db.execute("UPDATE outbox SET status=?, attempts=?, next_attempt_utc=?, last_error=? WHERE event_id=?",
                               ("FAILED" if final else "PENDING", n, iso(nxt), err or f"http {status}", r["event_id"]))
    return {"sent": sent}


def write_local(root, cfg, journal, summary, session_label):
    d = os.path.join(root, "reports")
    os.makedirs(d, exist_ok=True)
    st = stamp(cfg)
    csv_path = os.path.join(d, f"events_{session_label}.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["# feed_profile", st["feed_profile"], "option_quotes", st["option_quotes"], "config_hash",
                    st["config_hash"]])
        cols = ["seq", "event_id", "ts_utc", "ts_sydney", "experiment_id", "strategy_version", "config_hash",
                "feed_profile", "run_id", "kind", "strategy", "symbol", "payload"]
        w.writerow(cols)
        for e in journal.events():
            w.writerow([redact(str(e[c])) for c in cols])
    html_path = os.path.join(d, f"report_{session_label}.html")
    body = json.dumps({"stamp": st, **summary}, indent=2, default=str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(f"<html><head><meta charset='utf-8'><title>SakibPaperDesk {html.escape(session_label)}</title></head>"
                f"<body><h1>SakibPaperDesk report {html.escape(session_label)}</h1>"
                f"<p><b>Feed profile: {html.escape(st['feed_profile'])}</b> | Option quotes: "
                f"<b>{html.escape(st['option_quotes'])}</b> | PAPER ONLY | config {st['config_hash']}</p>"
                f"<pre>{html.escape(redact(body))}</pre></body></html>")
    return html_path, csv_path
