"""Order intents, deterministic client_order_id, reservations and reconciliation.

Rules (guide p10/p15):
- intent + reservation are written in ONE DB transaction before any network call;
- client_order_id = hash(experiment, strategy_version, config_hash, strategy, symbol, signal key, purpose, attempt);
- a timeout/5xx after submit marks the intent UNKNOWN; resources stay reserved; next cycle looks the ID up first;
- duplicate scheduling: an existing intent with the same id is never resubmitted unless broker lookup says
  the id does not exist (Alpaca rejects duplicate client_order_id anyway);
- no writer (DRY_RUN) => intent is recorded as DRY_RUN and nothing is sent.
"""
import hashlib, json, uuid
from .alpaca import UncertainResponse, BrokerError
from .journal import iso

OPEN_STATES = {"PERSISTED", "SUBMITTED", "UNKNOWN", "new", "accepted", "pending_new", "partially_filled",
               "accepted_for_bidding", "pending_cancel", "pending_replace", "held", "done_for_day", "stopped",
               "calculated", "suspended"}
FINAL_STATES = {"filled", "canceled", "expired", "rejected", "replaced", "DRY_RUN", "NOT_FOUND_RELEASED"}


def client_order_id(cfg, strategy, symbol, signal_key, purpose, attempt=0):
    raw = "|".join([cfg.policy["experiment_id"], cfg.strategy_version, cfg.config_hash, strategy, symbol,
                    str(signal_key), purpose, str(attempt)])
    return "SPD-" + hashlib.sha256(raw.encode()).hexdigest()[:40]


