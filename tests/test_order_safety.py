"""restart-after-submit, uncertain status, duplicate scheduling, DRY_RUN by construction, process lock."""
import pytest
from conftest import FakeBroker, ReaderView
from sakibdesk.orders import OrderManager, client_order_id
from sakibdesk.ledger import Ledger
from sakibdesk.lock import ProcessLock, AlreadyRunning
from sakibdesk import cycle as C

BODY = {"symbol": "MU", "qty": "10", "side": "buy", "type": "limit", "time_in_force": "day", "limit_price": "100"}


def place(om, key="sig1"):
    return om.place(strategy="STOCK", symbol="MU", purpose="ENTRY", signal_key=key, body=BODY,
                    reservations=[("STOCK", "ORDER_NOTIONAL", 1000.0)])


def test_client_order_id_deterministic_and_short(cfg):
    a = client_order_id(cfg, "STOCK", "MU", "k", "ENTRY")
    assert a == client_order_id(cfg, "STOCK", "MU", "k", "ENTRY")
    assert a != client_order_id(cfg, "STOCK", "MU", "k", "ENTRY", attempt=1)
    assert len(a) <= 128


def test_intent_persisted_before_submit(cfg, journal, broker):
    seen = {}
    orig = broker.submit
    def spy(body):
        row = journal.db.execute("SELECT status FROM intents WHERE client_order_id=?", (body["client_order_id"],)).fetchone()
        res = journal.db.execute("SELECT COUNT(*) n FROM reservations WHERE client_order_id=?", (body["client_order_id"],)).fetchone()
        seen.update(status=row and row["status"], res=res["n"])
        return orig(body)
    broker.submit = spy
    place(OrderManager(journal, cfg, writer=broker))
    assert seen == {"status": "PERSISTED", "res": 1}


def test_restart_after_ambiguous_submit_creates_no_second_order(cfg, journal, broker):
    broker.fail_next_submit_uncertain_after_accept = True
    om = OrderManager(journal, cfg, writer=broker)
    r = place(om)
    assert r["status"] == "UNKNOWN"
    assert om.reserved("STOCK") == 1000.0                    # uncertain => resources stay locked
    om2 = OrderManager(journal, cfg, writer=broker)           # "restart"
    om2.reconcile(ReaderView(broker))
    assert place(om2)["status"] == "DUPLICATE"               # scheduler fires the same signal again
    assert len(broker.submits) == 1
    assert om2.intent(r["client_order_id"])["status"] == "accepted"


def test_unknown_not_found_resends_same_id_once(cfg, journal, broker):
    broker.fail_next_submit_uncertain_before_accept = True
    om = OrderManager(journal, cfg, writer=broker)
    r = place(om)
    assert r["status"] == "UNKNOWN" and om.reserved("STOCK") == 1000.0
    om.reconcile(ReaderView(broker))
    assert [b["client_order_id"] for b in broker.submits] == [r["client_order_id"]] * 2
    om.reconcile(ReaderView(broker))
    assert len(broker.submits) == 2


def test_duplicate_scheduling(cfg, journal, broker):
    om = OrderManager(journal, cfg, writer=broker)
    assert place(om)["status"] == "accepted"
    assert place(om)["status"] == "DUPLICATE"
    assert len(broker.submits) == 1


def test_process_lock_blocks_second_instance(tmp_path):
    p = str(tmp_path / "x.lock")
    with ProcessLock(p):
        with pytest.raises(AlreadyRunning):
            with ProcessLock(p):
                pass


def test_dry_run_cannot_submit_by_construction(cfg, journal, root):
    om = OrderManager(journal, cfg, writer=None)
    r = place(om)
    assert r["status"] == "DRY_RUN" and om.w is None
    assert om.reserved("STOCK") == 0
    with pytest.raises(RuntimeError):
        C.run_cycle(cfg, journal, "DRY_RUN", C.Deps(writer=object()), root)


def test_dry_run_build_deps_has_no_writer(monkeypatch):
    import desk
    from sakibdesk import secrets_store as SS
    monkeypatch.setattr(SS, "missing", lambda root: [])
    monkeypatch.setattr(desk, "_secret", lambda n: "https://x" if n == "GROK_WEBHOOK_URL" else "v")
    d = desk.build_deps(desk._cfg(), "DRY_RUN")
    assert d.writer is None and not hasattr(d.reader, "submit")


def test_rejected_releases_but_unknown_does_not(cfg, journal, broker):
    from sakibdesk.alpaca import BrokerError
    om = OrderManager(journal, cfg, writer=broker)
    broker.submit = lambda b: (_ for _ in ()).throw(BrokerError("422"))
    place(om, "k2")
    assert om.reserved("STOCK") == 0


def test_cancel_requires_confirmation(cfg, journal, broker):
    om = OrderManager(journal, cfg, writer=broker)
    r = place(om)
    broker.cancel = lambda oid: None        # broker never confirms
    assert om.cancel_and_confirm(ReaderView(broker), "OID-1", r["client_order_id"]) is None
    assert journal.events("CANCEL_UNCONFIRMED")
