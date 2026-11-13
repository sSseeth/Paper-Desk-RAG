"""Paper-account profiles.

`main` is the evaluation book. Its secret names and on-disk paths stay as they
are so existing credential-manager entries and journals keep working.

`mechanics` is a second Alpaca paper account used for lifecycle checks. It has
its own secrets, journal, lock, log, and reports directory so its orders and
ledgers cannot mix with the main book.
"""
import os

PROFILES = ("main", "mechanics")


def check_profile(account):
    """Raise ValueError when `account` is not a known profile."""
    if account not in PROFILES:
        raise ValueError(f"unknown account profile: {account}")
    return account


def journal_path(root, account):
    """SQLite journal for this profile. Main path is unchanged."""
    check_profile(account)
    if account == "main":
        return os.path.join(root, "data", "journal.sqlite3")
    return os.path.join(root, "data", "mechanics", "journal.sqlite3")


def lock_path(root, account):
    """Process lock. Mechanics does not take the main lock."""
    check_profile(account)
    if account == "main":
        return os.path.join(root, "data", "desk.lock")
    return os.path.join(root, "data", "mechanics", "desk.lock")


def reports_dir(root, account):
    """Local HTML/CSV/snapshot directory. Main stays `reports/`."""
    check_profile(account)
    if account == "main":
        return os.path.join(root, "reports")
    return os.path.join(root, "reports", "mechanics")


def log_path(root, account):
    """Redacted file log. Mechanics does not append to the main log."""
    check_profile(account)
    if account == "main":
        return os.path.join(root, "data", "desk.log")
    return os.path.join(root, "data", "mechanics", "desk.log")


def expected_ids_equal(left, right):
    """True when both expected ids are set and name the same account.

    Comparison matches `account_matches`: trimmed and case-insensitive. Empty
    values are not a match, so a profile that has not been configured yet does
    not collide with the other one.
    """
    if left is None or right is None:
        return False
    a = str(left).strip().lower()
    b = str(right).strip().lower()
    return bool(a) and a == b
