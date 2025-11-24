import sqlite3
import tempfile
import unittest

from simulator import OrderStatus, SimulatedExecutor, SimulatorMetrics


class FakeMarketModel:
    def __init__(self, *, fill_sequence=None, latency: float = 0.0, slippage_bps: float = 1.0):
        self.fill_sequence = fill_sequence or []
        self.latency = latency
        self.slippage_bps = slippage_bps
        self.calls = 0

    def generate_latency(self) -> float:
        return self.latency

    def calculate_slippage(self, *, expected_price: float, side: str):
        fill_price = expected_price * (1 + self.slippage_bps / 10_000)
        return fill_price, self.slippage_bps

    def should_fill_limit_order(self, **_: object):
        if self.calls < len(self.fill_sequence):
            decision = self.fill_sequence[self.calls]
        else:
            decision = (False, 0.0)
        self.calls += 1
        return decision


class SimulatedExecutorTest(unittest.TestCase):
    def test_market_order_fills_immediately(self):
        model = FakeMarketModel(latency=0.0, slippage_bps=5.0)
        executor = SimulatedExecutor(model, lambda _: 100.0, sleep_func=lambda _: None)

        result = executor.place_order("ABC", "buy", 10, "market")

        self.assertEqual(result.status, OrderStatus.FILLED)
        self.assertAlmostEqual(result.avg_fill_price, 100.0 * (1 + 5.0 / 10_000))
        self.assertEqual(result.filled_quantity, 10)

    def test_limit_order_transitions_from_pending_to_filled(self):
        fill_sequence = [(False, 0.1), (True, 0.95)]
        model = FakeMarketModel(fill_sequence=fill_sequence, latency=0.0)
        executor = SimulatedExecutor(model, lambda _: 50.0, sleep_func=lambda _: None)

        initial = executor.place_order("XYZ", "sell", 20, "limit", limit_price=49.5)
        self.assertEqual(initial.status, OrderStatus.PENDING)

        polled = executor.get_order_status(initial.order_id)
        self.assertEqual(polled.status, OrderStatus.FILLED)
        self.assertEqual(polled.filled_quantity, 20)
        self.assertEqual(polled.avg_fill_price, 49.5)

    def test_partial_fill_then_completion(self):
        fill_sequence = [(True, 0.5), (True, 1.0)]
        model = FakeMarketModel(fill_sequence=fill_sequence, latency=0.0)
        executor = SimulatedExecutor(model, lambda _: 25.0, sleep_func=lambda _: None)

        first = executor.place_order("LMN", "buy", 40, "limit", limit_price=24.5)
        self.assertEqual(first.status, OrderStatus.PARTIALLY_FILLED)
        self.assertAlmostEqual(first.filled_quantity, 20.0)
        self.assertEqual(first.avg_fill_price, 24.5)

        second = executor.get_order_status(first.order_id)
        self.assertEqual(second.status, OrderStatus.FILLED)
        self.assertAlmostEqual(second.filled_quantity, 40.0)
        self.assertEqual(second.avg_fill_price, 24.5)

    def test_cancel_prevents_additional_fills(self):
        fill_sequence = [(False, 0.2), (True, 1.0)]
        model = FakeMarketModel(fill_sequence=fill_sequence, latency=0.0)
        executor = SimulatedExecutor(model, lambda _: 75.0, sleep_func=lambda _: None)

        placed = executor.place_order("CXL", "buy", 5, "limit", limit_price=75.0)
        self.assertEqual(placed.status, OrderStatus.PENDING)

        self.assertTrue(executor.cancel_order(placed.order_id))
        cancelled = executor.get_order_status(placed.order_id)
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)
        self.assertEqual(cancelled.filled_quantity, 0)

    def test_db_persistence_records_orders_fills_and_metrics(self):
        fill_sequence = [(True, 1.0)]
        model = FakeMarketModel(fill_sequence=fill_sequence, latency=0.0, slippage_bps=2.0)
        metrics = SimulatorMetrics()
        with tempfile.NamedTemporaryFile(delete=False) as temp_db:
            executor = SimulatedExecutor(
                model,
                lambda _: 100.0,
                sleep_func=lambda _: None,
                metrics=metrics,
                db_path=temp_db.name,
                session_id="test-session",
            )

            market_order = executor.place_order("ABC", "buy", 5, "market")
            self.assertEqual(market_order.status, OrderStatus.FILLED)

            limit_order = executor.place_order("XYZ", "sell", 10, "limit", limit_price=101.0)
            self.assertEqual(limit_order.status, OrderStatus.FILLED)

            with sqlite3.connect(temp_db.name) as conn:
                order_rows = conn.execute("SELECT COUNT(*) FROM sim_orders").fetchone()[0]
                fill_rows = conn.execute("SELECT COUNT(*) FROM sim_fills").fetchone()[0]
                metrics_rows = conn.execute("SELECT COUNT(*) FROM sim_metrics").fetchone()[0]

            self.assertEqual(order_rows, 2)
            self.assertGreaterEqual(fill_rows, 2)
            self.assertGreater(metrics_rows, 0)


if __name__ == "__main__":
    unittest.main()
