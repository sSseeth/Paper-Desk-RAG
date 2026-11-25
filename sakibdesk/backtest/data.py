"""Split-adjusted daily bars for the backtest.

The simulator reads CSV files only. An optional fetcher can fill that cache
from the Alpaca market-data host. Credentials come from the secrets store.
They are never hardcoded and never written into the cache.
"""

import csv
import os
import re
from datetime import timezone

from sakibdesk.backtest.pricing import STOCK_HALF_SPREAD
from sakibdesk.calendar_util import NY
from sakibdesk.stock import parse_rfc3339

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.]{0,9}$")
COLUMNS = ("date", "open", "high", "low", "close", "volume")


class CredentialsMissing(RuntimeError):
    """Raised when a fetch was asked for and the secrets store has no key."""


class Bar:
    """One split-adjusted daily bar. `date` is the session date."""

    __slots__ = ("date", "o", "h", "l", "c", "v")

    def __init__(self, date, o, h, l, c, v):
        self.date = date
        self.o = o
        self.h = h
        self.l = l
        self.c = c
        self.v = v


class History:
    """Bars for one symbol, sorted by session date, one row per date."""

    def __init__(self, bars):
        ordered = sorted(bars, key=lambda b: b.date)
        dedup = {}
        for bar in ordered:
            dedup[bar.date] = bar
        self.bars = [dedup[d] for d in sorted(dedup)]
        self.dates = [b.date for b in self.bars]
        self.by_date = {b.date: b for b in self.bars}

    def on(self, day):
        return self.by_date.get(day)

    def slice(self, start, end_exclusive):
        import bisect
        i0 = bisect.bisect_left(self.dates, start)
        i1 = bisect.bisect_left(self.dates, end_exclusive)
        return self.bars[i0:i1]


def _check_symbol(symbol):
    if not _SYMBOL.match(symbol or ""):
        raise ValueError("symbol must be a ticker, not a path")
    return symbol


def cache_path(cache_dir, symbol):
    symbol = _check_symbol(symbol)
    return os.path.join(cache_dir, symbol + ".csv")


def bar_from_row(row):
    """Return a Bar, or None when the row is not a usable positive bar."""
    try:
        day = str(row["date"])[:10]
        o = float(row["open"])
        h = float(row["high"])
        l = float(row["low"])
        c = float(row["close"])
        raw_v = row.get("volume", 0) if isinstance(row, dict) else 0
        v = float(raw_v or 0)
    except (KeyError, TypeError, ValueError):
        return None
    if min(o, h, l, c) <= 0:
        return None
    # A crossed bar is repaired to contain the open and the close. The repair
    # is recorded by the wider high or low; it does not invent a trend.
    h = max(h, o, c)
    l = min(l, o, c)
    if len(day) != 10:
        return None
    return Bar(day, o, h, l, c, v)


def load_csv(path):
    """Load one cache file. Missing file returns an empty list."""
    if not path or not os.path.exists(path):
        return []
    out = []
    with open(path, newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            bar = bar_from_row(row)
            if bar is not None:
                out.append(bar)
    return out


def write_csv(path, bars):
    """Write a cache file. Creates the directory. Does not write secrets."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(COLUMNS)
        for bar in bars:
            writer.writerow([bar.date, bar.o, bar.h, bar.l, bar.c, int(bar.v)])


def load_cache(cache_dir, symbols):
    """Return ({symbol: History}, [missing symbols]).

    A symbol with no rows is missing. The caller decides whether that blocks
    the run. This function does not open a network connection.
    """
    found, missing = {}, []
    for symbol in symbols:
        bars = load_csv(cache_path(cache_dir, symbol))
        if not bars:
            missing.append(symbol)
            continue
        found[symbol] = History(bars)
    return found, missing


def session_date_of(timestamp):
    """Session date of an Alpaca bar timestamp, in America/New_York."""
    text = str(timestamp)
    if len(text) == 10:
        return text
    dt = parse_rfc3339(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(NY).date().isoformat()


def bars_from_alpaca(rows):
    """Convert Alpaca bar objects to Bars. Drops unusable rows."""
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or "t" not in row:
            continue
        try:
            day = session_date_of(row["t"])
        except (TypeError, ValueError):
            continue
        bar = bar_from_row({
            "date": day,
            "open": row.get("o"),
            "high": row.get("h"),
            "low": row.get("l"),
            "close": row.get("c"),
            "volume": row.get("v") or 0,
        })
        if bar is not None:
            out.append(bar)
    return out


def fetch_cache(root, symbols, start, end, cache_dir):
    """Fetch split-adjusted daily bars into the CSV cache.

    Uses the read-only Alpaca market-data bars endpoint through MarketData.
    No trading client is constructed and the account id is not read.

    Credentials are the main profile's key id and secret from secrets_store.get,
    the same read desk.py uses. On Windows that is Credential Manager first,
    then the user-only secrets file. A missing key raises CredentialsMissing
    before any request is made.

    Returns ({symbol: number of bars}, [symbols that came back empty]).
    """
    from sakibdesk.alpaca import MarketData
    from sakibdesk.config import Config
    from sakibdesk.secrets_store import alpaca_keys, get, redact

    symbols = [_check_symbol(s) for s in symbols]
    cfg = Config(root)
    # Same names as the main profile's market-data client. Not the account id.
    key_name, secret_name, _expected_name = alpaca_keys("main")
    key_id = get(key_name, root)
    secret = get(secret_name, root)
    if not key_id or not secret:
        raise CredentialsMissing(
            f"{key_name} and {secret_name} are required to fetch bars. "
            "They are read from the secrets store only.")
    feed = cfg.feeds["stock_feed"]
    option_feed = cfg.feeds["option_feed"]
    client = MarketData(key_id, secret, feed, option_feed)
    written, empty = {}, []
    try:
        for symbol in symbols:
            try:
                payload = client.daily_bars([symbol], start)
            except Exception as exc:
                raise RuntimeError(redact(str(exc))) from None
            rows = (payload or {}).get(symbol) or []
            bars = [b for b in bars_from_alpaca(rows) if start <= b.date <= end]
            if not bars:
                empty.append(symbol)
                continue
            write_csv(cache_path(cache_dir, symbol), bars)
            written[symbol] = len(bars)
    finally:
        # Drop the header values from this process's secret set by not logging
        # them. The client holds them only in memory.
        del key_id, secret
    return written, empty


def stock_quote(open_px, now):
    """Synthetic NBBO around the session open.

    The full width is two times STOCK_HALF_SPREAD. The timestamp is `now`,
    so the live quote-age check sees a fresh quote. This is not a historical
    NBBO.
    """
    bid = open_px * (1.0 - STOCK_HALF_SPREAD)
    ask = open_px * (1.0 + STOCK_HALF_SPREAD)
    stamp = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return bid, ask, (bid + ask) / 2.0, {"bp": bid, "ap": ask, "t": stamp}
