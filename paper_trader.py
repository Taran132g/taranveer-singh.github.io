# paper_trader.py
import sqlite3
import time
import threading
import json
import atexit
from pathlib import Path

DB_PATH = "penny_basing.db"
POSITION_SIZE = 1000        # Long size
SHORT_SIZE = 1000           # Short size
SLIPPAGE = 0.001
COMMISSION = 0.0
STATE_FILE = "paper_trader_state.json"


class PaperTrader:
    """Paper trading engine — FLIP-ONLY version.
    Only flips when signal direction changes. No stacking positions.
    """

    def __init__(self) -> None:
        self.load_state()
        self.last_alert_id = self._get_last_alert_id()
        self._init_db_schema()

        # === Daily PnL tracking ===
        self.daily_pnl = 0.0
        self.daily_date = time.strftime("%Y-%m-%d")

        atexit.register(self.save_state)

    # ============================================================
    # State Persistence
    # ============================================================
    def load_state(self):
        if Path(STATE_FILE).exists():
            try:
                with open(STATE_FILE, "r") as f:
                    data = json.load(f)
                    self.cash = float(data.get("cash", 100_000.0))
                    self.positions = data.get("positions", {})
                print(f"[PAPER] Loaded state: Cash ${self.cash:,.2f}, {len(self.positions)} positions", flush=True)
            except:
                self.cash = 100_000.0
                self.positions = {}
        else:
            self.cash = 100_000.0
            self.positions = {}

    def save_state(self):
        state = {"cash": self.cash, "positions": self.positions}
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f, indent=2)
            print(f"[PAPER] State saved.", flush=True)
        except Exception as e:
            print(f"[PAPER] Failed to save state: {e}", flush=True)

    # ============================================================
    # DB
    # ============================================================
    def _open_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db_schema(self) -> None:
        with self._open_conn() as conn:
            cur = conn.cursor()

            cur.execute("""
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
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS paper_positions (
                    symbol TEXT PRIMARY KEY,
                    qty INTEGER,
                    entry_price REAL,
                    current_price REAL,
                    entry_time REAL,
                    pnl REAL,
                    pnl_percent REAL
                )
            """)

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

    # ============================================================
    # Order Wrapper Functions (required)
    # ============================================================
    def _buy(self, symbol, qty, price):   self._execute_order(symbol, qty, price, "BUY")
    def _sell(self, symbol, qty, price):  self._execute_order(symbol, -qty, price, "SELL")
    def _short(self, symbol, qty, price): self._execute_order(symbol, -qty, price, "SHORT")
    def _cover(self, symbol, qty, price): self._execute_order(symbol, qty, price, "COVER")

    # ============================================================
    # Order Execution
    # ============================================================
    def _execute_order(self, symbol, qty, price, side):
        notional = abs(qty) * price

        # Cash movements
        if qty > 0:  # Buy/Cover
            cost = notional * (1 + SLIPPAGE)
            if cost > self.cash:
                print(f"[PAPER] Not enough cash for {side}", flush=True)
                return
            self.cash -= cost
        else:  # Sell/Short
            proceeds = notional * (1 - SLIPPAGE)
            self.cash += proceeds

        # Old position values
        old_pos = self.positions.get(symbol, {"qty": 0, "entry_price": price})
        old_qty = old_pos["qty"]
        old_entry = old_pos["entry_price"]

        # Create new position if needed
        if symbol not in self.positions:
            self.positions[symbol] = {"qty": 0, "entry_price": price, "entry_time": time.time()}

        pos = self.positions[symbol]
        pos["qty"] = pos["qty"] + qty

        # Weighted average entry for same-direction adds
        if pos["qty"] != 0 and (old_qty * qty > 0):
            total = abs(old_qty) + abs(qty)
            pos["entry_price"] = (
                (abs(old_qty) * old_entry + abs(qty) * price) / total
            )

        # Log trade + PnL
        self._log_trade(symbol, side, abs(qty), price, old_entry, old_qty)

        # If back to flat remove position
        if pos["qty"] == 0:
            del self.positions[symbol]

        self._update_position_db(symbol, cur_price=price)

    # ============================================================
    # Daily PnL reset
    # ============================================================
    def _reset_daily_pnl_if_needed(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.daily_date:
            self.daily_date = today
            self.daily_pnl = 0.0
            print(f"[PAPER] New day detected — Daily PnL reset.", flush=True)

    # ============================================================
    # Trade Logging + PNL Calculation
    # ============================================================
    def _log_trade(self, symbol, side, qty, price, entry_price, old_qty):

        # Correct realized PnL
        pnl = 0.0

        # Closing a LONG
        if old_qty > 0 and side == "SELL":
            pnl = (price - entry_price) * qty

        # Closing a SHORT
        elif old_qty < 0 and side == "COVER":
            pnl = (entry_price - price) * qty

        # === Daily PnL update ===
        self._reset_daily_pnl_if_needed()

        if (old_qty > 0 and side == "SELL") or (old_qty < 0 and side == "COVER"):
            self.daily_pnl += pnl

        # Save daily pnl so UI can read it
        with open("daily_pnl.txt", "w") as f:
            f.write(str(self.daily_pnl))

        # Log to DB
        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO paper_trades
                (timestamp, symbol, side, qty, price, slippage, commission, pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (time.time(), symbol, side, qty, price,
                qty * price * SLIPPAGE, COMMISSION, pnl))
            conn.commit()

        # === Enhanced log with daily PnL ===
        print(
            f"[PAPER] {side} {qty} {symbol} @ ${price:.3f} | "
            f"Cash: ${self.cash:,.2f} | Trade PnL: ${pnl:.2f} | Daily PnL: ${self.daily_pnl:.2f}",
            flush=True
        )

    # ============================================================
    # Update Position Table
    # ============================================================
    def _update_position_db(self, symbol, cur_price=None):
        if symbol not in self.positions:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
                conn.commit()
            return

        pos = self.positions[symbol]
        cur_price = cur_price if cur_price is not None else self._get_current_price(symbol)

        qty = pos["qty"]
        entry = pos["entry_price"]

        if qty > 0:
            pnl = (cur_price - entry) * qty
        else:
            pnl = (entry - cur_price) * abs(qty)

        cost_basis = abs(qty) * entry
        pnl_pct = (pnl / cost_basis) * 100 if cost_basis != 0 else 0

        with self._open_conn() as conn:
            cur = conn.cursor()
            cur.execute("""
                INSERT OR REPLACE INTO paper_positions
                (symbol, qty, entry_price, current_price, entry_time, pnl, pnl_percent)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (symbol, qty, entry, cur_price, pos["entry_time"], pnl, pnl_pct))
            conn.commit()

    # ============================================================
    # FLIP-ONLY ALERT LOGIC
    # ============================================================
    def monitor_alerts(self):
        print("[PAPER] Monitoring alerts (Flip-Only Mode)…", flush=True)

        # Sleep is adaptive: immediately after seeing alerts we use the 50ms
        # minimum to react quickly; consecutive empty polls (no rows newer
        # than last_alert_id) double the wait time up to a 2s ceiling to
        # avoid hot loops when idle. To avoid sleeping through a new alert
        # that arrives just after a poll, we watch for DB file writes during
        # the longer idle backoff and wake early when the file mtime changes.
        # Fast-path latency target when alerts are flowing. Compared to the
        # original fixed 1s poll, a new alert is usually seen within ~50ms
        # after activity or ~10ms during idle file-probing.
        min_sleep = 0.05

        # While idling we probe the DB file mtime on a tighter cadence so a
        # newly written alert is noticed within ~10ms even if the outer backoff
        # has grown toward the 2s ceiling.
        mtime_probe = 0.01

        max_sleep = 2.0    # back off to 2s when idle
        idle_sleep = min_sleep
        db_path = Path(DB_PATH)
        last_db_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0

        while True:
            with self._open_conn() as conn:
                cur = conn.cursor()
                cur.execute("""
                    SELECT rowid, symbol, direction, price
                    FROM alerts
                    WHERE rowid > ?
                    ORDER BY rowid ASC
                """, (self.last_alert_id,))
                rows = cur.fetchall()

            for row in rows:
                alert_id, symbol, direction, price = row
                self.last_alert_id = alert_id

                pos = self.positions.get(symbol, {})
                current_qty = pos.get("qty", 0)

                # ASK-HEAVY → SHORT
                if direction == "ask-heavy":
                    if current_qty > 0:  # flip long → short
                        self._sell(symbol, current_qty, price)
                        self._short(symbol, SHORT_SIZE, price)
                    elif current_qty == 0:  # open new short
                        self._short(symbol, SHORT_SIZE, price)

                # BID-HEAVY → LONG
                elif direction == "bid-heavy":
                    if current_qty < 0:  # flip short → long
                        self._cover(symbol, abs(current_qty), price)
                        self._buy(symbol, POSITION_SIZE, price)
                    elif current_qty == 0:  # open new long
                        self._buy(symbol, POSITION_SIZE, price)

                # Update current price and PnL even when no trade is executed
                self._update_position_db(symbol, cur_price=price)

            activity_detected = bool(rows)

            # Track DB file changes so we can wake early from long sleeps.
            db_mtime_snapshot = db_path.stat().st_mtime if db_path.exists() else last_db_mtime

            if activity_detected:
                # Fresh alerts observed → use minimum sleep for quick response.
                idle_sleep = min_sleep
                last_db_mtime = db_mtime_snapshot
                time.sleep(idle_sleep)
                continue

            # No alerts observed → back off exponentially up to the ceiling,
            # but poll the DB mtime every few milliseconds so we can break out
            # quickly if new alerts are inserted right after the query.
            target_sleep = min(idle_sleep * 2, max_sleep)
            wake_deadline = time.monotonic() + target_sleep
            woke_for_write = False

            while time.monotonic() < wake_deadline:
                time.sleep(mtime_probe)
                current_mtime = db_path.stat().st_mtime if db_path.exists() else 0.0
                if current_mtime != db_mtime_snapshot:
                    woke_for_write = True
                    db_mtime_snapshot = current_mtime
                    break

            last_db_mtime = db_mtime_snapshot
            idle_sleep = min_sleep if woke_for_write else target_sleep

            # If a write occurred, immediately loop to fetch new alerts; if not,
            # we've already slept the full backoff duration via the wake loop.
            if woke_for_write:
                continue

    # ============================================================
    # Start Trader Thread
    # ============================================================
    def start(self):
        t = threading.Thread(target=self.monitor_alerts, daemon=True)
        t.start()
        print(f"[PAPER] LIVE (Flip-Only Mode) | Cash: ${self.cash:,.2f}", flush=True)
        return t


if __name__ == "__main__":
    trader = PaperTrader()
    trader.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[PAPER] Stopped.", flush=True)
