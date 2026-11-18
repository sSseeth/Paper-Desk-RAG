"""Symbols and size caps for the mechanics account only.

Not loaded by Config, and not part of the config hash. policy.json limits stay
as they are. The live suite still refuses a quote that has left the band.
"""

# Ford Motor: liquid US equity and optionable, usually inside $5-$30.
STOCK_SYMBOL = "F"
STOCK_MIN_PRICE = 5.0
STOCK_MAX_PRICE = 30.0
STOCK_ENTRY_QTY = 1
STOCK_PARTIAL_QTY = 2
# Hard cap if a 2-share probe and the $30 band were both filled. The planned
# round trip is 1 share. The suite refuses an order above this notional.
STOCK_MAX_NOTIONAL = STOCK_PARTIAL_QTY * STOCK_MAX_PRICE

# Same underlying for the wheel suites. Collateral is strike*100 plus the
# policy allowance, and the selector also requires that sum to fit broker cash.
WHEEL_UNDERLYING = "F"
WHEEL_CONTRACTS = 1
WHEEL_MAX_STRIKE = 30.0

# Nearest OTM strike at or below this fraction of the bid. Farther strikes on
# a low-priced name often have a zero bid on the indicative feed, so the
# mechanics selector stops at 90% of spot and keeps the first positive bid.
FAR_OTM_FRACTION = 0.90
ITM_FRACTION = 1.01
MIN_DTE = 1
SELL_BUYBACK_MAX_DTE = 21
EXPIRY_MAX_DTE = 10

# Indicative option quotes are not OPRA. A far put's bid can sit above the
# price the paper broker will sell, and the ask can sit below the price it
# will buy. The sell walks down to this premium per share.
SELL_PUT_FLOOR = 0.01
# Hard cap for a buy-to-close, premium per share. One contract costs
# BUYBACK_MAX_PRICE * 100 dollars. The walk never bids above this.
BUYBACK_MAX_PRICE = 0.25
# Polls of one resting limit before cancel-and-replace, and the seconds
# between those polls. Giving up at 20 seconds left the indicative bid
# working with status `new`.
PRICE_WALK_ATTEMPTS = 30
PRICE_WALK_INTERVAL_SEC = 1.0
# At most three limits: the quote, half the bid or double the ask, then
# the floor or the cap.
PRICE_WALK_MAX_STEPS = 3

OBSERVED_ASSIGNMENT = "OBSERVED_PAPER_ASSIGNMENT"
MOCK_EVIDENCE = "MOCK"
SERIES = "mechanics-test"
TRACK_KEY = "mech_wheel_expiry_v1"

# Regular US session while the US is on Eastern Standard Time.
SYDNEY_HOURS = "1:30 AM-8:00 AM Australia/Sydney AEDT"
NY_HOURS = "09:30-16:00 America/New_York"
