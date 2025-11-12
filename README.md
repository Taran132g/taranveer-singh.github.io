# Trading Alert Bot & Streamlit Dashboard

This repository powers a penny-stock basing detector that consumes live Level II order book data from the Schwab streaming API, stores actionable alerts in SQLite, and surfaces them in a Streamlit dashboard. It also contains an end-of-day support/resistance scanner built on Yahoo Finance data.

## Features
- **Real-time alerting (`grok.py`)** – Subscribes to Schwab Level II feeds, tracks heavy bid/ask activity, and records alerts when liquidity conditions trip configurable thresholds.
- **Interactive dashboard (`ui.py`)** – Streamlit app that renders the latest alerts and open positions out of the SQLite database with auto-refresh styling for quick triage.
- **Support & resistance watcher (`sup_res.py`)** – Periodically polls a universe of $2–$50 Nasdaq tickers for proximity to weekly, monthly, and yearly levels.
- **Utility scripts** – OAuth helper (`auth_login.py`), combined launcher (`run_both.sh`), process cleanup (`stop_trading_bot.sh`), and sample SQL queries (`sql.py`).

## Repository layout

| Path | Purpose |
| ---- | ------- |
| `grok.py` | Main Schwab streaming client and alert generator. |
| `ui.py` | Streamlit UI for viewing alerts/positions from SQLite. |
| `sup_res.py` | Support/resistance polling loop for Nasdaq symbols. |
| `auth_login.py` | Creates/refreshed Schwab OAuth tokens using `.env` secrets. |
| `run_both.sh` | Convenience script to launch `grok.py` and the dashboard together (adjust the hard-coded path first). |
| `stop_trading_bot.sh` | Kills any running `grok.py` or Streamlit processes. |
| `sql.py` | Example SQLite readout (edit the database path before running). |
| `nasdaq_2_to_50_stocks.csv` | Symbol universe used by `sup_res.py`. |
| `requirements.txt` | Python dependencies for the project. |

## Prerequisites
- Python 3.10+ (tested with CPython 3.11).
- Schwab Developer account with API key, secret, redirect URI, and enabled streaming permissions.
- Ability to create OAuth tokens via the Schwab login flow (interactive browser access).

## Installation
1. Clone the repository and change into it.
   ```bash
   git clone https://github.com/<your-account>/taranveer-singh.github.io.git
   cd taranveer-singh.github.io
   ```
2. Create and activate a virtual environment (optional but recommended).
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
Create a `.env` file in the project root with at least the following variables:

```ini
SCHWAB_CLIENT_ID=your_app_key_without_suffix
SCHWAB_APP_SECRET=your_app_secret
SCHWAB_REDIRECT_URI=https://127.0.0.1:8182/
SCHWAB_ACCOUNT_ID=123456789
SCHWAB_TOKEN_PATH=./schwab_tokens.json  # optional; defaults to ./schwab_tokens.json
SYMBOLS=SNAP,F            # optional; overrides the default single symbol (F)
MIN_VOLUME=150000         # optional; per-symbol minimum shares/minute
DB_PATH=penny_basing.db   # optional; set a custom SQLite location
```

Additional optional knobs:
- `WINDOW_SECONDS`, `HEARTBEAT_SEC`, `MIN_ASK_HEAVY`, `MIN_BID_HEAVY`, `MAX_RANGE_CENTS`, `ALERT_THROTTLE_SEC`, `MIN_IMBALANCE_DURATION_SEC`, `BOOK_INTERVAL_SEC`, and `BOOK_RAW_LIMIT` tune runtime behavior of `grok.py`.
- `SYMBOLS` can use either commas or spaces between tickers.

## Authenticate with Schwab
Generate and/or refresh the Schwab OAuth token before streaming:
```bash
python auth_login.py --force-login
```
This guides you through the browser login flow and saves the token file defined by `SCHWAB_TOKEN_PATH`.

## Running the alert pipeline
1. **Start the streamer** (requires the `.env` variables and a token file):
   ```bash
   python grok.py --symbols SNAP,NVDA --min-volume 150000
   ```
   - Alerts are written into the SQLite database specified by `DB_PATH` (defaults to `penny_basing.db`).
   - On Windows the Streamlit UI stores data under `%LOCALAPPDATA%\taranveer_app\penny_basing.db`; on other systems a `data/` folder is created beside `ui.py`.

2. **Launch the dashboard** in a separate terminal:
   ```bash
   streamlit run ui.py
   ```
   Visit the displayed URL (default `http://localhost:8501`) to view incoming alerts and positions with auto-refresh.

3. **Optional combined launcher** – Update the `cd` path inside `run_both.sh` and execute it to start both services in one command. Use `stop_trading_bot.sh` to terminate them.

## Support/resistance scanning
The `sup_res.py` script scans the Nasdaq universe loaded from `nasdaq_2_to_50_stocks.csv`.
```bash
python sup_res.py --watch
```
It prints alerts when closing prices break or approach key levels (1-week, 30-day, 52-week) within a 0.5% band, respecting volume and price filters defined near the top of the script.

## Database notes
- `grok.py` creates/maintains two tables:
  - `alerts`: timestamped imbalance events (symbol, direction, price, bid/ask totals, heavy venues, etc.).
  - `positions`: optional table for tracking open trades.
- `sql.py` demonstrates how to query alerts, but it contains a user-specific path—edit `db_path` before running.

## Troubleshooting
- **Missing env vars** – `grok.py` and `auth_login.py` enumerate missing variables and exit with `CONFIG_ERROR` messages.
- **Token errors** – Delete the token file referenced by `SCHWAB_TOKEN_PATH` and re-run `auth_login.py --force-login`.
- **Database not found** – The Streamlit app displays the expected path in warning messages; confirm the bot is writing to that location.
- **Dependencies** – If Streamlit or Schwab imports fail, re-run `pip install -r requirements.txt` inside your active virtual environment.

## License
No explicit license has been provided. Assume all rights reserved unless the repository owner specifies otherwise.
