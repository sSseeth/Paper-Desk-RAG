"""Policy/watchlist loading, validation and config hashing. Invalid config is rejected."""
import hashlib, json, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PAPER_TRADING_HOST = "https://paper-api.alpaca.markets"   # hard-coded; no override exists
DATA_HOST = "https://data.alpaca.markets"
FEED_PROFILES = ("FREE_MECHANICS", "EVALUATION")
WHEEL_UNIVERSE = ("MU", "AME", "AMD", "TSM", "NVDA", "AVGO", "ANET", "VRT")


class ConfigError(Exception):
    pass


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()


def _req(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            raise ConfigError(f"missing config key {path}")
        cur = cur[p]
    return cur


def validate_policy(p):
    for k in ("strategy_version", "experiment_id", "feed_profile", "capital.initial", "capital.stock",
              "capital.wheel", "stock", "wheel", "orders", "risk", "costs", "research", "data_settings"):
        _req(p, k)
    if p["feed_profile"] not in FEED_PROFILES:
        raise ConfigError("feed_profile must be chosen explicitly: FREE_MECHANICS or EVALUATION")
    c = p["capital"]
    if c["initial"] != 100000.0 or c["stock"] + c["wheel"] != c["initial"] or c["stock"] != 20000.0:
        raise ConfigError("capital must be 100000 split 20000 STOCK / 80000 WHEEL (guide p6)")
    s, w, r = p["stock"], p["wheel"], p["risk"]
    fixed = {("stock", "max_pct_per_company"): 0.05, ("stock", "max_open_or_pending_names"): 3,
             ("stock", "max_new_entries_per_session"): 2, ("stock", "risk_pct_per_entry"): 0.0025,
             ("stock", "min_valid_bars"): 220, ("wheel", "max_active_cycles"): 2,
             ("wheel", "max_pct_per_name"): 0.20, ("wheel", "max_pct_combined"): 0.40,
             ("risk", "daily_loss_pct"): 0.02, ("risk", "experiment_drawdown_pct"): 0.10}
    for (sec, k), v in fixed.items():
        if p[sec].get(k) != v:
            raise ConfigError(f"{sec}.{k} must equal guide value {v} (limits cannot be loosened)")
    if tuple(sorted(w["universe"])) != tuple(sorted(WHEEL_UNIVERSE)):
        raise ConfigError("wheel.universe must be the 8 guide names")
    if not (isinstance(w.get("put_reserve_cost_allowance_per_contract"), (int, float))
            and w["put_reserve_cost_allowance_per_contract"] >= 0):
        raise ConfigError("wheel.put_reserve_cost_allowance_per_contract must be a non-negative number")
    if p["research"].get("poll_min_interval_sec", 0) < 900:
        raise ConfigError("research poll interval must be >= 900s")
    for forbidden in ("trading_host", "base_url", "live", "mode_live", "host_override"):
        if forbidden in json.dumps(p).lower() and f'"{forbidden}"' in json.dumps(p).lower():
            raise ConfigError(f"forbidden key {forbidden}: no host override / live mode")
    if p["data_settings"].get("bar_adjustment") != "split":
        raise ConfigError("bars must be split-adjusted (guide p7)")
    return p


def validate_watchlist(wl):
    names = wl["names"]
    if len(names) != 25:
        raise ConfigError("watchlist must have 25 names")
    if sum(n["stock_eligible"] for n in names) != 19:
        raise ConfigError("exactly 19 US stock-eligible names required")
    if sum(n["alerts_only"] for n in names) != 6:
        raise ConfigError("exactly 6 alerts-only names required")
    wheel = sorted(n["symbol"] for n in names if n["wheel_eligible"])
    if tuple(wheel) != tuple(sorted(WHEEL_UNIVERSE)):
        raise ConfigError("wheel-eligible names must equal the 8 guide names")
    for n in names:
        if n["wheel_eligible"] and not n["stock_eligible"]:
            raise ConfigError("wheel name must be a US listing")
    return wl


class Config:
    def __init__(self, root=ROOT):
        self.root = root
        self.policy_path = os.path.join(root, "policy.json")
        self.watchlist_path = os.path.join(root, "watchlist.json")
        with open(self.policy_path, "rb") as f:
            raw_p = f.read()
        with open(self.watchlist_path, "rb") as f:
            raw_w = f.read()
        try:
            self.policy = validate_policy(json.loads(raw_p))
            self.watchlist = validate_watchlist(json.loads(raw_w))
        except (ValueError, KeyError, TypeError) as e:
            raise ConfigError(f"invalid config: {e}")
        self.config_hash = hashlib.sha256(raw_p + b"\n" + raw_w).hexdigest()[:16]
        self.strategy_version = self.policy["strategy_version"]
        self.feed_profile = self.policy["feed_profile"]

    @property
    def us_symbols(self):
        return [n["symbol"] for n in self.watchlist["names"] if n["stock_eligible"]]

    @property
    def wheel_symbols(self):
        return [n["symbol"] for n in self.watchlist["names"] if n["wheel_eligible"]]

    @property
    def feeds(self):
        return self.policy["data_settings"][self.feed_profile]
