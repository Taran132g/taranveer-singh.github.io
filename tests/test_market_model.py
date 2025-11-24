import random
import unittest

from simulator import MarketModel, SimulationConfig


class MarketModelTest(unittest.TestCase):
    def test_latency_samples_are_non_negative(self):
        config = SimulationConfig(latency_mean_ms=50, latency_std_ms=10)
        model = MarketModel(config, rng=random.Random(42))

        samples = [model.generate_latency() for _ in range(20)]

        self.assertTrue(all(latency >= 0 for latency in samples))
        self.assertGreater(sum(samples) / len(samples), 0)

    def test_slippage_applies_in_correct_direction(self):
        config = SimulationConfig(slippage_min_bps=0.0, slippage_max_bps=3.0)

        buy_model = MarketModel(config, rng=random.Random(1))
        buy_fill, buy_bps = buy_model.calculate_slippage(expected_price=100.0, side="buy")
        self.assertGreater(buy_fill, 100.0)
        self.assertAlmostEqual(buy_fill, 100.0 * (1 + buy_bps / 10_000), places=6)

        sell_model = MarketModel(config, rng=random.Random(1))
        sell_fill, sell_bps = sell_model.calculate_slippage(expected_price=100.0, side="sell")
        self.assertLess(sell_fill, 100.0)
        self.assertAlmostEqual(sell_fill, 100.0 * (1 - sell_bps / 10_000), places=6)

    def test_limit_fill_probability_moves_with_aggression(self):
        config = SimulationConfig(limit_fill_base_probability=0.5)
        model = MarketModel(config, rng=random.Random(5))

        base_prob = model.limit_fill_probability(
            current_price=100.0,
            limit_price=100.0,
            order_size=100,
            typical_volume=10_000,
            elapsed_seconds=0.5,
            side="buy",
        )
        aggressive_prob = model.limit_fill_probability(
            current_price=100.0,
            limit_price=101.0,
            order_size=100,
            typical_volume=10_000,
            elapsed_seconds=0.5,
            side="buy",
        )

        self.assertGreater(aggressive_prob, base_prob)

    def test_limit_fill_decision_is_deterministic_with_seed(self):
        config = SimulationConfig(limit_fill_base_probability=0.7)
        model = MarketModel(config, rng=random.Random(3))

        filled, probability = model.should_fill_limit_order(
            current_price=50.0,
            limit_price=50.1,
            order_size=200,
            typical_volume=10_000,
            elapsed_seconds=3.0,
            side="buy",
        )

        self.assertTrue(filled)
        self.assertGreater(probability, 0.2)

    def test_invalid_inputs_raise(self):
        config = SimulationConfig()
        model = MarketModel(config, rng=random.Random(2))

        with self.assertRaises(ValueError):
            model.calculate_slippage(expected_price=-1, side="buy")
        with self.assertRaises(ValueError):
            model.calculate_slippage(expected_price=1, side="invalid")
        with self.assertRaises(ValueError):
            model.limit_fill_probability(
                current_price=0,
                limit_price=1,
                order_size=1,
                typical_volume=1,
                elapsed_seconds=0,
                side="buy",
            )
        with self.assertRaises(ValueError):
            SimulationConfig(slippage_min_bps=5, slippage_max_bps=4).validate()


if __name__ == "__main__":
    unittest.main()
