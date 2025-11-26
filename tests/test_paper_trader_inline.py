import os
import sqlite3
import tempfile
import unittest

import paper_trader


class PaperTraderInlineTest(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "paper_alerts.db")
        self.state_path = os.path.join(self.tmpdir.name, "state.json")

        # Patch module-level paths so each test is isolated.
        self._orig_db_path = paper_trader.DB_PATH
        self._orig_state_file = paper_trader.STATE_FILE
        paper_trader.DB_PATH = self.db_path
        paper_trader.STATE_FILE = self.state_path

        self.trader = paper_trader.PaperTrader()

    def tearDown(self):
        paper_trader.DB_PATH = self._orig_db_path
        paper_trader.STATE_FILE = self._orig_state_file
        self.tmpdir.cleanup()
        if os.path.exists(self.state_path):
            os.remove(self.state_path)

    def _fetch_all(self, query, *, column=0):
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(query)
            return [row[column] for row in cur.fetchall()]

    def test_process_alert_records_seen_price_and_position(self):
        self.trader.process_alert(1, "TEST", "ask-heavy", 12.34)

        seen_prices = self._fetch_all(
            "SELECT price FROM paper_seen_prices ORDER BY id ASC", column=0
        )
        positions = self._fetch_all(
            "SELECT qty FROM paper_positions WHERE symbol='TEST'", column=0
        )

        self.assertAlmostEqual(seen_prices[0], 12.34, places=4)
        self.assertEqual(self.trader.positions.get("TEST", {}).get("qty"), -paper_trader.SHORT_SIZE)
        self.assertEqual(positions[0], -paper_trader.SHORT_SIZE)

    def test_seen_price_records_even_without_flip(self):
        # First alert opens a short position.
        self.trader.process_alert(1, "TEST", "ask-heavy", 10.0)
        # Second alert with the same direction should not flip, but should still log price.
        self.trader.process_alert(2, "TEST", "ask-heavy", 9.5)

        seen_prices = self._fetch_all(
            "SELECT price FROM paper_seen_prices ORDER BY id ASC", column=0
        )

        self.assertEqual(len(seen_prices), 2)
        self.assertAlmostEqual(seen_prices[0], 10.0, places=4)
        self.assertAlmostEqual(seen_prices[1], 9.5, places=4)


if __name__ == "__main__":
    unittest.main()
