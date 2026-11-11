"""SQLite journal. `events` is append-only (UPDATE/DELETE blocked by triggers). Mutable working tables
(intents, reservations, wheel cycles, outbox) record every change as an event too."""
import json, os, sqlite3, uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SYD = ZoneInfo("Australia/Sydney")

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
  ts_utc TEXT NOT NULL, ts_sydney TEXT NOT NULL, experiment_id TEXT, strategy_version TEXT, config_hash TEXT,
  feed_profile TEXT, run_id TEXT, kind TEXT NOT NULL, strategy TEXT, symbol TEXT, payload TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TRIGGER IF NOT EXISTS events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT,'append-only'); END;
CREATE TABLE IF NOT EXISTS intents(client_order_id TEXT PRIMARY KEY, created_utc TEXT, strategy TEXT, symbol TEXT,
  side TEXT, qty REAL, limit_price REAL, purpose TEXT, body TEXT, status TEXT, broker_order_id TEXT,
  filled_qty REAL DEFAULT 0, filled_avg_price REAL, strategy_version TEXT, config_hash TEXT, signal_ts TEXT,
  attempt INTEGER DEFAULT 0, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS reservations(res_id TEXT PRIMARY KEY, ledger TEXT, kind TEXT, symbol TEXT,
  amount REAL, client_order_id TEXT, status TEXT, created_utc TEXT, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS wheel_cycles(cycle_id TEXT PRIMARY KEY, symbol TEXT, state TEXT, data TEXT,
  created_utc TEXT, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS stock_positions(symbol TEXT PRIMARY KEY, data TEXT, updated_utc TEXT);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS outbox(event_id TEXT PRIMARY KEY, created_utc TEXT, payload TEXT, status TEXT,
  attempts INTEGER DEFAULT 0, next_attempt_utc TEXT, last_error TEXT);
CREATE TABLE IF NOT EXISTS delivery_attempts(id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT, ts_utc TEXT,
  http_status INTEGER, error TEXT);
CREATE TABLE IF NOT EXISTS research_cache(path TEXT PRIMARY KEY, etag TEXT, content TEXT, fetched_utc TEXT);
CREATE TABLE IF NOT EXISTS equity_marks(ts_utc TEXT PRIMARY KEY, session_date TEXT, broker_equity REAL,
  conservative_equity REAL, stock_ledger REAL, wheel_ledger REAL, spy_price REAL);
"""


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


class Journal:
    def __init__(self, path, cfg=None, run_id=None, clock=utcnow):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, isolation_level=None, timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.cfg, self.run_id, self.clock = cfg, run_id or uuid.uuid4().hex[:12], clock

    def tx(self):
        return _Tx(self.db)

    def event(self, kind, payload=None, strategy=None, symbol=None, event_id=None):
        now = self.clock()
        eid = event_id or uuid.uuid4().hex
        c = self.cfg
        self.db.execute(
            "INSERT OR IGNORE INTO events(event_id,ts_utc,ts_sydney,experiment_id,strategy_version,config_hash,"
            "feed_profile,run_id,kind,strategy,symbol,payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (eid, iso(now), now.astimezone(SYD).isoformat(), c and c.policy["experiment_id"],
             c and c.strategy_version, c and c.config_hash, c and c.feed_profile, self.run_id, kind, strategy,
             symbol, json.dumps(payload or {}, default=str, sort_keys=True)))
        return eid

    def events(self, kind=None):
        q = "SELECT * FROM events" + (" WHERE kind=?" if kind else "") + " ORDER BY seq"
        return self.db.execute(q, (kind,) if kind else ()).fetchall()

    def get(self, k, default=None):
        r = self.db.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return json.loads(r["v"]) if r else default

    def set(self, k, v):
        self.db.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                        (k, json.dumps(v, default=str)))


class _Tx:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, et, e, tb):
        self.db.execute("COMMIT" if et is None else "ROLLBACK")
        return False
