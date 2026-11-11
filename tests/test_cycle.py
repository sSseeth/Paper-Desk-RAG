"""End-to-end cycle with mocked broker: no-broker, unsigned schema, DRY_RUN with data, account mismatch, risk block."""
from conftest import ReaderView, make_root
from sakibdesk import cycle as C
from sakibdesk.config import Config
from sakibdesk.journal import Journal


def test_no_broker_records_no_trade(cfg, journal, root):
    s = C.run_cycle(cfg, journal, "DRY_RUN", C.Deps(), root)
    assert any(b.startswith("NO_BROKER_CONNECTION") for b in s["entry_blocks"]) and journal.events("NO_TRADE")


def test_unsigned_schema_blocks_entries(tmp_path, broker, clock):
    root = make_root(tmp_path, signed=False)
    cfg = Config(root)
    j = Journal(str(tmp_path / "j.db"), cfg, clock=clock)
    s = C.run_cycle(cfg, j, "AUTO_PAPER", C.Deps(reader=ReaderView(broker), writer=broker, md=broker,
                                                 expected_account_id="ACC-1"), root)
    assert any("SCHEMA_NOT_SIGNED" in b for b in s["entry_blocks"]) and not broker.submits


def test_dry_run_with_data_never_submits(cfg, journal, root, broker):
    s = C.run_cycle(cfg, journal, "DRY_RUN", C.Deps(reader=ReaderView(broker), md=broker,
                                                    expected_account_id="ACC-1"), root)
    assert not broker.submits and s["mode"] == "DRY_RUN"


def test_account_mismatch_blocks(cfg, journal, root, broker):
    s = C.run_cycle(cfg, journal, "AUTO_PAPER", C.Deps(reader=ReaderView(broker), writer=broker, md=broker,
                                                       expected_account_id="OTHER"), root)
    assert "ACCOUNT_ID_MISMATCH" in s["entry_blocks"] and not broker.submits


def test_daily_loss_blocks_new_entries(cfg, journal, root, broker):
    broker.acct.update(equity="97000", last_equity="100000")
    s = C.run_cycle(cfg, journal, "AUTO_PAPER", C.Deps(reader=ReaderView(broker), writer=broker, md=broker,
                                                       expected_account_id="ACC-1"), root)
    assert any("DAILY_LOSS_2PCT" in b for b in s["entry_blocks"]) and not broker.submits


def test_stale_research_blocks_stock_entries(cfg, journal, root, broker):
    s = C.run_cycle(cfg, journal, "AUTO_PAPER", C.Deps(reader=ReaderView(broker), writer=broker, md=broker,
                                                       expected_account_id="ACC-1"), root)
    assert not [b for b in broker.submits if b.get("position_intent") in ("buy_to_open", "sell_to_open")]
