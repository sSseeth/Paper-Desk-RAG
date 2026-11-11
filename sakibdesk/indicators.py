"""Indicators on completed daily bars (list of dicts with o,h,l,c,t)."""


def sma(vals, n):
    return sum(vals[-n:]) / n if len(vals) >= n else None


def atr_wilder(bars, n=14):
    if len(bars) < n + 1:
        return None
    trs = [max(b["h"] - b["l"], abs(b["h"] - p["c"]), abs(b["l"] - p["c"])) for p, b in zip(bars, bars[1:])]
    atr = sum(trs[:n]) / n
    for tr in trs[n:]:
        atr = (atr * (n - 1) + tr) / n
    return atr


def pct_return(closes, n):
    if len(closes) < n + 1 or closes[-n - 1] <= 0:
        return None
    return closes[-1] / closes[-n - 1] - 1
