"""Daily 2% and experiment 10% loss controls; pause state; recovery counter."""


class RiskState:
    def __init__(self, journal, policy):
        self.j, self.p = journal, policy

    def update(self, equity, session_date, prior_close_equity):
        hi = max(self.j.get("equity_high", 0.0) or 0.0, equity)
        self.j.set("equity_high", hi)
        blocks = []
        if prior_close_equity and equity <= prior_close_equity * (1 - self.p["risk"]["daily_loss_pct"]):
            if self.j.get("daily_block_session") != session_date:
                self.j.set("daily_block_session", session_date)
                self.j.event("RISK_DAILY_LOSS_BLOCK", {"equity": equity, "prior_close": prior_close_equity})
        if self.j.get("daily_block_session") == session_date:
            blocks.append("DAILY_LOSS_2PCT")
        if equity <= hi * (1 - self.p["risk"]["experiment_drawdown_pct"]):
            if not self.j.get("experiment_block"):
                self.j.set("experiment_block", True)
                self.j.event("RISK_EXPERIMENT_DRAWDOWN_BLOCK", {"equity": equity, "high": hi})
        if self.j.get("experiment_block"):
            blocks.append("EXPERIMENT_DRAWDOWN_10PCT")
        if self.j.get("paused"):
            blocks.append("PAUSED")
        return blocks

    def healthy(self, ok):
        n = (self.j.get("healthy_streak", 0) + 1) if ok else 0
        self.j.set("healthy_streak", n)
        return n >= self.p["orders"]["healthy_reconciliations_to_resume"]
