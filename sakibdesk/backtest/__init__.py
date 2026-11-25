"""Historical backtest of the shipped stock and wheel books.

The simulator calls the live pure functions. It does not place orders and it
does not write the paper journal or the paper performance series. Wheel
premiums are estimates.
"""

from sakibdesk.backtest.engine import run_backtest

__all__ = ["run_backtest"]
