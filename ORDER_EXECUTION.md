# Execution strategy and latency resilience

This note summarizes how we balance fast alert handling with order-safety when
limits risk missing fills because prices move during the alert-to-trade window.
It also calls out operational checks that keep the inline Grok → LiveTrader path
safe from bugs.

## Current behavior
- **Inline dispatch first.** Grok sends each alert directly to LiveTrader so the
  trading logic runs immediately; the DB entry is for durability, not signaling.
- **Limit-first, market-backed.** LiveTrader prefers limit orders for price
  control but falls back to market only when limit submission fails (e.g., API
  rejection) to avoid silent drops.
- **Fill polling.** Accepted limit orders are polled until they fill, cancel,
  or time out; only filled orders update positions.

## Price movement risk
If price moves between alert creation and order submission, a limit anchored to
stale data can miss. Two safeguards reduce that risk:

1. **Aggressive limit padding.** A configurable bps cushion moves the limit a
   small amount in the favorable direction to cross the spread while still
   capping slippage. For buys/covers the price is nudged up; for shorts/sells it
   is nudged down. The default pad is 10 bps (~0.1%).
2. **Submission fallback.** If the limit cannot be submitted, LiveTrader logs a
   warning and sends a market order instead so alerts are not dropped silently.

## Recommended settings
- Tune `LIVE_LIMIT_SLIPPAGE_BPS` to the minimum cushion that routinely fills on
  your symbols (e.g., 5–15 bps for liquid names). Set to `0` to disable padding
  if you must cap price strictly.
- Keep `LIVE_PREFER_LIMIT_ORDERS=true` to avoid surprise fills when latency
  spikes; the pad helps fills without fully switching to market.
- Shorten `LIVE_LIMIT_FILL_TIMEOUT` if you prefer to give up sooner and move on;
  lengthen it for thin symbols where fills take longer.

## P&L impact of the slippage pad
- A 10 bps pad adds roughly $0.01 on a $10 order; the cost is bounded by your
  configured `LIVE_LIMIT_SLIPPAGE_BPS` and is usually less than the spread you
  were already willing to cross when latency hits.
- Without the pad, alerts that drift by a cent or two often never fill, which
  can quietly erase expected edge. The pad trades a tiny, known cost for a
  higher fill rate so the strategy actually participates.
- Reduce the pad toward 0 for extremely tight-spread symbols or when you see
  systematic negative slippage; increase slightly (e.g., 15 bps) for illiquid
  names to avoid repeat timeouts and market fallbacks.

## Operational guardrails
- **Kill switch**: ensure `LIVE_KILL_SWITCH_FILE` is monitored; creating the file
  triggers a cancel-all and market flatting on next loop iteration.
- **Rate limiting**: `LIVE_MAX_TRADES_PER_HOUR` keeps runaway alert storms from
  spiraling; exceeding it engages the kill switch.
- **State persistence**: last seen alert ID and positions are checkpointed after
  each processed alert so restarts do not re-run old alerts.

## Debugging checklist
- Watch logs for `Limit price adjusted...` to confirm padding is active.
- Verify DB `live_orders` rows record the adjusted price you expect.
- In dry-run mode, confirm `_record_fill` messages fire only after the fill poll
  succeeds; otherwise the position should remain unchanged.
