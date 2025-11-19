# Schwab Level II Alerting, Dashboard, and Trading Bridge

This repository contains a full pipeline for spotting heavy bid/ask activity on
Schwab Level II feeds, storing the signals in SQLite, visualizing them in
Streamlit, and optionally mirroring them into paper or live Schwab orders. It
also includes an end-of-day (EOD) support/resistance scanner for a Nasdaq
universe.

```
┌────────────┐     Level II       ┌────────────┐      alerts.db
│ Schwab API │ ───────────────▶ │ grok.py    │ ───────┬──────────────┐
└────────────┘  (quotes/orders)  │ (alert bot)│       │              │
                                  └────────────┘   SQL │              │
                                                       ▼              ▼
                                               paper_trader.py   ui.py (Streamlit)
                                                       │              ▲
                                                       ▼              │
                                                 live_trader.py  Support/Res
                                                                  sup_res.py
```

## Components

| Path | Purpose |
| ---- | ------- |
| `grok.py` | Main Schwab streaming client. Normalizes Level II books, calculates rolling window metrics, and inserts alerts/positions into SQLite. |
| `ui.py` | Streamlit dashboard that auto-refreshes to show the latest alerts, positions, and paper fills. |
| `paper_trader.py` | Flip-only paper trading engine that tails the `alerts` table, stores fills/PnL, and persists state so you can stop/restart without losing context. |
| `live_trader.py` | Schwab order bridge that replays the paper trader's signals into paperMoney or live REST endpoints (market orders via `schwab-py`). |
| `auth_login.py` | Helper to create/refresh Schwab OAuth tokens referenced by every other script. |
| `sup_res.py` | Support/resistance watcher that walks the Nasdaq `$2–$50` universe defined in `nasdaq_2_to_50_stocks.csv`. |
| `run_both.sh` / `stop_trading_bot.sh` | Convenience shell scripts for launching/killing the alert bot + dashboard together. |
| `sql.py` | Example read-only queries for exploring the SQLite database. |
| `requirements.txt` | Python dependencies for the whole stack. |

## Prerequisites

- Python 3.10+ (developed against CPython 3.11).
- Schwab Developer account with API key, app secret, redirect URI, and streaming permissions.
- Browser access to complete the OAuth login flow when refreshing tokens.
- (Optional) Schwab paperMoney or live account for `live_trader.py`.

## Installation

1. Clone the repo and enter it.
   ```bash
   git clone https://github.com/<your-account>/taranveer-singh.github.io.git
   cd taranveer-singh.github.io
   ```
2. Create/activate a virtual environment (recommended).
   ```bash
   python -m venv .venv
   source .venv/bin/activate  # Windows: .venv\Scripts\activate
   ```
3. Install dependencies.
   ```bash
   pip install --upgrade pip
   pip install -r requirements.txt
   ```

## Environment configuration

All scripts rely on a `.env` file in the repo root. The following variables are
required for the streaming + trading stack:

```ini
SCHWAB_CLIENT_ID=your_app_key_without_suffix
SCHWAB_APP_SECRET=your_app_secret
SCHWAB_REDIRECT_URI=https://127.0.0.1:8182/
SCHWAB_ACCOUNT_ID=paper_or_live_account_hash
SCHWAB_TOKEN_PATH=./schwab_tokens.json
DB_PATH=penny_basing.db          # overrides default SQLite path
SYMBOLS=SNAP,NVDA                # comma or space separated list
MIN_VOLUME=150000                # per-symbol shares/minute requirement
```

`grok.py` also honors tuning variables such as `WINDOW_SECONDS`,
`HEARTBEAT_SEC`, `BOOK_INTERVAL_SEC`, `MIN_ASK_HEAVY`, `MIN_BID_HEAVY`,
`MAX_RANGE_CENTS`, `ALERT_THROTTLE_SEC`, `MIN_IMBALANCE_DURATION_SEC`, and
`BOOK_RAW_LIMIT`. Unset variables fall back to the script defaults.

## Authenticate with Schwab

Create or refresh the OAuth token before streaming:

```bash
python auth_login.py --force-login
```

