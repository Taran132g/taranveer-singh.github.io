import sqlite3
import time
import threading
import json
import atexit
from pathlib import Path
from typing import Any

DB_PATH = "penny_basing.db"
POSITION_SIZE = 1000
SHORT_SIZE = 1000
SLIPPAGE = 0.001
COMMISSION = 0.0
STATE_FILE = "paper_trader_state.json"


class PaperTrader:
    """Paper-trading engine that reacts to EVERY alert from grok.py."""

    def __init__(self) -> None:
        self.load_state()
        self.last_alert_id = self._get_last_alert_id()
        self._init_db_schema()
        atexit.register(self.save_state)

    # --------------------------------------------------------------------- #
    # State Persistence
    # --------------------------------------------------------------------- #
    def load_state(self):
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.cash = float(data.get("cash", 100_000.0))
                    self.positions = data.get("positions", {})
                print(f"[PAPER] Loaded state: Cash ${self.cash:,.0f}, {len(self.positions)} positions")
            except Exception as e:
                print(f"[PAPER] Failed to load state: {e}")
                self.cash = 100_000.0
                self.positions = {}
        else:
            self.cash = 100_000.0
            self.positions = {}

    def save_state(self):
        state = {
            "cash": self.cash,
            "positions": self.positions
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print(f"[PAPER] State saved: Cash ${self.cash:,.0f}, {len(self.positions)} open")
        except Exception as e:
            print(f"[PAPER] Failed to save state: {e}")

    # --------------------------------------------------------------------- #
    # DB helpers
    # --------------------------------------------------------------------- #
    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db_schema(self) -> None:
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp REAL,
                    symbol TEXT,
                    side TEXT,
                    qty INTEGER,
                    price REAL,
                    slippage REAL,
                    commission REAL,
                    pnl REAL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    qty INTEGER,
                    entry_price REAL,
                    current_price REAL,
                    entry_time REAL,
                    pnl REAL,
                    pnl_percent REAL
                )
                """
            )
            cur.execute("PRAGMA table_info(paper_trades)")
            if "pnl" not in [row["name"] for row in cur.fetchall()]:
                cur.execute("ALTER TABLE paper_trades ADD COLUMN pnl REAL DEFAULT 0.0")
            conn.commit()

    def _get_last_alert_id(self) -> int:
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT MAX(rowid) FROM alerts")
            row = cur.fetchone()
            return row[0] if row and row[0] else 0

    def _get_current_price(self, symbol: str) -> float:
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT price FROM alerts WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
                (symbol,),
            )
            row = cur.fetchone()
            return float(row[0]) if row else 13.35

    # --------------------------------------------------------------------- #
    # Order Execution
    # --------------------------------------------------------------------- #
    def _execute_order(self, symbol: str, qty: int, price: float, side: str) -> None:
        issues = abs(qty) * price
        if qty > 0:  # BUY / COVER
            cost = issues * (1 + SLIPPAGE) + COMMISSION * qty
            if cost > self.cash:
                print(f"[PAPER] Not enough cash to {side} {abs(qty)} {symbol} @ {price:.3f}")
                return
            self.cash -= cost
        else:  # SELL / SHORT
            proceeds = issues * (1 - SLIPPAGE) - COMMISSION * abs(qty)
            self.cash += proceeds

        # Position update
        if symbol not in self.positions:
            self.positions[symbol] = {"qty": 0, "entry_price": price, "entry_time": time.time()}
        pos = self.positions[symbol]

        old_qty = pos["qty"]
        new_qty = old_qty + qty

        # Weighted average entry (same direction only)
        if old_qty == 0 or (old_qty * qty > 0):
            total_shares = abs(old_qty) + abs(qty)
            new_entry = (abs(old_qty) * pos["entry_price"] + abs(qty) * price) / total_shares
        else:
            new_entry = pos["entry_price"]

        pos.update(qty=new_qty, entry_price=new_entry, entry_time=time.time() if new_qty != old_qty else pos["entry_time"])

        if pos["qty"] == 0:
            del self.positions[symbol]

        self._log_trade(symbol, side, abs(qty), price)
        self._update_position_db(symbol)

    def _buy(self, symbol: str, qty: int, price: float) -> None:
        self._execute_order(symbol, qty, price, "BUY")

    def _sell(self, symbol: str, qty: int, price: float) -> None:
        self._execute_order(symbol, -qty, price, "SELL")

    def _short(self, symbol: str, qty: int, price: float) -> None:
        self._execute_order(symbol, -qty, price, "SHORT")

    def _cover(self, symbol: str, qty: int, price: float) -> None:
        self._execute_order(symbol, qty, price, "COVER")

    # --------------------------------------------------------------------- #
    # Logging & P&L
    # --------------------------------------------------------------------- #
    def _log_trade(self, symbol: str, side: str, qty: int, price: float) -> None:
        slippage_cost = qty * price * SLIPPAGE
        commission_cost = COMMISSION * qty

        # Realized P&L on closing legs
        pnl = 0.0
        if side in ("SELL", "COVER"):
            entry = self.positions.get(symbol, {}).get("entry_price", price)
            if side == "SELL":
                pnl = (price * (1 - SLIPPAGE) - COMMISSION) * qty - (entry * qty)
            else:  # COVER
                pnl = (entry * qty) - (price * (1 + SLIPPAGE) + COMMISSION) * qty

        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO paper_trades
                (timestamp, symbol, side, qty, price, slippage, commission, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (time.time(), symbol, side, qty, price, slippage_cost, commission_cost, pnl),
            )
            conn.commit()

        print(f"[PAPER] {side} {qty} {symbol} @ ${price:.3f} | Cash: ${self.cash:,.0f} | P&L: ${pnl:,.2f}")

    def _update_position_db(self, symbol: str) -> None:
        if symbol not in self.positions:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
                conn.commit()
            return

        pos = self.positions[symbol]
        cur_price = self._get_current_price(symbol)
        qty = pos["qty"]
        entry = pos["entry_price"]

        # CORRECT P&L: long = (current - entry) * qty; short = (entry - current) * |qty|
        if qty > 0:
            pnl = (cur_price - entry) * qty
        else:
            pnl = (entry - cur_price) * abs(qty)

        cost_basis = abs(qty) * entry
        pnl_pct = (pnl / cost_basis) * 100 if cost_basis > 0 else 0.0

        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR REPLACE INTO paper_positions
                (symbol, qty, entry_price, current_price, entry_time, pnl, pnl_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, qty, entry, cur_price, pos["entry_time"], pnl, pnl_pct),
            )
            conn.commit()

    # --------------------------------------------------------------------- #
    # Alert Monitor
    # --------------------------------------------------------------------- #
    def monitor_alerts(self) -> None:
        print("[PAPER] Monitoring ALL alerts – NO filters")
        while True:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT rowid, symbol, direction, price
                    FROM alerts
                    WHERE rowid > ?
                    ORDER BY timestamp ASC
                    """,
                    (self.last_alert_id,),
                )
                rows = cur.fetchall()

            for row in rows:
                alert_id, symbol, direction, price = row
                self.last_alert_id = alert_id

                pos = self.positions.get(symbol, {})
                current_qty = pos.get("qty", 0)

                if direction == "ask-heavy":
                    if current_qty > 0:
                        self._sell(symbol, current_qty, price)
                    self._short(symbol, SHORT_SIZE, price)

                elif direction == "bid-heavy":
                    if current_qty < 0:
                        self._cover(symbol, abs(current_qty), price)
                    self._buy(symbol, POSITION_SIZE, price)

            time.sleep(1)

    # --------------------------------------------------------------------- #
    # Start
    # --------------------------------------------------------------------- #
    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.monitor_alerts, daemon=True)
        thread.start()
        print(f"[PAPER] LIVE | Cash: ${self.cash:,.0f} | Long: {POSITION_SIZE} | Short: {SHORT_SIZE}")
        return thread


if __name__ == "__main__":
    trader = PaperTrader()
    trader.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[PAPER] Stopped.")
