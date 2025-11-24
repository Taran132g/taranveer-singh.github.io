import unittest

from simulator import OrderResult, OrderStatus, SimulatorMetrics


class SimulatorMetricsTest(unittest.TestCase):
    def test_records_and_summarizes_latency_and_slippage(self):
        metrics = SimulatorMetrics()
        order1 = OrderResult(
            order_id="1",
            symbol="ABC",
            side="BUY",
            quantity=10,
            order_type="MARKET",
            status=OrderStatus.FILLED,
            filled_quantity=10,
            avg_fill_price=100.0,
            timestamp=0.0,
        )
        metrics.record_order(order1, latency_seconds=0.1, slippage_bps=1.5, is_limit=False)

        order2 = OrderResult(
            order_id="2",
            symbol="XYZ",
            side="SELL",
            quantity=5,
            order_type="LIMIT",
            status=OrderStatus.PENDING,
            filled_quantity=0,
            avg_fill_price=None,
            timestamp=0.0,
        )
        metrics.record_order(order2, latency_seconds=None, slippage_bps=None, is_limit=True)

        order2_filled = OrderResult(
            order_id="2",
            symbol="XYZ",
            side="SELL",
            quantity=5,
            order_type="LIMIT",
            status=OrderStatus.FILLED,
            filled_quantity=5,
            avg_fill_price=10.0,
            timestamp=0.0,
        )
        metrics.record_order(order2_filled, latency_seconds=0.2, slippage_bps=0.0, is_limit=True)

        summary = metrics.get_summary()

        self.assertEqual(summary["total_orders"], 2)
        self.assertEqual(summary["filled_orders"], 2)
        self.assertEqual(summary["limit_orders_placed"], 1)
        self.assertEqual(summary["limit_orders_filled"], 1)
        self.assertGreater(summary["latency_mean_ms"], 0)
        self.assertAlmostEqual(summary["slippage_mean_bps"], 0.75)

    def test_reset_clears_state(self):
        metrics = SimulatorMetrics()
        order = OrderResult(
            order_id="1",
            symbol="ABC",
            side="BUY",
            quantity=1,
            order_type="MARKET",
            status=OrderStatus.FILLED,
            filled_quantity=1,
            avg_fill_price=1.0,
            timestamp=0.0,
        )
        metrics.record_order(order, latency_seconds=0.05, slippage_bps=0.2, is_limit=False)
        metrics.reset()

        summary = metrics.get_summary()
        self.assertEqual(summary["total_orders"], 0)
        self.assertEqual(summary["filled_orders"], 0)
        self.assertEqual(summary["latency_mean_ms"], 0.0)
        self.assertEqual(summary["slippage_mean_bps"], 0.0)


if __name__ == "__main__":
    unittest.main()
