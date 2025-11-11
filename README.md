# Trading Bot Orchestration

This repository contains the scripts used to monitor a NASDAQ stock universe for support/resistance levels and to stream level 2 order book data for automated trading alerts.

## Repository Layout

| File | Purpose |
| --- | --- |
| `sup_res.py` | Scans a CSV universe for symbols priced between $5–$30 with ≥100k shares/minute and emits support/resistance alerts. |
| `grok.py` | Connects to the Schwab streaming API to watch level 2 data and fire buy/sell alerts when heavy activity is detected across multiple venues. |
| `run_both.sh` | Orchestrates a single support/resistance scan, boots `grok.py` with the discovered symbols, and keeps the scanner running in watch mode. |
| `stop_trading_bot.sh` | Utility script to kill any lingering `grok.py` or `ui.py` processes. |
| `nasdaq_2_to_50_stocks.csv` | Base universe of NASDAQ tickers to screen each morning. |
| `requirements.txt` | Core Python dependencies for the support/resistance scanner. |

## Prerequisites

1. **Python**: Python 3.9 or newer is recommended.
2. **System packages**: Ensure `git`, `bash`, and `pip` are available.
3. **Python packages**:
   ```bash
   pip install -r requirements.txt
   pip install python-dotenv schwab-py
   ```
   The extra packages are required so `grok.py` can authenticate with Schwab and read environment variables.

## Configuring Credentials

The level 2 streamer expects Schwab API credentials to be present in your environment. The easiest approach is to create a `.env` file alongside the scripts:

```env
SCHWAB_CLIENT_ID=your_app_key
SCHWAB_APP_SECRET=your_app_secret
SCHWAB_REDIRECT_URI=https://127.0.0.1:8182/
SCHWAB_ACCOUNT_ID=123456789
SCHWAB_TOKEN_PATH=./schwab_tokens.json  # optional override
```

Refer to Schwab's developer documentation for obtaining these values. The first run will open a browser window so you can authorize the app; tokens are cached at the path specified by `SCHWAB_TOKEN_PATH`.

## Generating Support/Resistance Alerts

You can run the scanner once to refresh the daily watchlist:

```bash
python sup_res.py --once --output alerts.txt
```

This command:
- Loads the NASDAQ CSV universe.
- Filters for tickers trading between $5 and $30 with sufficient volume.
- Writes any alerts and tickers to `alerts.txt` in the format consumed by downstream scripts.

To keep the watchlist updated throughout the session, start watch mode instead:

```bash
python sup_res.py --watch --output alerts.txt
```

## Streaming Level 2 Data with `grok.py`

Once credentials and symbols are ready, you can launch the level 2 monitor manually:

```bash
python grok.py --symbols "F,AAPL" --min-volume 100000 --min-venues 4
```

Key CLI flags mirror the environment variables declared in the script and allow per-run overrides. Alerts are persisted to a SQLite database (`penny_basing.db` by default) and logged to stdout.

## Full Orchestration (`run_both.sh`)

For day-to-day use, rely on the orchestrator:

```bash
./run_both.sh
```

The script will:
1. Run a single `sup_res.py --once` pass and capture the tickers.
2. Start `grok.py` with the discovered symbols (requiring at least four heavy venues by default).
3. Relaunch `sup_res.py` in `--watch` mode to keep `alerts.txt` and `sup_res.log` updated.

Logs are written to `grok.log` and `sup_res.log` in the repository root. Press `Ctrl+C` to stop both services; the script cleans up processes automatically.

## Stopping Services Manually

If you ever need to force-stop the running processes, execute:

```bash
./stop_trading_bot.sh
```

## Updating the Universe CSV

`sup_res.py` reads `nasdaq_2_to_50_stocks.csv`. Replace or refresh this file each morning with the latest universe to ensure accurate screening. The script will log any CSV parsing issues to the console and exit.

## Troubleshooting

- **Missing tokens**: Delete the file pointed to by `SCHWAB_TOKEN_PATH` and rerun `grok.py` to restart the OAuth flow.
- **No tickers discovered**: Verify the CSV universe is populated and trading hours are open; otherwise the scanner may not find qualifying symbols.
- **Dependencies**: If you see import errors for Schwab modules, confirm `schwab-py` installed correctly and that you are using the same Python interpreter for installation and execution.

## License

This repository is private and provided for internal automation. Consult the project maintainers before redistributing.
