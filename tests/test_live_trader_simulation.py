import os
import sqlite3
import tempfile
import unittest

from live_trader import LiveTrader


class StubOrderExecutor:
    def __init__(self):
        self.dry_run = True
        self.submitted = []

    def submit_limit(self, *, symbol: str, qty: int, side: str, limit_price: float):
        order = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "price": limit_price,
            "kind": "limit",
        }
        self.submitted.append(order)
        return {
            "order_id": f"LIM-{len(self.submitted)}",
            "status_code": "201",
            "dry_run": True,
        }

    def submit_market(self, *, symbol: str, qty: int, side: str):
        order = {
            "symbol": symbol,
            "qty": qty,
            "side": side,
            "kind": "market",
        }
        self.submitted.append(order)
        return {
            "order_id": f"MKT-{len(self.submitted)}",
            "status_code": "201",
            "dry_run": True,
        }

    def cancel_all_orders(self):
        return True


class LiveTraderInlineFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_alerts.db")
        os.environ["DB_PATH"] = self.db_path
        os.environ["LIVE_POSITION_SIZE"] = "1000"
        os.environ["LIVE_SHORT_SIZE"] = "1000"
        os.environ["LIVE_LIMIT_SLIPPAGE_BPS"] = "10"
        os.environ["LIVE_PREFER_LIMIT_ORDERS"] = "1"
        self.executor = StubOrderExecutor()
        self.trader = LiveTrader(dry_run=True, executor=self.executor)

    def tearDown(self):
        self.tmpdir.cleanup()
        for key in [
            "DB_PATH",
            "LIVE_POSITION_SIZE",
            "LIVE_SHORT_SIZE",
            "LIVE_LIMIT_SLIPPAGE_BPS",
            "LIVE_PREFER_LIMIT_ORDERS",
        ]:
            os.environ.pop(key, None)

    def test_inline_dispatch_processes_and_records_orders(self):
        # Ask-heavy alert should trigger a short with padded limit price.
        self.trader.process_alert(1, "TEST", "ask-heavy", 10.0)
        self.assertEqual(self.trader.positions.get("TEST"), -1000)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM live_orders")
            order_count = cur.fetchone()[0]
            cur.execute("SELECT price FROM live_orders ORDER BY id ASC LIMIT 1")
            recorded_price = cur.fetchone()[0]

        self.assertEqual(order_count, 1)
        self.assertAlmostEqual(recorded_price, 9.99, places=4)

        # Bid-heavy alert should cover the short and open a long position.
        self.trader.process_alert(2, "TEST", "bid-heavy", 10.2)
        self.assertEqual(self.trader.positions.get("TEST"), 1000)

        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM live_orders")
            total_orders = cur.fetchone()[0]
            cur.execute("SELECT price FROM live_orders ORDER BY id DESC LIMIT 1")
            last_price = cur.fetchone()[0]

        self.assertEqual(total_orders, 3)
        self.assertAlmostEqual(last_price, 10.2102, places=4)

    def test_limit_padding_direction(self):
        base_price = 50.0
        buy_price = self.trader._aggressive_limit_price(side="BUY", reference_price=base_price)
        short_price = self.trader._aggressive_limit_price(side="SHORT", reference_price=base_price)

        self.assertGreater(buy_price, base_price)
        self.assertLess(short_price, base_price)


if __name__ == "__main__":
    unittest.main()
