# Paper Desk RAG

[![tests](https://github.com/sSseeth/Paper-Desk-RAG/actions/workflows/tests.yml/badge.svg)](https://github.com/sSseeth/Paper-Desk-RAG/actions/workflows/tests.yml)

[WIP] Simple code you can write and add within an AI RAG to do paper trading for you on Alpaca

## Paper only

This is a paper-trading runner. The trading host is hard-coded to `https://paper-api.alpaca.markets` and market data uses `https://data.alpaca.markets`. There is no live mode, no host override, and no switch that points the client at the live API. Plain HTTP is rejected, and redirects are refused.

It is not financial advice. Paper fills are simulated by the broker. Nothing here is a recommendation to buy or sell any security, and paper results are not a forecast of live results. The software is provided under the MIT License, without warranty. See [License](#license).

## What it does

One Alpaca paper account holds two books with separate cash ledgers:

- **Stock.** US names marked `stock_eligible` on `watchlist.json`. A new entry needs a usable research record, an open session inside the configured window, a completed-bar breakout with the close above the 50- and 200-day averages, and a passing SPY filter (close above its 200-day average). Size, open-name, and per-session entry caps come from `policy.json`.
- **Wheel.** The eight names in `policy.json` (`MU`, `AME`, `AMD`, `TSM`, `NVDA`, `AVGO`, `ANET`, `VRT`). The book sells cash-secured puts and, after assignment, covered calls, and it tracks each name through an explicit state machine. Collateral is reserved on the wheel ledger. Broker margin buying power is never treated as cash.

The shipped policy splits a 100,000 paper book into 20,000 stock and 80,000 wheel. The loader rejects a different split and rejects loosened risk limits.

An optional research feed can be read from a GitHub repository (`research/*.json` only). Every file is validated against the pinned schema before any new exposure. A missing, stale, or unverified record blocks that symbol. When the broker connection and account check have already succeeded, exit management still runs.

After a cycle or a manual report, the runner writes a local HTML report and a CSV event log, and it can POST a summary to an HTTPS webhook. The webhook is outbound only: the response body is ignored. Pending deliveries sit in the journal outbox and are retried with an idempotency key, exponential backoff, and a cap of 12 attempts.

The journal is SQLite (`data/journal.sqlite3`, WAL). The `events` table is append-only: triggers abort `UPDATE` and `DELETE`. Timestamps are stored in UTC and Australia/Sydney. Orders, reservations, wheel cycles, and the outbox live in separate working tables.

`feed_profile` in `policy.json` must be `FREE_MECHANICS` (IEX stock feed, indicative option quotes, labelled INDICATIVE on reports) or `EVALUATION` (SIP and OPRA). There is no silent fallback. An entitlement error blocks; it does not switch profile.

## Safety checks

A new stock entry and a new wheel put on the same symbol are refused. The check runs at the moment of the order and reads both books: filled positions (the stock position table, broker share and option positions, and active wheel cycles, including shares assigned to a cycle), broker orders in any non-terminal status, runner intents the broker has not acknowledged (including `UNKNOWN` after a timeout), and active reserves (wheel put collateral and the stock book's order reserve). The journal records `CROSS_BOOK_OVERLAP:STOCK` or `CROSS_BOOK_OVERLAP:WHEEL`. When the position or open-order read fails, the new entry is skipped and recorded as `CROSS_BOOK_OVERLAP:UNKNOWN`. In one cycle the stock book submits first; a wheel put later in that pass sees the stock intent and skips. A covered call on shares already held by that same wheel cycle stays allowed. Same-book caps and exclusions are unchanged.

## Requirements

- Python 3.11 or newer
- The packages in `requirements.txt` (`keyring`, `jsonschema`, `pytest`, `requests`, `tzdata`)
- An Alpaca paper account
- An OS keyring (Windows Credential Manager, macOS Keychain, or a Secret Service backend)
- For the research feed: a GitHub token limited to reading one repository, and that repository's `owner/name`
- For notifications: an HTTPS webhook URL and a bearer token

`tzdata` supplies the IANA timezone database on Windows, which the journal needs for Australia/Sydney timestamps.

## Setup

Clone `https://github.com/sSseeth/Paper-Desk-RAG.git` and change into that directory. Create a virtual environment with Python 3.11 or newer and install `requirements.txt`. On Windows use `py -m venv .venv` and `.\.venv\Scripts\python.exe -m pip install -r requirements.txt`. On Linux or macOS use `python3 -m venv .venv` and `./.venv/bin/python -m pip install -r requirements.txt`. Run only one machine as the order writer.

On Windows, from PowerShell, every session must activate that environment with `.\.venv\Scripts\Activate.ps1` before `py desk.py ...`:

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py setup-secrets
py desk.py self-test
py desk.py doctor
py desk.py cycle --mode DRY_RUN
```

Troubleshooting: without `.\.venv\Scripts\Activate.ps1` in that PowerShell session, `keyring` is not importable and `doctor` reports every secret as missing even though Credential Manager has them.

A hash mismatch in `doctor` after cloning on Windows (`core.autocrlf=true` turning `schemas/news_schema_v1.7.json`, `policy.json`, and `watchlist.json` into CRLF) is fixed by `git config core.autocrlf false; git rm --cached -r -q .; git reset --hard`. `.gitattributes` now prevents that conversion on fresh clones.

## Secrets

`desk.py setup-secrets` prompts for each value and stores it in the OS keyring under the service name `SakibPaperDesk`. Secret values use a hidden prompt. They are never taken from command-line arguments, source files, or logs. Do not put them in the repository, in a `.env` file, or in a shell history.

If the machine has no keyring backend, the command falls back to `data/secrets.local.json` with mode `0600` (and a user-only ACL on Windows). That file is gitignored. Prefer a real keyring so the values stay out of the tree. A file that is readable by group or other is rejected.

`doctor` on the main account expects all seven names to be present:

| Name | Prompt | Purpose |
| --- | --- | --- |
| `ALPACA_PAPER_KEY_ID` | hidden | Paper API key id |
| `ALPACA_PAPER_SECRET` | hidden | Paper API secret |
| `ALPACA_EXPECTED_ACCOUNT_ID` | visible | Paper account UUID or `PA…` account number. Exact match after trimming, case-insensitive. |
| `GITHUB_READONLY_TOKEN` | hidden | Token used only to read `research/*.json` |
| `GITHUB_RESEARCH_REPO` | visible | `owner/name` of the research repository, for example `<owner>/<research-repo>` |
| `GROK_WEBHOOK_URL` | hidden | HTTPS endpoint for outbound summaries |
| `GROK_WEBHOOK_BEARER` | hidden | Bearer token sent with those POSTs |

Leave a prompt blank to keep the value already stored.

## Mechanics account

Stock and Wheel lifecycle checks run on a second Alpaca paper account, not on the main evaluation account. Create that paper account, then store its key id, secret, and expected account id under separate names in the same `SakibPaperDesk` keyring service. `--account` defaults to `main`. `main` keeps `ALPACA_PAPER_KEY_ID`, `ALPACA_PAPER_SECRET`, and `ALPACA_EXPECTED_ACCOUNT_ID`.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py setup-secrets --account mechanics
py desk.py doctor --account mechanics
```

`setup-secrets --account mechanics` prompts only for these names, and every prompt is hidden (nothing is echoed):

| Name | Purpose |
| --- | --- |
| `MECH_ALPACA_PAPER_KEY_ID` | Mechanics paper API key id |
| `MECH_ALPACA_PAPER_SECRET` | Mechanics paper API secret |
| `MECH_ALPACA_EXPECTED_ACCOUNT_ID` | Mechanics account UUID or `PA…` account number |

`doctor --account mechanics` checks those three broker secrets, the hard-coded paper host, and that the broker account matches the mechanics expected id. It does not require the main account's secrets. The command refuses when the mechanics expected account id equals the main expected account id, and when the account the broker returns does not match the selected profile.

Mechanics orders, the journal, and reports stay apart from the main book: `data/mechanics/journal.sqlite3` and `reports/mechanics/`. The main journal stays `data/journal.sqlite3` and main reports stay in `reports/`. The paper host stays `https://paper-api.alpaca.markets`. There is no live endpoint.

After a mechanics suite passes, record the check it proved. `lifecycle-record` refuses `--account main`. It reads the mechanics account and does not submit or cancel orders. Do not put the account id in `--evidence`. `wheel` is accepted only when the evidence contains `OBSERVED_PAPER_ASSIGNMENT` and does not contain `MOCK`.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py lifecycle-record --account mechanics --check stock --evidence "stock lifecycle run <run-id>"
py desk.py lifecycle-record --account mechanics --check wheel-sell-buyback --evidence "wheel sell and buyback run <run-id>"
py desk.py lifecycle-record --account mechanics --check wheel --evidence "OBSERVED_PAPER_ASSIGNMENT run <run-id>"
```

The record is `state/lifecycle_gate.json` (gitignored). `stock_lifecycle`, `wheel_sell_buyback`, and `wheel_lifecycle` each store the UTC time, a 16-character sha256 prefix of the mechanics expected account id, the runner git commit, the config hash, and the evidence note. The raw account id is not written. A missing or corrupt file is not a pass.

`cycle --mode AUTO_PAPER` on the main account (the default, including `run-paper.cmd` and `run-paper.sh`) refuses with journal event `AUTO_PAPER_REFUSED` and reason `LIFECYCLE_GATE_NOT_PASSED` until `stock_lifecycle` is recorded. With that check recorded, the stock book may enter. Wheel entries stay blocked until `wheel_sell_buyback` is recorded and the mock wheel state paths pass. Until `wheel_lifecycle` is recorded from an observed paper assignment, those wheel entries are labelled `PROVISIONAL` on the journal, the report, and `doctor`. Do not call that book fully verified. `cycle --mode AUTO_PAPER --account mechanics` is not gated. `DRY_RUN` is never gated. `doctor` on main prints each check. `BLOCKED` and `PROVISIONAL` do not by themselves fail doctor. A missing or corrupt gate file still refuses main `AUTO_PAPER`.

## Mechanics lifecycle tests

These commands trade only on the mechanics paper account. `--account main` is refused before a client is built. They use `F` (Ford), a liquid name that is usually inside $5–$30, from mechanics-only settings. They do not change `policy.json`, the config hash, or the main journal. Mechanics fills are not part of the main performance or SPY benchmark series.

The stock round trip buys 1 share. A partial-fill probe rests 2 shares. The suite refuses an ask outside $5–$30 and any order above a $60 notional cap. The wheel suites sell 1 contract. The strike is at most $30, and `strike * 100` plus the $3 reserve allowance must fit mechanics cash, so collateral is at most $3,003 and never more than the cash in that account.

Run them only while the Alpaca clock says the regular US session is open (09:30–16:00 America/New_York). In Australia/Sydney that is currently 1:30 AM–8:00 AM AEDT. Outside that window the command refuses and sends nothing. `wheel-expiry --reconcile` may be run later in the day to read assignment activity; selling the covered call still waits for the regular session.

The stock suite refuses if `F` already has a position or an open order, so it will not sell shares held for the expiry track.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py mechanics-test --account mechanics --suite stock --record
```

On PASS, `--record` writes `stock_lifecycle`. Without `--record` the same command prints the `lifecycle-record` line, including the mechanics journal run id. The evidence says whether partial-fill protection was `BROKER` or `MOCK`. A far limit that never partial-fills is still a pass when the runner's reservation and duplicate-order checks pass on a scratch book, and that part is marked `MOCK`.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py mechanics-test --account mechanics --suite wheel-sell-buyback
```

This sells one far-OTM put on the nearest weekly with at least 1 day to expiry, buys it back, and on PASS records `wheel_sell_buyback`.

If that account already holds a short put on `F`, or has an open `F` option order, the suite does not sell another put. It cancel-confirms the open `F` option orders, buys the short put back with the same walk (never above $1.00 per share), prints the final broker state, and exits without recording. Re-run the suite once the account is flat.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py mechanics-test --account mechanics --suite wheel-expiry --start
```

This sells one near-expiry, slightly in-the-money put and stores the intent in the mechanics journal. It does not record `wheel_lifecycle`.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py mechanics-test --account mechanics --suite wheel-expiry --reconcile
```

Run that on later days. It reads broker activities. An option that disappears without `OPASN`, `OPEXC`, or `OPEXP` is `MOCK` and does not record `wheel_lifecycle`. An `OPASN` assignment or an `OPEXC` exercise is `OBSERVED_PAPER_ASSIGNMENT`: the run checks that 100 shares are reserved, sells a covered call, and a later reconcile records `wheel_lifecycle` only after that call's delivery or expiry is observed. A mock transition is not full verification.

## Commands

Run them as `py desk.py <command>` from the repository root. On Windows, every PowerShell session must run `.\.venv\Scripts\Activate.ps1` before `py desk.py <command>`. Broker commands take `--account main` (the default) or `--account mechanics`.

| Command | What it does |
| --- | --- |
| `setup-secrets` | Prompt for the main seven secrets, or with `--account mechanics` the three `MECH_ALPACA_*` secrets, and store them in the OS keyring. |
| `doctor` | Check the config hash, hard-coded paper host, feed profile, pinned schema hash, and that profile's secrets. When those secrets are present, also read the paper account, clock, and a SPY quote. On main, prints each lifecycle check (`RECORDED`, `NOT_RECORDED`, `PROVISIONAL`, or `INVALID`) plus `lifecycle_gate` (`PASS`, `BLOCKED`, or `CORRUPT`). `BLOCKED` and `PROVISIONAL` do not fail doctor. A failed mock wheel path does. Exit 0 when the checks pass, 2 otherwise. Nothing is sent. |
| `self-test` | Print the config and schema status, then run the offline pytest suite. The exit code is pytest's. |
| `report` | Write the local HTML and CSV reports from that profile's journal and try to deliver pending outbox events. Does not trade. |
| `snapshot` | Read-only account, ledger, position, open-order, and covered-call snapshot. Refuses if that profile's broker secrets are missing, if the two expected account ids match, or if the broker account does not match. Saves `reports/snapshot_<date>.json` (mechanics: `reports/mechanics/`). Never constructs an order writer. |
| `research-check` | Read-only fetch of `research/*.json`, validated against the pinned schema. Needs `GITHUB_RESEARCH_REPO` and `GITHUB_READONLY_TOKEN`. Saves `reports/research_check_<date>.json`. Does not trade, write to GitHub, or touch the journal. |
| `pause` | Block new entries. Later cycles still reconcile and manage exits. Cancels pending entry orders when paper credentials can build a writer. Refuses if the selected broker account does not match. |
| `resume` | Clear the pause only when `doctor` passes, the experiment drawdown block is off, and three consecutive healthy reconciliations are on record. A daily loss block for the current session still applies. |
| `flatten` | Pause and work the selected paper book toward flat: confirm cancels, buy back short calls before short puts, and sell long shares only once no short calls remain. Refuses without credentials or on an account mismatch. Does not claim completion until fills reconcile. |
| `lifecycle-record --check stock\|wheel-sell-buyback\|wheel --evidence "..."` | Mechanics only. After read-only checks pass, record that check in `state/lifecycle_gate.json`. `wheel` requires `OBSERVED_PAPER_ASSIGNMENT` and rejects `MOCK`. Refuses `--account main`. |
| `mechanics-test --suite stock\|wheel-sell-buyback\|wheel-expiry` | Mechanics only. Broker-verified lifecycle steps on `F`, journaled separately from the main book. See [Mechanics lifecycle tests](#mechanics-lifecycle-tests). |
| `cycle --mode DRY_RUN\|AUTO_PAPER` | Run one cycle under a process lock. `--mode` is required. Main `AUTO_PAPER` requires `stock_lifecycle`. Wheel entries also require `wheel_sell_buyback` and the mock wheel paths, and stay `PROVISIONAL` until `wheel_lifecycle`. |
| `backtest --start YYYY-MM-DD --end YYYY-MM-DD` | Historical backtest of the stock book and the wheel book. Writes `backtest_report.json` and `backtest_report.md`. Does not trade and does not write the journal or the paper performance series. See [Historical backtest](#historical-backtest). |

If `data/desk.lock` is already held, `cycle` prints a message and exits 0 without sending orders. `flatten` takes the same lock after the credential check and does not catch a busy lock, so a second flatten fails instead of sending another batch.

## Modes

`cycle` has two modes and no default. You pass one.

- **DRY_RUN.** No `PaperWriter` is constructed, so the process has no object that can submit or cancel an order. Intents that would have been sent are recorded with status `DRY_RUN` and nothing goes to the broker. Use this while checking secrets, research, and the log.
- **AUTO_PAPER.** Builds a paper writer only when the selected profile's broker secrets are present, and can submit and cancel orders on the paper host. If those secrets are missing, the cycle is refused and no order is sent. On the main account this mode also refuses until `stock_lifecycle` is recorded (see [Mechanics account](#mechanics-account)). Wheel entries then wait for `wheel_sell_buyback` and the mock wheel paths, and are `PROVISIONAL` until `wheel_lifecycle`. `--account mechanics` is not gated. This is the mode `run-paper.cmd` and `run-paper.sh` call, on the main account.

There is no live mode.

## Research feed

The feed is optional. Point `GITHUB_RESEARCH_REPO` at `<owner>/<research-repo>` (exactly `owner/name`). The runner lists `research/*.json` through the GitHub contents API with the read-only token, follows `ETag` / `If-None-Match`, and polls at most every 15 minutes. Paths outside that folder, non-JSON names, and files over 1 MB are skipped. File bodies are parsed as JSON data and are never executed or imported.

The contract is News schema v1.7, `schemas/news_schema_v1.7.json`. Its sha256 must equal `policy.research.signed_schema_sha256`:

`a3b4c2264350807286308af60613d106e021eedcb6e0d6b51acd3a4c9e31c2d0`

The file's `schema_version` must be `news-research-v1.7`. A missing signature, a hash mismatch, or a different version blocks every new entry (`SCHEMA_NOT_SIGNED`, `SCHEMA_HASH_MISMATCH`, `SCHEMA_VERSION_MISMATCH`). `schemas/news_schema_v1.2.json`, `schemas/news_schema_v1.3.json`, `schemas/news_schema_v1.5.json` and `schemas/news_schema_v1.6.json` are kept for history and are not accepted. The v1.6 null rules still hold: a fact's source fields may be null only when its value is `UNKNOWN`, and `verified_by` may be null only while `verified_at_utc` is null. v1.7 also requires `verified_by` to be `Skeptic` for a `VERIFIED`, `INVALID`, or `UNKNOWN` verdict. `source_type` `INTERNAL_GUIDE` and `evidence_kind` `INTERNAL_POLICY` may back only `paper_universe_eligible`, and every `source_url` must still be `https://`. A dividend is the next declared dividend (`amount`, `ex_date`, `payment_date`), `NOT_YET_DECLARED`, `VERIFIED_NONE`, or `UNKNOWN`. A past `ex_date` is rejected. `NOT_YET_DECLARED` blocks a new covered call on that symbol (`DIVIDEND_NOT_YET_DECLARED`) and is otherwise handled like an `UNKNOWN` dividend.

A research file is a wrapper:

```json
{
  "schema_version": "news-research-v1.7",
  "published_at_utc": "2026-01-01T00:00:00Z",
  "publisher": "News",
  "records": []
}
```

Each record carries the same `schema_version`. Wrapper problems reject the file. Record problems block that record only.

A record may be used for new stock exposure only when `verification_status` is `VERIFIED`, `verified_at_utc` is at or after `checked_at_utc`, and the current time is before `valid_until_utc`. Wheel use also requires `ownership_card_status` `VERIFIED` and a `card_valid_until_utc` still in the future (at most 72 hours after `card_created_at_utc`, which must not move while the card is valid). A fact with `evidence_kind` `RUMOUR` cannot satisfy an eligibility field. An `UNKNOWN` eligibility fact fails the gate. A file or record with `"test": true` is never usable. Each block is stored on the journal as `RESEARCH_RECORD_BLOCKED` with a specific reason code and is listed on the cycle summary.

`desk.py research-check` validates each fetched file against those rules and prints a reason code per file and per record. It does not trade, write to GitHub, or touch the journal, so it does not apply the journal's memory of an earlier wheel-card timestamp. Until the repository actually contains records that pass, no symbol has usable research, so new entries stay blocked.

## Scheduling

Scheduling is optional. The runner does not install a task for you.

- **Windows Task Scheduler.** Action: `run-paper.cmd` in the clone. Start in that same directory. Prefer "Do not start a new instance". The script runs one `AUTO_PAPER` cycle and appends the exit code to `logs/launcher.log`.
- **cron.** `run-paper.sh` does the same with the virtualenv Python on `PATH` inside the clone (`./.venv/bin/python`). Example, from the clone path you choose:

```cron
*/5 * * * * /path/to/Paper-Desk-RAG/run-paper.sh
```

The cycle itself refuses new entries when the US cash session is closed, and the process lock ignores an overlapping start. Five minutes is only an example. Do not point two hosts at the same paper account as writers.

## Project layout

```
desk.py                  Command line
policy.json              Capital split, limits, feed profile, schema pin
watchlist.json           25 names: 19 US stock-eligible, 8 of those wheel-eligible, 6 alerts-only
readiness.json           Offline verification notes (what has and has not been connected)
run-paper.cmd            Windows scheduler entry, one AUTO_PAPER cycle
run-paper.sh             cron entry, one AUTO_PAPER cycle
requirements.txt         Pinned Python packages
pytest.ini               Pytest paths; cache provider disabled
sakibdesk/               Runner package (broker client, books, journal, research, risk)
sakibdesk/backtest/      Historical backtest (CSV cache, estimated wheel prices, its own report)
schemas/                 Pinned news schema v1.7, plus v1.2, v1.3, v1.5 and v1.6 for history
tests/                   Offline pytest suite and fixtures
.github/workflows/tests.yml
```

Runtime files are created locally and are gitignored: `data/journal.sqlite3` (and its WAL/SHM sidecars; the outbox is a table in this database), `data/desk.lock`, `data/desk.log`, `data/secrets.local.json`, `data/mechanics/` (mechanics journal, lock, and log), `reports/`, `reports/mechanics/`, `state/lifecycle_gate.json`, and `logs/`.

## Running tests

The suite under `tests/` mocks the broker, the data feed, and GitHub. `tests/conftest.py` replaces socket connects so a test cannot open a network connection. No Alpaca or GitHub credentials are read.

Create the virtual environment and install `requirements.txt` the same way as [Setup](#setup) if that is not done yet.

```powershell
.\.venv\Scripts\Activate.ps1
py -m pytest
```

`python desk.py self-test` runs that same suite. GitHub Actions (`.github/workflows/tests.yml`) runs it on `ubuntu-latest` with Python 3.11 and 3.13 for pushes and pull requests to `main`. The workflow has `contents: read` only, passes no secrets, and sets `PYTHON_KEYRING_BACKEND` to the null keyring so a headless runner does not probe the Secret Service.

## Historical backtest

`py desk.py backtest` replays the shipped stock and wheel rules on daily split-adjusted bars. It calls the live signal, size, exit, contract screen, tick rounding, and wheel state machine. It does not change `policy.json`, `watchlist.json`, or the config hash. It does not write the journal, the paper performance series, or any broker order.

Wheel prices are estimates. The report labels them ESTIMATED. A cache of CSV bars is enough to run; tests use synthetic bars and do not open a network connection. `--fetch` optionally fills the cache from the Alpaca market-data bars endpoint (`data.alpaca.markets`, split-adjusted, the policy feed). It also requests the 420 calendar days before `--start`, which is the same warmup the live runner uses, so the 200-day average exists on the first session. The key and secret come from the existing secrets store. They are not command-line arguments and they are not written into the cache.

```powershell
.\.venv\Scripts\Activate.ps1
py desk.py backtest --start 2021-01-04 --end 2026-01-02 --output-dir reports\backtest --fetch
```

`--capital` accepts a comma-separated list. The default list is 2000, 5000, 10000, 25000, and the configured paper capital. `--books` is `stock`, `wheel`, or `stock,wheel`.

The report is a point-in-time calculation with these limits:

- Survivorship bias. The universe is the watchlist fixed in this repository today. Names that failed earlier are absent.
- Estimated option prices. The wheel book uses Black-Scholes on realised volatility, with an IV haircut, a bid/ask haircut, and the live tick rounding. That is not a historical option chain.
- Idealised fills. Decisions use the session open. A daily bar that touches both the stop and the target is filled at the stop. Historical research cards, earnings dates, dividends, and open interest are not in the cache; the report states the assumption used for each.
- It is not a forecast and it is not financial advice.

## Status and limitations

This is a work in progress. The pieces below are implemented and covered by offline tests. They have not been observed against a live paper session in this repository.

- New entries stay blocked until a research repository supplies records that pass the pinned gate. The bridge client itself is not pre-connected to any repository.
- `mechanics-test` can verify stock orders, a wheel sell/buyback, and a multi-day assignment on the mechanics paper account. Until those runs are recorded, stock and wheel order lifecycles in this repository are mock-tested only. A mock assignment is not full verification. Mechanics results stay out of the main performance series.
- Webhook delivery is mock-tested. You still have to supply the URL and bearer, and to install Task Scheduler or cron yourself.
- `flatten` can take more than one run: short calls are bought back before the shares that cover them are sold, and the command does not report the book flat until those fills reconcile.
- The process lock is local to one machine. It does not stop a second computer.
- `readiness.json` is a point-in-time note of those gaps, not a health check of your account.

## License

MIT. The copyright notice and permission text are in [LICENSE](LICENSE). Copyright (c) 2026 sSseeth.
