import copy
import unittest

from calculators.staged_entry import calculate


class StagedEntry(unittest.TestCase):
    def spec(self):
        return {"currency": "KRW", "budget": 1000000, "reference_basis": "before bonus",
                "stages": [{"budget": 600000, "reference_price": 40000,
                            "share_multiplier": 1, "condition": "model condition"}],
                "terminal_reference_prices": {"base": 50000}}

    def test_bonus_changes_units_not_value_or_profit(self):
        before = self.spec()
        after = copy.deepcopy(before)
        after["stages"][0]["share_multiplier"] = 2
        a, b = calculate(before), calculate(after)
        self.assertEqual(b["stages"][0]["quantity"], 2 * a["stages"][0]["quantity"])
        self.assertEqual(a["total_deployed"], b["total_deployed"])
        self.assertEqual(a["all_stages_arithmetic"], b["all_stages_arithmetic"])
        self.assertEqual(a["reserved_budget"], 400000)

    def test_combined_budget_and_currency_are_checked(self):
        spec = self.spec()
        spec["stages"] *= 2
        with self.assertRaises(ValueError):
            calculate(spec)
        spec = self.spec()
        spec["stages"][0]["currency"] = "USD"
        with self.assertRaises(ValueError):
            calculate(spec)

    def test_nonfinite_and_zero_multiplier_are_rejected(self):
        for invalid in (float("nan"), float("inf"), 0, -1, True):
            spec = self.spec()
            spec["stages"][0]["share_multiplier"] = invalid
            with self.assertRaises(ValueError):
                calculate(spec)


if __name__ == "__main__":
    unittest.main()
