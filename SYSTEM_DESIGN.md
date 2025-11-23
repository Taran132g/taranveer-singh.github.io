# Real-time alerting and trading system design

## High-level architecture
- **grok.py** streams Schwab Level II data, detects bid/ask imbalances, and creates alert payloads.
- **Inline LiveTrader (preferred path)**: when the LiveTrader class initializes successfully inside `grok.py`, each alert is dispatched immediately via `inline_trader_dispatch` into `LiveTrader.process_alert`, which schedules the trade on the asyncio executor without waiting for database I/O.
- **SQLite persistence (durability + observability)**: alerts are still written to `alerts` (unless `INLINE_DISPATCH_ONLY=1` is set) so dashboards (`ui.py`), paper trading, and backfill flows have a durable history.
- **Polling fallback (standalone mode)**: when LiveTrader is run as a separate process, it tails `alerts` with the adaptive poller and mirrors intents into Schwab orders. PaperTrader uses the same adaptive poller for simulation.
- **Streamlit dashboard** reads the shared SQLite DB to visualize alerts, positions, and paper fills.

## End-to-end control flow
1. **Subscribe**: `grok.py` authenticates with Schwab, resolves per-symbol exchanges, and subscribes to Level II feeds.
2. **Detect**: order books are flattened and rolled into ratios/venue counts; when thresholds are met for long enough, an alert dictionary is assembled.
3. **Dispatch**: the next alert ID is picked (monotonic even in inline-only mode) and the alert is sent to `inline_trader_dispatch` if available.
4. **Persist + notify**: unless `INLINE_DISPATCH_ONLY=1`, the alert is inserted into SQLite. PaperTrader, LiveTrader (standalone), and Streamlit consume this durable row.
5. **Trade**: LiveTrader’s `_handle_alert` flip logic converts the alert into Schwab REST orders (or dry-run logs) via `SchwabOrderExecutor`.

## Component integration checks
- **Inline availability**: verify `inline_trader_dispatch` is logged as enabled at grok startup; otherwise alerts will rely on the fallback poller.
- **ID consistency**: inline dispatch uses the same `rowid` sequence that SQLite would, ensuring the trade and DB consumers reference identical alert IDs.
- **Shared environment**: `SCHWAB_*` credentials and `DB_PATH` must match between grok, LiveTrader, PaperTrader, and Streamlit so they operate on the same account and database file.
- **Alert schema**: grok ensures `alerts` exists with the expected columns before writing; downstream consumers assume that schema.
- **Backfill/standalone**: if running LiveTrader separately, confirm the DB file is reachable and the poller backoff is tuned via `LIVE_POLL_INTERVAL` as needed.

## Latency reduction playbook
- **Stay inline when possible**: run LiveTrader inside grok so alerts skip polling entirely. Use `INLINE_DISPATCH_ONLY=1` if durable persistence isn’t required mid-session.
- **Executor efficiency**: pin the asyncio executor with a small, dedicated thread pool for trading callbacks to reduce thread wake-up jitter during bursts.
- **Logging impact**: keep `DEBUG`/book dumps off in production; structured log assembly can add milliseconds under load.
- **Database footprint**: if persistence is required, keep `DB_PATH` on fast local storage (tmpfs/NVMe) and ensure WAL mode is enabled to minimize write stalls.
- **Polling fallback tuning**: lower `LIVE_POLL_INTERVAL` toward the paper trader’s 50ms hot-loop ceiling when running standalone, and prefer `INLINE_DISPATCH_ONLY` during critical windows.
- **Network prep**: keep `SchwabOrderExecutor` instantiated once per process and reuse its client; avoid recreating clients on every alert.
- **System resources**: pin processes to performance cores and keep CPU scaling governors in performance mode to reduce scheduling latency.

## Resilience considerations
- **Inline watchdog**: monitor grok process health; if it dies, LiveTrader’s standalone poller can continue trading from persisted alerts.
- **Order error handling**: `SchwabOrderExecutor` logs the HTTP status and location header for debugging; periodically reconcile orders against Schwab to catch partial fills.
- **Graceful shutdown**: ensure `atexit` hooks or signal handlers flush outstanding commits and cancel open orders when stopping the bot.
