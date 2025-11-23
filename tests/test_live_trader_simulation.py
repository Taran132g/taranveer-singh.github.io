import os
import sqlite3
import tempfile
import unittest

from live_trader import LiveTrader


class StubOrderExecutor:
    def __init__(self, *, status_sequence=None, quote=None, fail_cancel=False):
        self.dry_run = True
        self.submitted = []
        self.status_sequence = status_sequence or []
        self.sticky_status = None
        self.quote = quote or {"bidPrice": 0, "askPrice": 0, "lastPrice": 0}
        self.cancelled = []
        self.fail_cancel = fail_cancel

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
            "dry_run": False,
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
            "dry_run": False,
        }

    def fetch_order_status(self, order_id: str):
        if self.status_sequence:
            status = self.status_sequence.pop(0)
            self.sticky_status = status
            return status
        if self.sticky_status:
            return self.sticky_status
        return {"status": "FILLED", "filled_quantity": None, "raw": {"order_id": order_id}}

    def cancel_order(self, order_id: str):
        if self.fail_cancel:
            return False
        self.cancelled.append(order_id)
        return True

    def cancel_all_orders(self):
        return True

    def fetch_quote(self, symbol: str):
        return {"symbol": symbol, **self.quote}


class LiveTraderInlineFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test_alerts.db")
        self.state_path = os.path.join(self.tmpdir.name, "state.json")
        os.environ["DB_PATH"] = self.db_path
        os.environ["LIVE_POSITION_SIZE"] = "1000"
        os.environ["LIVE_SHORT_SIZE"] = "1000"
        os.environ["LIVE_LIMIT_SLIPPAGE_BPS"] = "10"
        os.environ["LIVE_PREFER_LIMIT_ORDERS"] = "1"
        os.environ["LIVE_LIMIT_FILL_TIMEOUT"] = "0.2"
        os.environ["LIVE_LIMIT_FILL_POLL_INTERVAL"] = "0.05"
        os.environ["LIVE_LIMIT_TIMEOUT_POLICY"] = "MARKET"
        os.environ["LIVE_STATE_FILE"] = self.state_path
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
            "LIVE_LIMIT_FILL_TIMEOUT",
            "LIVE_LIMIT_FILL_POLL_INTERVAL",
            "LIVE_LIMIT_TIMEOUT_POLICY",
            "LIVE_STATE_FILE",
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

    def test_limit_timeout_triggers_market_fallback(self):
        os.environ["LIVE_LIMIT_FILL_TIMEOUT"] = "0.05"
        os.environ["LIVE_LIMIT_FILL_POLL_INTERVAL"] = "0.01"
        os.environ["LIVE_LIMIT_TIMEOUT_POLICY"] = "MARKET"
        executor = StubOrderExecutor(status_sequence=[{"status": "WORKING", "filled_quantity": 0, "raw": {}}])
        trader = LiveTrader(dry_run=True, executor=executor)

        trader.process_alert(5, "FAST", "ask-heavy", 20.0)

        self.assertEqual([o["kind"] for o in executor.submitted], ["limit", "market"])
        self.assertIn("LIM-1", executor.cancelled)
        self.assertEqual(trader.positions.get("FAST"), -1000)

    def test_reference_price_refreshes_from_quote(self):
        quote = {"bidPrice": 11.9, "askPrice": 12.1, "lastPrice": 12.0}
        executor = StubOrderExecutor(quote=quote)
        trader = LiveTrader(dry_run=True, executor=executor)

        trader.process_alert(6, "REF", "ask-heavy", 10.0)

        with sqlite3.connect(os.environ["DB_PATH"]) as conn:
            cur = conn.cursor()
            cur.execute("SELECT price FROM live_orders ORDER BY id ASC LIMIT 1")
            recorded_price = cur.fetchone()[0]

        self.assertAlmostEqual(recorded_price, 11.988, places=3)

    def test_limit_padding_direction(self):
        base_price = 50.0
        buy_price = self.trader._aggressive_limit_price(side="BUY", reference_price=base_price)
        short_price = self.trader._aggressive_limit_price(side="SHORT", reference_price=base_price)

        self.assertGreater(buy_price, base_price)
        self.assertLess(short_price, base_price)


if __name__ == "__main__":
    unittest.main()