A browser will prompt for Schwab credentials. On success, the token file at
`SCHWAB_TOKEN_PATH` is updated and reused by every script.

## Running the real-time alert pipeline

1. **Start the streamer.** Supply symbols and optional overrides on the command
   line (these take precedence over `.env`).
   ```bash
   python grok.py --symbols SNAP,NVDA --min-volume 150000
   ```
   - Level II data is normalized per exchange, debounced, and inserted into the
     `alerts` table inside `DB_PATH`.
   - Structured logs (INFO/WARN) describe throttling, heartbeat gaps, and alert
     payloads for debugging.

2. **Launch the dashboard.**
   ```bash
   streamlit run ui.py
   ```
   The Streamlit app reads the same database and auto-refreshes to show alerts,
   open positions, and paper fills. On Windows the DB defaults to
   `%LOCALAPPDATA%\taranveer_app\penny_basing.db`; elsewhere it lives beside
   `ui.py` unless `DB_PATH` is set.

3. **Optional combined launcher.** Update the `cd` path inside `run_both.sh`
   before executing it to start `grok.py` + `ui.py` together. Use
   `stop_trading_bot.sh` to terminate them.

## Paper trading loop

`paper_trader.py` consumes the `alerts` table and flips between long/short
positions when the alert direction changes (no stacking). Key traits:

- Persists cash + open positions in `paper_trader_state.json` so you can restart
  intraday.
- Writes every fill to `paper_trades` and maintains a `paper_positions` table
  for the dashboard.
- Respects configurable constants near the top of the script (`POSITION_SIZE`,
  `SHORT_SIZE`, `SLIPPAGE`, `COMMISSION`).

Run it alongside the streamer:

```bash
python paper_trader.py
```

## Live/paperMoney execution bridge

`live_trader.py` mirrors the paper trader's flip-only logic into Schwab REST
orders. It shares the same `.env` credentials and supports a `LIVE_DRY_RUN=1`
flag for rehearsals. Typical usage:

```bash
python live_trader.py --db penny_basing.db --min-alert-id 0
```

- The script tails the `alerts` table, deduplicates order intents, and submits
  market orders (`BUY`, `SELL`, `SHORT`, `COVER`) via `schwab-py`.
- The `--min-alert-id` flag is handy when you want to ignore historical alerts
  after restarting the bot.
- Ensure `SCHWAB_ACCOUNT_ID` points to your paperMoney account hash before going
  live.

## Support/resistance scanning

The `sup_res.py` utility scans the Nasdaq universe in `nasdaq_2_to_50_stocks.csv`
for prices approaching 1-week/30-day/52-week levels within a configurable
band.

```bash
python sup_res.py --watch
```

It prints human-readable alerts and can be run independently of the Level II
pipeline.

## Database schema

`grok.py` automatically creates:

- `alerts` – timestamped imbalance events with symbol, price, side, aggregate
  bid/ask volume, heavy venues, etc.
- `positions` – optional manual entries for currently held trades.

`paper_trader.py` adds:

- `paper_trades` – synthetic fills including slippage/commission and realized
  PnL.
- `paper_positions` – current holdings tracked by the paper engine.

`sql.py` contains sample queries (update the `db_path` variable inside before
running it manually).

## Troubleshooting

- **Missing env vars** – Scripts raise descriptive `RuntimeError` messages if an
  expected variable is absent.
- **Token failures** – Delete the file pointed at by `SCHWAB_TOKEN_PATH` and
  re-run `python auth_login.py --force-login`.
- **Database mismatch** – The dashboard prints the exact DB path it is trying
  to open; make sure it matches the `DB_PATH` used by `grok.py`/`paper_trader.py`.
- **Schwab throttling** – Watch the `grok.py` logs for `HEARTBEAT` warnings or
  reconnect messages if the streaming client gets disconnected.
- **Order errors** – `live_trader.py` logs the Schwab response status and order
  location headers; double-check account permissions and whether `dry_run` mode
  is still enabled.

## License

No explicit license is provided. Assume all rights reserved unless the owner
states otherwise.