class OrderManager:
    def __init__(self, journal, cfg, writer=None, reader=None, ledger=None):
        self.j, self.cfg, self.w, self.r, self.ledger = journal, cfg, writer, reader, ledger

    # ---- reservations ----
    def reserved(self, ledger, kind=None, symbol=None):
        q = ("SELECT COALESCE(SUM(amount),0) a FROM reservations WHERE status='ACTIVE' AND ledger=?"
             + ("" if kind else " AND kind!='CALL_SHARES'"))   # CALL_SHARES are shares, not dollars
        a = [ledger]
        if kind:
            q += " AND kind=?"; a.append(kind)
        if symbol:
            q += " AND symbol=?"; a.append(symbol)
        return self.j.db.execute(q, a).fetchone()["a"]

    def release(self, client_order_id, reason, amount=None):
        """Release only confirmed reserves. amount=None releases fully."""
        with self.j.tx() as db:
            rows = db.execute("SELECT * FROM reservations WHERE client_order_id=? AND status='ACTIVE'",
                              (client_order_id,)).fetchall()
            for r in rows:
                if amount is None or amount >= r["amount"]:
                    db.execute("UPDATE reservations SET status='RELEASED', updated_utc=? WHERE res_id=?",
                               (iso(self.j.clock()), r["res_id"]))
                else:
                    db.execute("UPDATE reservations SET amount=amount-?, updated_utc=? WHERE res_id=?",
                               (amount, iso(self.j.clock()), r["res_id"]))
        self.j.event("RESERVATION_RELEASED", {"client_order_id": client_order_id, "reason": reason, "amount": amount})

    def intent(self, cid):
        return self.j.db.execute("SELECT * FROM intents WHERE client_order_id=?", (cid,)).fetchone()

    def open_intents(self, strategy=None):
        rows = self.j.db.execute("SELECT * FROM intents").fetchall()
        return [r for r in rows if r["status"] in OPEN_STATES and (strategy is None or r["strategy"] == strategy)]

    # ---- submit ----
    def place(self, *, strategy, symbol, purpose, signal_key, body, reservations, signal_ts=None,
              cash_check=None, attempt=0):
        """reservations: list of (ledger, kind, amount). cash_check(db) -> (ok, reason) runs inside the txn."""
        cid = client_order_id(self.cfg, strategy, symbol, signal_key, purpose, attempt)
        body = dict(body, client_order_id=cid)
        now = iso(self.j.clock())
        with self.j.tx() as db:
            if db.execute("SELECT 1 FROM intents WHERE client_order_id=?", (cid,)).fetchone():
                self.j.event("DUPLICATE_INTENT_SKIPPED", {"client_order_id": cid}, strategy, symbol)
                return {"status": "DUPLICATE", "client_order_id": cid}
            if cash_check:
                ok, why = cash_check(db)
                if not ok:
                    self.j.event("ORDER_BLOCKED", {"reason": why, "purpose": purpose}, strategy, symbol)
                    return {"status": "BLOCKED", "reason": why, "client_order_id": cid}
            status = "PERSISTED" if self.w else "DRY_RUN"
            db.execute("INSERT INTO intents(client_order_id,created_utc,strategy,symbol,side,qty,limit_price,purpose,"
                       "body,status,strategy_version,config_hash,signal_ts,attempt,updated_utc) "
                       "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (cid, now, strategy, symbol, body.get("side"), float(body.get("qty", 0)),
                        float(body["limit_price"]) if body.get("limit_price") else None, purpose,
                        json.dumps(body), status, self.cfg.strategy_version, self.cfg.config_hash, signal_ts,
                        attempt, now))
            if self.w:
                for ledger, kind, amount, *res_sym in reservations:   # optional 4th item: underlying symbol
                    db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)",
                               (uuid.uuid4().hex, ledger, kind, res_sym[0] if res_sym else symbol, amount, cid,
                                "ACTIVE", now, now))
        self.j.event("ORDER_INTENT", {"client_order_id": cid, "body": body, "reservations": reservations,
                                      "mode": "AUTO_PAPER" if self.w else "DRY_RUN"}, strategy, symbol)
        if not self.w:
            return {"status": "DRY_RUN", "client_order_id": cid}
        return self._send(cid, body, strategy, symbol)

    def _send(self, cid, body, strategy, symbol):
        try:
            o = self.w.submit(body)
        except UncertainResponse as e:
            self._set(cid, "UNKNOWN")
            self.j.event("ORDER_STATUS_UNKNOWN", {"client_order_id": cid, "error": str(e)}, strategy, symbol)
            return {"status": "UNKNOWN", "client_order_id": cid}
        except BrokerError as e:
            self._set(cid, "rejected")
            self.release(cid, "broker_rejected_on_submit")
            self.j.event("ORDER_REJECTED", {"client_order_id": cid, "error": str(e)}, strategy, symbol)
            return {"status": "rejected", "client_order_id": cid}
        self._set(cid, o.get("status", "SUBMITTED"), broker_order_id=o.get("id"))  # fills booked in reconcile
        self.j.event("ORDER_SUBMITTED", {"client_order_id": cid, "broker_order_id": o.get("id"),
                                         "status": o.get("status")}, strategy, symbol)
        return {"status": o.get("status"), "client_order_id": cid, "order": o}

    def _set(self, cid, status, **kw):
        sets = ", ".join(["status=?", "updated_utc=?"] + [f"{k}=?" for k in kw])
        self.j.db.execute(f"UPDATE intents SET {sets} WHERE client_order_id=?",
                          [status, iso(self.j.clock()), *kw.values(), cid])

    def _apply(self, cid, o):
        self._set(cid, o.get("status", "SUBMITTED"), broker_order_id=o.get("id"),
                  filled_qty=float(o.get("filled_qty") or 0),
                  filled_avg_price=float(o["filled_avg_price"]) if o.get("filled_avg_price") else None)

    # ---- reconcile ----
    def reconcile(self, reader):
        """Look up every non-final intent by client_order_id. Unknown stays locked."""
        changes = []
        for it in self.open_intents():
            if it["status"] == "DRY_RUN":
                continue
            cid = it["client_order_id"]
            try:
                o = reader.order_by_client_id(cid)
            except BrokerError as e:
                self.j.event("RECONCILE_LOOKUP_FAILED", {"client_order_id": cid, "error": str(e)})
                continue
            if o is None:
                if it["status"] in ("UNKNOWN", "PERSISTED"):
                    # broker has no such order: safe to resend the SAME client_order_id (never a new id)
                    self.j.event("ORDER_NOT_FOUND_RESENDING_SAME_ID", {"client_order_id": cid})
                    if self.w:
                        changes.append(self._send(cid, json.loads(it["body"]), it["strategy"], it["symbol"]))
                continue
            prev_fill = it["filled_qty"] or 0
            prev_avg = it["filled_avg_price"] or 0
            self._apply(cid, o)
            fq = float(o.get("filled_qty") or 0)
            if fq > prev_fill:
                self.j.event("BROKER_FILL", {"client_order_id": cid, "broker_order_id": o["id"], "filled_qty": fq,
                                             "delta_qty": fq - prev_fill, "filled_avg_price": o.get("filled_avg_price"),
                                             "status": o["status"], "side": o.get("side")},
                             it["strategy"], it["symbol"])
                mult = 100 if o.get("asset_class") == "us_option" else 1
                px = float(o.get("filled_avg_price") or it["limit_price"] or 0)
                if self.ledger:
                    gross = (fq * px - prev_fill * prev_avg) * mult
                    self.ledger.post(it["strategy"], it["symbol"], -gross if o.get("side") == "buy" else gross,
                                     f"fill_{o.get('side')}", f"{o['id']}-{fq}")
                self._reduce_notional(cid, (fq - prev_fill) * px * mult)
            if o["status"] in ("canceled", "expired", "rejected", "filled"):
                self._release_kind(cid, "ORDER_NOTIONAL", f"order_{o['status']}")
            if o["status"] in ("canceled", "expired", "rejected") and fq == 0:
                self.release(cid, f"order_{o['status']}_unfilled")
            changes.append({"client_order_id": cid, "status": o["status"], "filled_qty": fq})
        return changes

    def cancel_and_confirm(self, reader, broker_order_id, cid=None):
        """Cancel and confirm terminal status before any replacement. Returns final order or None if unconfirmed."""
        if not self.w:
            return None
        try:
            self.w.cancel(broker_order_id)
        except UncertainResponse:
            pass
        o = reader.order_by_client_id(cid) if cid else reader.order(broker_order_id)
        if o and o["status"] in ("canceled", "filled", "expired", "rejected"):
            return o  # fill/cash accounting happens only in reconcile()
        self.j.event("CANCEL_UNCONFIRMED", {"broker_order_id": broker_order_id, "client_order_id": cid})
        return None

    def _reduce_notional(self, cid, amount):
        with self.j.tx() as db:
            for r in db.execute("SELECT * FROM reservations WHERE client_order_id=? AND status='ACTIVE' "
                                "AND kind='ORDER_NOTIONAL'", (cid,)).fetchall():
                db.execute("UPDATE reservations SET amount=MAX(0, amount-?), updated_utc=? WHERE res_id=?",
                           (amount, iso(self.j.clock()), r["res_id"]))

    def _release_kind(self, cid, kind, reason):
        with self.j.tx() as db:
            n = db.execute("UPDATE reservations SET status='RELEASED', updated_utc=? WHERE client_order_id=? "
                           "AND kind=? AND status='ACTIVE'", (iso(self.j.clock()), cid, kind)).rowcount
        if n:
            self.j.event("RESERVATION_RELEASED", {"client_order_id": cid, "kind": kind, "reason": reason})

    def release_kind_for_symbol(self, ledger, kind, symbol, reason):
        with self.j.tx() as db:
            n = db.execute("UPDATE reservations SET status='RELEASED', updated_utc=? WHERE ledger=? AND kind=? "
                           "AND symbol=? AND status='ACTIVE'", (iso(self.j.clock()), ledger, kind, symbol)).rowcount
        if n:
            self.j.event("RESERVATION_RELEASED", {"ledger": ledger, "kind": kind, "symbol": symbol, "reason": reason})

    def add_reservation(self, ledger, kind, symbol, amount, ref):
        now = iso(self.j.clock())
        with self.j.tx() as db:
            db.execute("INSERT INTO reservations VALUES(?,?,?,?,?,?,?,?,?)",
                       (uuid.uuid4().hex, ledger, kind, symbol, amount, ref, "ACTIVE", now, now))
