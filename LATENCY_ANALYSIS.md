# Alert-to-trade latency comparison

This note compares the major alert-consumption paths and how quickly they can
turn an alert row into a trade. Latency below refers to the software pipeline
between the alert being inserted and the trading logic starting; network/exchange
latency for the Schwab order itself is unchanged.

## Baseline: fixed 1s polling (original PaperTrader/LiveTrader)
- Behavior: `monitor_alerts` polled `alerts` once per second.
- Latency: up to **1s worst case** if a new alert arrived right after a poll.
- Hot-loop protection: natural from the 1s sleep, but responsiveness was poor.

## Adaptive polling with mtime wake-ups (current PaperTrader)
- Behavior: after seeing any alerts, the loop sleeps only 50ms; when no new
  rows are found it exponentially backs off to 2s but probes the DB file mtime
  every 10ms to break the sleep when a write occurs.
- Latency: ~50ms between alerts while active; during idle backoff, a new alert
  is detected within ~10ms of the write instead of waiting for the full backoff
  window. 【F:paper_trader.py†L262-L356】
- Trade-off: slightly higher idle CPU due to the 10ms probe, but still bounded
  and far lower latency than the original 1s sleep.

## LiveTrader poller (fallback when inline dispatch is unavailable)
- Behavior: mirrors PaperTrader’s adaptive polling: 50ms hot path, exponential
  backoff to the greater of `LIVE_POLL_INTERVAL` or 2s, and 10ms DB mtime probes
  that wake the loop the moment an alert is written. 【F:live_trader.py†L117-L175】【F:live_trader.py†L360-L439】
- Latency: ~50ms between alerts while active; during idle backoff, new alerts
  wake the loop via mtime probing in ~10ms instead of waiting for the full
  backoff window.

## Inline dispatch inside `grok.py`
- Behavior: every alert insert immediately hands the rowid + payload to
  `LiveTrader.process_alert` via the asyncio executor, bypassing any polling
  waits. Inline dispatch is now always enabled when `LiveTrader` initializes
  successfully. 【F:grok.py†L792-L821】
- Latency: effectively **sub-millisecond scheduling** from the insert to the
  executor submission (plus the executor thread wake-up), removing the polling
  gap entirely. DB writes are still used for durability but not for signaling.
- Trade-off: ties trading directly to the grok process; if grok dies or stalls,
  inline dispatch stops. Keeping the polling path as a fallback maintains
  resilience.

## Which is fastest?
1. **Inline dispatch** is the lowest latency because it triggers trading logic
   immediately when the alert is written, without waiting for any poll.
2. **Adaptive LiveTrader/PaperTrader pollers** both respond within ~50ms on the
   hot path and ~10ms when idling, providing a resilient fallback when inline
   dispatch is unavailable.

## Additional options to push latency lower
- Swap the 10ms file probe for an event-driven watcher (e.g., inotify on Linux)
  to wake on writes without periodic sleeps.
- Move alert emission and trading into the same process boundary (similar to
  inline dispatch) but using an in-memory queue/channel so inserts and trades
  are decoupled from SQLite I/O.
- If polling remains necessary, align the LiveTrader interval with PaperTrader’s
  fast path (50ms or lower) and keep a small idle backoff + wake trigger to
  avoid hot loops while retaining responsiveness.
