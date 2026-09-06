import unittest

from atsuite.recommender.normalize import CurrencyNormalizer, min_max_normalize


class NormalizeTests(unittest.TestCase):
    def test_min_max(self):
        self.assertEqual(min_max_normalize({"a": 1, "b": 3, "c": 2, "d": None}), {"a": 1, "b": 0, "c": 0.5})
        self.assertEqual(min_max_normalize({"a": 2, "b": 2}), {"a": 1, "b": 1})
        self.assertEqual(min_max_normalize({"a": None}), {})

    def test_currency(self):
        self.assertAlmostEqual(CurrencyNormalizer({"CNY": 0.14}).to_usd(10, "CNY").value, 1.4)
        self.assertEqual(CurrencyNormalizer().to_usd(0, "USD").value, 0)
        self.assertIsNone(CurrencyNormalizer().to_usd(0, "CNY").value)
        for rates in ({"USD": 2}, {"CNY": 0}, {"CNY": float("nan")}, {"CNY": True}):
            with self.assertRaises(ValueError):
                CurrencyNormalizer(rates)
