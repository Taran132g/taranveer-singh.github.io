"""Plot latency, slippage, and limit fill rates from simulator runs."""

from __future__ import annotations

import argparse
import logging
import sqlite3
from pathlib import Path
from typing import Dict

import matplotlib.pyplot as plt
import pandas as pd

LOGGER = logging.getLogger("visualize_simulator")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _load_tables(db_path: Path) -> Dict[str, pd.DataFrame]:
    """Load simulator tables if present, returning empty DataFrames when missing."""

    frames: Dict[str, pd.DataFrame] = {}
    with sqlite3.connect(db_path) as conn:
        for table in ["sim_orders", "sim_fills", "sim_metrics"]:
            try:
                frames[table] = pd.read_sql(f"SELECT * FROM {table}", conn)
            except Exception as exc:  # table might not exist
                LOGGER.warning("Skipping %s: %s", table, exc)
                frames[table] = pd.DataFrame()
    return frames


def _plot_latency(ax, orders: pd.DataFrame) -> None:
    """Render a latency histogram if data is available."""

    if orders.empty or "latency_ms" not in orders:
        ax.text(0.5, 0.5, "No latency data", ha="center", va="center")
        ax.set_axis_off()
        return
    ax.hist(orders["latency_ms"].dropna(), bins=25, color="#4c78a8")
    ax.set_title("Latency (ms)")
    ax.set_xlabel("Latency (ms)")
    ax.set_ylabel("Count")


def _plot_slippage(ax, fills: pd.DataFrame) -> None:
    """Render a slippage histogram if data is available."""

    if fills.empty or "slippage_bps" not in fills:
        ax.text(0.5, 0.5, "No slippage data", ha="center", va="center")
        ax.set_axis_off()
        return
    ax.hist(fills["slippage_bps"].dropna(), bins=25, color="#f58518")
    ax.set_title("Slippage (bps)")
    ax.set_xlabel("Slippage (bps)")
    ax.set_ylabel("Count")


def _plot_limit_fill_rate(ax, orders: pd.DataFrame) -> None:
    """Plot cumulative limit fill rate over time."""

    if orders.empty:
        ax.text(0.5, 0.5, "No order data", ha="center", va="center")
        ax.set_axis_off()
        return

    limit_orders = orders[orders["order_type"].str.upper() == "LIMIT"].copy()
    if limit_orders.empty:
        ax.text(0.5, 0.5, "No limit orders", ha="center", va="center")
        ax.set_axis_off()
        return

    limit_orders = limit_orders.sort_values("submit_time")
    limit_orders["filled"] = limit_orders["status"].str.upper() == "FILLED"
    limit_orders["cumulative_fill_rate"] = limit_orders["filled"].cumsum() / range(1, len(limit_orders) + 1)
    ax.plot(limit_orders["submit_time"], limit_orders["cumulative_fill_rate"], color="#54a24b")
    ax.set_title("Limit Fill Rate")
    ax.set_xlabel("Submit Time (epoch)")
    ax.set_ylabel("Cumulative Fill Rate")
    ax.set_ylim(0, 1)


def visualize(db_path: str, output: str) -> Path:
    """Generate a summary plot for simulator runs."""

    db_file = Path(db_path)
    if not db_file.exists():
        raise FileNotFoundError(f"Simulator DB not found at {db_path}")

    frames = _load_tables(db_file)
    fig, (ax_latency, ax_slippage, ax_fill_rate) = plt.subplots(1, 3, figsize=(15, 4))
    _plot_latency(ax_latency, frames.get("sim_orders", pd.DataFrame()))
    _plot_slippage(ax_slippage, frames.get("sim_fills", pd.DataFrame()))
    _plot_limit_fill_rate(ax_fill_rate, frames.get("sim_orders", pd.DataFrame()))

    fig.tight_layout()
    output_path = Path(output)
    fig.savefig(output_path)
    LOGGER.info("Saved visualization to %s", output_path)
    return output_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Visualize simulator SQLite outputs.")
    parser.add_argument("--db", required=True, help="Path to SQLite DB containing sim_orders/sim_fills.")
    parser.add_argument("--output", default="simulator_report.png", help="Path to save the generated plot.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    visualize(args.db, args.output)


if __name__ == "__main__":
    main()
