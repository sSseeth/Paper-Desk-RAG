"""Session timing from the broker calendar (named America/New_York zone; never fixed offsets)."""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


class Session:
    def __init__(self, day):  # day: Alpaca calendar entry {"date","open","close"}
        d = day["date"]
        self.date = d
        self.open = datetime.fromisoformat(f"{d}T{day['open']}").replace(tzinfo=NY).astimezone(timezone.utc)
        self.close = datetime.fromisoformat(f"{d}T{day['close']}").replace(tzinfo=NY).astimezone(timezone.utc)

    def in_window(self, now, start_min, end_min=None, stop_before_close_min=None):
        if not (self.open <= now < self.close):
            return False
        if now < self.open + timedelta(minutes=start_min):
            return False
        if end_min is not None and now > self.open + timedelta(minutes=end_min):
            return False
        if stop_before_close_min is not None and now > self.close - timedelta(minutes=stop_before_close_min):
            return False
        return True


def sessions(calendar):
    return [Session(d) for d in calendar]


def current_session(calendar, now):
    for s in sessions(calendar):
        if s.open <= now < s.close:
            return s
    return None


def sessions_between(calendar, d1, d2):
    """Number of trading sessions strictly after d1 up to and including d2 (ISO date strings)."""
    return sum(1 for d in calendar if d1 < d["date"] <= d2)


def session_offset(calendar, date, n):
    """Trading date n sessions from `date` (n<0: before). If `date` is not a session, counts from the
    next session for n>0 and the previous for n<0."""
    import bisect
    dates = sorted(d["date"] for d in calendar)
    i = bisect.bisect_left(dates, date)
    if n >= 0:
        j = i + n if (i < len(dates) and dates[i] == date) else i + n - 1
    else:
        j = i + n
    return dates[j] if 0 <= j < len(dates) else None
