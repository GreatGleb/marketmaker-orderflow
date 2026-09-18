"""Проверка состава и покрытия процентного парка без БД и Redis."""

from collections import Counter
from decimal import Decimal
from itertools import combinations, product
import unittest

from app.constants.demo_seed import percentage_bot_rows


class DemoSeedTests(unittest.TestCase):
    def setUp(self):
        self.rows = percentage_bot_rows(Decimal("0.001"))
        self.expected = {
            "start_updown_percents": [Decimal(x) / 1000 for x in (5, 10, 20, 30, 40, 50, 70, 100)],
            "stop_loss_percents": [Decimal(x) / 1000 for x in (20, 30, 40, 50, 60, 80, 150, 300, 600)],
            "stop_win_percents": [Decimal(x) / 1000 for x in (5, 10, 20, 40, 60, 100, 300)],
            "min_timeframe_asset_volatility": [Decimal(x) for x in ("0.5", "1", "2", "3")],
        }

    def test_exact_count_unique_and_percentage_only(self):
        self.assertEqual(len(self.rows), 2000)
        self.assertEqual(len({tuple(row[f] for f in self.expected) for row in self.rows}), 2000)
        for row in self.rows:
            self.assertEqual(row["symbol"], "")
            self.assertTrue(row["is_active"])
            for field in self.expected:
                self.assertGreater(row[field], 0)
            for field in (
                "stop_success_ticks", "stop_loss_ticks", "start_updown_ticks",
                "copy_bot_min_time_profitability_min", "copybot_v2_time_in_minutes",
                "copybot_v3_time_in_minutes",
            ):
                self.assertIsNone(row.get(field))

    def test_all_levels_balanced_including_extremes(self):
        for field, values in self.expected.items():
            with self.subTest(field=field):
                counts = Counter(row[field] for row in self.rows)
                self.assertEqual(set(counts), set(values))
                self.assertLessEqual(max(counts.values()) - min(counts.values()), 1)
                self.assertEqual(min(counts.values()), 2000 // len(values))

    def test_every_pair_of_levels_is_covered(self):
        for left, right in combinations(self.expected, 2):
            self.assertEqual(
                {(row[left], row[right]) for row in self.rows},
                set(product(self.expected[left], self.expected[right])),
            )

    def test_reproducible_and_scales_percentages_only(self):
        self.assertEqual(self.rows, percentage_bot_rows(Decimal("0.001")))
        scaled = percentage_bot_rows(Decimal("0.002"))
        for row, other in zip(self.rows, scaled):
            for field in self.expected:
                factor = 1 if field == "min_timeframe_asset_volatility" else 2
                self.assertEqual(other[field], row[field] * factor)

    def test_invalid_scale_rejected(self):
        for value in ("0", "-1", "NaN", "Infinity", "-Infinity"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                percentage_bot_rows(Decimal(value))


if __name__ == "__main__":
    unittest.main()
