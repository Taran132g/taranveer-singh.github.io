# Simulator Usage Guide

This guide explains how to use the simulator primitives (`SimulationConfig`, `MarketModel`, `SimulatedExecutor`) that ship with this repository. It covers configuration, integration patterns, metrics, persistence, and quick-start examples.

## Overview

The simulator mirrors the Schwab execution surface so that existing trading logic can be exercised without touching real endpoints. It is built from three pieces:

- **`SimulationConfig`** – Tunable parameters for latency, slippage, and limit-order fill behavior.
- **`MarketModel`** – Probabilistic generator that samples latency/slippage and estimates whether a limit order should fill.
- **`SimulatedExecutor`** – Drop-in executor that accepts orders, applies the market model, records metrics, and optionally persists events to SQLite.

Use these components directly in code (see `example_simulator.py`) or wire them into the current alert/paper trading flows for offline experiments.

## Quick start

```python
from simulator import MarketModel, SimulationConfig, SimulatedExecutor

config = SimulationConfig(
    latency_mean_ms=120,
    latency_std_ms=40,
    slippage_min_bps=0.5,
    slippage_max_bps=4.0,
    limit_fill_base_probability=0.55,
    volume_impact_factor=1.2,
)
model = MarketModel(config)
executor = SimulatedExecutor(
    market_model=model,
    current_price_func=lambda symbol: 100.0,  # replace with a real quote source
)

# Place a market order
market_result = executor.place_order("AAPL", side="BUY", quantity=50, order_type="MARKET")

# Place a limit order
limit_result = executor.place_order(
    "AAPL", side="SELL", quantity=50, order_type="LIMIT", limit_price=101.25
)

# Poll limit status until filled/cancelled
latest = executor.get_order_status(limit_result.order_id)
```

Pass a `sleep_func` (e.g., `lambda s: None`) to `SimulatedExecutor` to skip real sleeps during testing.

## Configuration

`SimulationConfig` controls the behavior of the `MarketModel`:

- `latency_mean_ms` / `latency_std_ms`: Truncated normal distribution for exchange + network latency.
- `slippage_min_bps` / `slippage_max_bps`: Uniform range for market-order slippage (basis points).
- `limit_fill_base_probability`: Baseline probability that a limit order resting near the touch will fill.
- `volume_impact_factor`: Exponential penalty applied when order size is large relative to typical volume.

All values are validated on initialization. A helper YAML config (e.g., `sim_config.yaml`) can mirror these fields:

```yaml
latency_mean_ms: 120
latency_std_ms: 35
slippage_min_bps: 0.25
slippage_max_bps: 3.5
limit_fill_base_probability: 0.6
volume_impact_factor: 1.0
```

Load YAML parameters with your preferred config loader and pass them into `SimulationConfig(**data)`.

## Integrating with alerts (SQLite)

The simulator can replay historical `alerts` from the SQLite database produced by `grok.py`:

1. Connect to the database (`DB_PATH` from `.env`).
2. Query the `alerts` table for your date range/symbols.
3. Convert alerts into order intents (e.g., BUY on heavy bid, SELL on heavy ask).
4. Route the intents through `SimulatedExecutor`.

`example_simulator.py` includes a minimal `simulate_alerts` helper that demonstrates this pattern. You can swap in richer logic from `paper_trader.py` if you want parity with the flip-only strategy.

## Metrics and analytics

Attach a `SimulatorMetrics` instance to capture aggregate statistics:

```python
from simulator import SimulatorMetrics

metrics = SimulatorMetrics()
executor = SimulatedExecutor(model, current_price_func=..., metrics=metrics)

# ... submit orders ...
print(metrics.get_summary())
```

Metrics include order counts, limit fill rates, slippage/latency min/max/mean/std, and can be reset between scenarios. If you pass `db_path`, per-order metrics snapshots are written to the `sim_metrics` table alongside `sim_orders` and `sim_fills`.

### SQLite schema

The simulator creates three tables when `db_path` is provided:

- **`sim_orders`**: `order_id`, `symbol`, `side`, `quantity`, `order_type`, `limit_price`, `status`, `submit_time`, `fill_time`, `latency_ms`
- **`sim_fills`**: `fill_id`, `order_id`, `filled_qty`, `fill_price`, `slippage_bps`, `fill_time`
- **`sim_metrics`**: `session_id`, `timestamp`, `metric_name`, `metric_value`

Use `visualize_simulator.py` to explore latency and slippage distributions or compute limit fill rates over time.

## Example flows

- **Market + limit demo**: See `run_basic_example()` in `example_simulator.py`.
- **Metrics collection**: See `run_metrics_example()` to aggregate slippage/latency stats.
- **Database persistence**: See `run_persistence_example()` to write to `sim_orders`/`sim_fills`.
- **Alert replay**: See `simulate_alerts()` for a simple alerts-to-orders loop.

## Tips

- Provide deterministic `random.Random(seed)` to `MarketModel` during tests for reproducible outcomes.
- Override `typical_volume_func` in `SimulatedExecutor` to reflect symbol-specific liquidity.
- Combine `SimulatedExecutor` with the `RealSchwabExecutor` stub for A/B comparisons if you want to contrast simulated vs. live behavior.

## Visualization script (optional)

`visualize_simulator.py` reads the simulator tables and plots latency/slippage histograms and a rolling limit fill rate. It expects `pandas` and `matplotlib`; install via `pip install pandas matplotlib` if your environment is missing them.
