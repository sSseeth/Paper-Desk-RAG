"""European Black-Scholes estimates for wheel premiums.

Historical option chains are not used. The price is an estimate from realised
volatility. American early exercise is decided by the simulator, not by this
price. Nothing here is a broker quote.
"""

import math

# Model choices. They are not policy.json parameters and they are not fitted.
RV_LOOKBACK = 20
RISK_FREE = 0.04
DIVIDEND_YIELD = 0.0
# Full bid/ask width is 5% of the haircut mid, inside the wheel 10% cap.
ENTRY_HALF_SPREAD = 0.025
# Stock quote width when no historical NBBO exists. 10 bps is inside the
# stock book's 0.5% maximum spread.
STOCK_HALF_SPREAD = 0.0005
VOL_FLOOR = 0.01
VOL_CAP = 3.0


def norm_cdf(x):
    """Standard normal CDF via math.erf. No third-party stats package."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def year_fraction(today, expiry, intraday=False):
    """Year fraction from a session date to an expiry date.

    On the expiry session the open still has a half-day of time value.
    The close of that session is expiry and returns 0.
    """
    from datetime import date
    days = (date.fromisoformat(expiry) - date.fromisoformat(today)).days
    if days < 0:
        return 0.0
    if days == 0:
        return (0.5 / 365.0) if intraday else 0.0
    return days / 365.0


def realized_vol(closes, lookback=RV_LOOKBACK):
    """Annualised sample standard deviation of log returns.

    Uses the last `lookback` returns. Returns None when the history is too
    short or a price is not positive. The floor and cap keep the pricer
    defined; both are model choices, not policy parameters.
    """
    if closes is None or len(closes) < lookback + 1:
        return None
    window = list(closes[-(lookback + 1):])
    rets = []
    for prev, cur in zip(window, window[1:]):
        if prev <= 0 or cur <= 0:
            return None
        rets.append(math.log(cur / prev))
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((x - mean) ** 2 for x in rets) / (len(rets) - 1)
    if var < 0:
        return None
    vol = math.sqrt(var) * math.sqrt(252.0)
    if vol < VOL_FLOOR:
        return VOL_FLOOR
    if vol > VOL_CAP:
        return VOL_CAP
    return vol


def _intrinsic(kind, spot, strike):
    if kind == "call":
        value = max(0.0, spot - strike)
        if spot > strike:
            delta = 1.0
        elif spot < strike:
            delta = 0.0
        else:
            delta = 0.5
        return value, delta
    if kind == "put":
        value = max(0.0, strike - spot)
        if spot < strike:
            delta = -1.0
        elif spot > strike:
            delta = 0.0
        else:
            delta = -0.5
        return value, delta
    raise ValueError("kind must be call or put")


def bs_price(kind, spot, strike, t_years, sigma, r=RISK_FREE, q=DIVIDEND_YIELD):
    """Return (premium, delta) for a European call or put.

    At expiry, or with no volatility, the premium is intrinsic.
    """
    if kind not in ("call", "put"):
        raise ValueError("kind must be call or put")
    if spot <= 0 or strike <= 0 or sigma <= 0 or t_years <= 0:
        return _intrinsic(kind, spot, strike)
    sqrt_t = math.sqrt(t_years)
    d1 = (math.log(spot / strike) + (r - q + 0.5 * sigma * sigma) * t_years) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    disc_s = math.exp(-q * t_years)
    disc_k = math.exp(-r * t_years)
    if kind == "call":
        price = spot * disc_s * norm_cdf(d1) - strike * disc_k * norm_cdf(d2)
        delta = disc_s * norm_cdf(d1)
    else:
        price = strike * disc_k * norm_cdf(-d2) - spot * disc_s * norm_cdf(-d1)
        delta = disc_s * (norm_cdf(d1) - 1.0)
    if price < 0:
        price = 0.0
    return price, delta


def quoted(theo, iv_haircut, half_spread=ENTRY_HALF_SPREAD):
    """Bid and ask around a haircut theoretical mid.

    `iv_haircut` reduces the Black-Scholes premium before the spread. It is a
    penalty. The backtest does not assume implied vol is richer than realised
    vol. The spread stays symmetric around that lower mid so a default quote
    still passes the policy spread cap.
    """
    if theo <= 0 or iv_haircut < 0 or iv_haircut >= 1:
        return 0.0, 0.0
    mid = theo * (1.0 - iv_haircut)
    if mid <= 0:
        return 0.0, 0.0
    return mid * (1.0 - half_spread), mid * (1.0 + half_spread)


def early_exercise(kind, theo, spot, strike):
    """True when a positive intrinsic value is at least the European price.

    That is the usual signal that an American option would already have been
    exercised. A call with zero dividend yield rarely meets it. Expiry is
    settled by the simulator and does not use this helper.
    """
    if kind == "put":
        intrinsic = max(0.0, strike - spot)
    elif kind == "call":
        intrinsic = max(0.0, spot - strike)
    else:
        raise ValueError("kind must be call or put")
    return intrinsic > 0.0 and theo <= intrinsic + 1e-8
