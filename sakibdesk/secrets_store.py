"""Secrets: OS keyring first, else a protected local file (0600 / user-only ACL). Never args, source or logs."""
import getpass, json, logging, os, re, stat, sys
from .account_profile import log_path

SERVICE = "SakibPaperDesk"
# Main Alpaca names are stable. Existing credential-manager entries must keep working.
MAIN_ALPACA_KEYS = ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET", "ALPACA_EXPECTED_ACCOUNT_ID")
MECH_ALPACA_KEYS = ("MECH_ALPACA_PAPER_KEY_ID", "MECH_ALPACA_PAPER_SECRET", "MECH_ALPACA_EXPECTED_ACCOUNT_ID")
KEYS = MAIN_ALPACA_KEYS + ("GITHUB_READONLY_TOKEN", "GITHUB_RESEARCH_REPO", "GROK_WEBHOOK_URL", "GROK_WEBHOOK_BEARER")
SECRET_KEYS = ("ALPACA_PAPER_KEY_ID", "ALPACA_PAPER_SECRET", "GITHUB_READONLY_TOKEN", "GROK_WEBHOOK_BEARER",
               "GROK_WEBHOOK_URL", "MECH_ALPACA_PAPER_KEY_ID", "MECH_ALPACA_PAPER_SECRET")
_known_values = set()
_PROFILES = ("main", "mechanics")


def alpaca_keys(account):
    """Broker secret names for this profile. Main names are the historical ones."""
    if account == "main":
        return MAIN_ALPACA_KEYS
    if account == "mechanics":
        return MECH_ALPACA_KEYS
    raise ValueError(f"unknown account profile: {account}")


def profile_keys(account):
    """Names setup and doctor require. Mechanics is only the three broker secrets."""
    if account == "main":
        return KEYS
    if account == "mechanics":
        return MECH_ALPACA_KEYS
    raise ValueError(f"unknown account profile: {account}")


def _file_path(root):
    return os.path.join(root, "data", "secrets.local.json")


def _kr():
    try:
        import keyring
        kr = keyring.get_keyring()
        if "fail" in type(kr).__module__.lower() or "null" in type(kr).__name__.lower():
            return None
        return keyring
    except Exception:
        return None


def get(name, root):
    v = None
    kr = _kr()
    if kr:
        try:
            v = kr.get_password(SERVICE, name)
        except Exception:
            v = None
    if not v:
        p = _file_path(root)
        if os.path.exists(p):
            if os.name != "nt" and (os.stat(p).st_mode & (stat.S_IRWXG | stat.S_IRWXO)):
                raise PermissionError("secrets.local.json must be user-only (chmod 600)")
            with open(p) as f:
                v = json.load(f).get(name)
    if v and name in SECRET_KEYS:
        _known_values.add(v)
    return v


def missing_names(root, names):
    out = []
    for k in names:
        try:
            if not get(k, root):
                out.append(k)
        except PermissionError as e:
            out.append(f"{k} ({e})")
    return out


def missing(root):
    """Main-profile names. Callers pass only `root`; that signature stays."""
    return missing_names(root, KEYS)


def setup_interactive(root, account="main"):
    if account not in _PROFILES:
        raise ValueError(f"unknown account profile: {account}")
    kr = _kr()
    store = {}
    names = profile_keys(account)
    # Mechanics prompts are all hidden. Main still shows the expected-account prompt.
    hidden = set(names) if account == "mechanics" else set(SECRET_KEYS)
    if account == "mechanics":
        print("Mechanics paper account. Every prompt is hidden and is not echoed.")
    print("Secrets are typed into hidden prompts; leave blank to keep existing value.")
    for k in names:
        if k in hidden:
            v = getpass.getpass(f"{k}: ")
            if k.endswith("EXPECTED_ACCOUNT_ID"):
                v = v.strip()
        else:
            v = input(f"{k}: ").strip()
        if not v:
            continue
        if kr:
            kr.set_password(SERVICE, k, v)
        else:
            store[k] = v
    if store:
        p = _file_path(root)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        old = {}
        if os.path.exists(p):
            with open(p) as f:
                old = json.load(f)
        old.update(store)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(old, f)
        if os.name == "nt":
            os.system(f'icacls "{p}" /inheritance:r /grant:r "%USERNAME%":F >NUL')
        print(f"No OS keyring backend; stored in protected file {p}")
    else:
        print("Stored in OS keyring." if kr else "Nothing stored.")


def redact(text):
    s = str(text)
    for v in list(_known_values):
        if v and len(v) >= 4:
            s = s.replace(v, "***REDACTED***")
    s = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+", r"\1***REDACTED***", s)
    s = re.sub(r"(?i)(APCA-API-(KEY-ID|SECRET-KEY)['\"]?\s*[:=]\s*['\"]?)[^'\",\s]+", r"\1***REDACTED***", s)
    s = re.sub(r"\b(gh[pousr]_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})\b", "***REDACTED***", s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record):
        record.msg = redact(record.getMessage())
        record.args = ()
        return True


def setup_logging(root, account="main"):
    path = log_path(root, account)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    log = logging.getLogger("spd." + account)
    if not log.handlers:
        h = logging.FileHandler(path, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        h.addFilter(RedactingFilter())
        log.addHandler(h)
        log.setLevel(logging.INFO)
        log.propagate = False
    return log
