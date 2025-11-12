import sqlite3
import time
import threading

DB_PATH = "penny_basing.db"
POSITION_SIZE = 1000  # shares per trade
SLIPPAGE = 0.001      # 0.1% slippage
COMMISSION = 0.0      # per share


class PaperTrader:
    """Paper trading engine that reacts to alerts in ``penny_basing.db``."""

    def __init__(self) -> None:
        self.positions: dict[str, dict[str, float]] = {}
        self.cash = 100000.0
        self.equity = self.cash
        self.trades: list[dict[str, float]] = []
        self.db = sqlite3.connect(DB_PATH)
        self.init_db()
        self.last_alert_id = self.get_last_alert_id()

    def init_db(self) -> None:
        """Ensure the paper trading tables exist."""
        cursor = self.db.cursor()
        cursor.execute(
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
        cursor.execute(
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
        self.db.commit()

    def get_last_alert_id(self) -> int:
        """Return the last processed alert id."""
        cursor = self.db.cursor()
        cursor.execute("SELECT MAX(rowid) FROM alerts")
        row = cursor.fetchone()
        return row[0] if row and row[0] else 0

    def get_current_price(self, symbol: str) -> float:
        """Fetch the latest price for ``symbol`` from the alerts table."""
        cursor = self.db.cursor()
        cursor.execute(
            "SELECT price FROM alerts WHERE symbol=? ORDER BY timestamp DESC LIMIT 1",
            (symbol,),
        )
        row = cursor.fetchone()
        return row[0] if row else 13.35

    def buy(self, symbol: str, qty: int, price: float) -> None:
        cost = qty * price * (1 + SLIPPAGE) + COMMISSION * qty
        if cost > self.cash:
            print(f"[PAPER] Not enough cash for {qty} {symbol} @ {price}")
            return
        self.cash -= cost
        if symbol in self.positions:
            pos = self.positions[symbol]
            new_qty = pos["qty"] + qty
            new_entry = (pos["qty"] * pos["entry_price"] + qty * price) / new_qty
            self.positions[symbol] = {
                "qty": new_qty,
                "entry_price": new_entry,
                "entry_time": time.time(),
            }
        else:
            self.positions[symbol] = {
                "qty": qty,
                "entry_price": price,
                "entry_time": time.time(),
            }
        self.log_trade(symbol, "BUY", qty, price)
        self.update_position_db(symbol)

    def sell(self, symbol: str, qty: int, price: float) -> None:
        if symbol not in self.positions or self.positions[symbol]["qty"] < qty:
            print(f"[PAPER] Not enough {symbol} to sell {qty}")
            return
        proceeds = qty * price * (1 - SLIPPAGE) - COMMISSION * qty
        self.cash += proceeds
        pos = self.positions[symbol]
        pos["qty"] -= qty
        if pos["qty"] <= 0:
            del self.positions[symbol]
        self.log_trade(symbol, "SELL", qty, price)
        self.update_position_db(symbol)

    def log_trade(self, symbol: str, side: str, qty: int, price: float) -> None:
        slippage_cost = qty * price * SLIPPAGE
        commission_cost = COMMISSION * qty
        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT INTO paper_trades
            (timestamp, symbol, side, qty, price, slippage, commission)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (time.time(), symbol, side, qty, price, slippage_cost, commission_cost),
        )
        self.db.commit()
        print(f"[PAPER] {side} {qty} {symbol} @ ${price:.3f} | Cash: ${self.cash:,.0f}")

    def update_position_db(self, symbol: str) -> None:
        if symbol not in self.positions:
            cursor = self.db.cursor()
            cursor.execute("DELETE FROM paper_positions WHERE symbol=?", (symbol,))
            self.db.commit()
            return

        pos = self.positions[symbol]
        current_price = self.get_current_price(symbol)
        market_value = pos["qty"] * current_price
        pnl = market_value - (pos["qty"] * pos["entry_price"])
        pnl_pct = (
            (pnl / (pos["qty"] * pos["entry_price"])) * 100
            if pos["entry_price"] > 0
            else 0
        )

        cursor = self.db.cursor()
        cursor.execute(
            """
            INSERT OR REPLACE INTO paper_positions
            (symbol, qty, entry_price, current_price, entry_time, pnl, pnl_percent)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol,
                pos["qty"],
                pos["entry_price"],
                current_price,
                pos["entry_time"],
                pnl,
                pnl_pct,
            ),
        )
        self.db.commit()

    def monitor_alerts(self) -> None:
        print("[PAPER] Monitoring alerts for auto-trading...")
        while True:
            cursor = self.db.cursor()
            cursor.execute(
                """
                SELECT rowid, symbol, direction, price, ratio, heavy_venues
                FROM alerts
                WHERE rowid > ?
                ORDER BY timestamp ASC
                """,
                (self.last_alert_id,),
            )
            rows = cursor.fetchall()
            for row in rows:
                alert_id, symbol, direction, price, ratio, venues = row
                self.last_alert_id = alert_id

                if direction == "bid-heavy" and ratio >= 2.0 and venues >= 4:
                    self.buy(symbol, POSITION_SIZE, price)
                elif direction == "ask-heavy" and ratio >= 2.0 and venues >= 4:
                    if symbol in self.positions:
                        qty = self.positions[symbol]["qty"]
                        self.sell(symbol, qty, price)

            time.sleep(1)

    def start(self) -> threading.Thread:
        thread = threading.Thread(target=self.monitor_alerts, daemon=True)
        thread.start()
        print(
            f"[PAPER] Paper trader started | Cash: ${self.cash:,.0f} | Size: {POSITION_SIZE} shares"
        )
        return thread


if __name__ == "__main__":
    trader = PaperTrader()
    trader.start()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[PAPER] Stopped.")
