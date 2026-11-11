"""STOCK / WHEEL cash subledgers inside one Alpaca account, built only from broker fills and
reconciled broker activities (assignment/exercise/expiry). Premium = cash in + an offsetting liability."""
import json

MULT = 100


def option_symbol(sym):
    return len(sym) > 10 and sym[-9] in "CP"


class Ledger:
    def __init__(self, journal, policy):
        self.j, self.p = journal, policy

    def cash(self, ledger):
        cash = self.p["capital"]["stock" if ledger == "STOCK" else "wheel"]
        for e in self.j.db.execute("SELECT * FROM events WHERE kind IN ('LEDGER_CASHFLOW') AND strategy=?",
                                   (ledger,)):
            cash += json.loads(e["payload"])["amount"]
        return cash

    def free_cash(self, ledger, om):
        return self.cash(ledger) - om.reserved(ledger)

    def post(self, ledger, symbol, amount, reason, ref):
        """Idempotent by ref: the same broker fill/activity is never booked twice."""
        eid = f"CF-{ledger}-{ref}"
        self.j.event("LEDGER_CASHFLOW", {"amount": round(amount, 2), "reason": reason, "ref": ref},
                     ledger, symbol, event_id=eid)

    def post_fill(self, ledger, symbol, side, qty, price, is_option, ref):
        amt = qty * price * (MULT if is_option else 1)
        self.post(ledger, symbol, -amt if side == "buy" else amt, f"fill_{side}", ref)
