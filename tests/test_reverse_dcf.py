"""역산 DCF 산술과 계산 산출물 무결성."""

import json
import tempfile
import unittest
from pathlib import Path

from calculators.contracts import (
    ContractError,
    SCHEMA_VERSION,
    calculation_id_for,
    save_validated_calculation,
    validate_calculation,
)
from calculators.reverse_dcf import (
    MODEL,
    MODEL_VERSION,
    annualized_pct,
    equity_value_per_share,
    solve,
)

BASE = {
    "discount_rate": 0.10,
    "terminal_growth": 0.025,
    "years": 10,
    "net_debt": -30e9,
    "shares": 3.5e9,
}


def calculation():
    inputs = {"price": 100.0, "source": "fixture"}
    outputs = {
        "scenarios": {
            # 이름이 단위를 말한다. 계약으로 가는 값은 누적이고 연율은 참고치다.
            "bear": {"total_return_pct": -20.0, "annualized_return_pct": -10.6},
            "base": {"total_return_pct": 10.0, "annualized_return_pct": 4.9},
            "bull": {"total_return_pct": 45.0, "annualized_return_pct": 20.4},
        }
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "calculation_id": calculation_id_for(
            MODEL, MODEL_VERSION, inputs, outputs
        ),
        "model": MODEL,
        "model_version": MODEL_VERSION,
        "instrument_id": "TSLA",
        "created_at": "2026-07-28T10:00:00+09:00",
        "source_ids": ["FUND-fixture"],
        "inputs": inputs,
        "outputs": outputs,
    }


class TestModel(unittest.TestCase):
    def test_value_matches_hand_calculation(self):
        value = equity_value_per_share(3.6e9, 0.468, **BASE)
        self.assertAlmostEqual(value, 330.0, delta=1.0)

    def test_value_increases_with_growth(self):
        values = [
            equity_value_per_share(3.6e9, growth / 100, **BASE)
            for growth in range(0, 60, 5)
        ]
        self.assertEqual(values, sorted(values))

    def test_solve_recovers_growth(self):
        price = equity_value_per_share(3.6e9, 0.22, **BASE)
        answer, status = solve(
            price,
            -0.95,
            2.0,
            lambda growth: equity_value_per_share(3.6e9, growth, **BASE),
        )
        self.assertEqual(status, "SOLVED")
        self.assertAlmostEqual(answer, 0.22, places=4)

    def test_annualized_return(self):
        self.assertAlmostEqual(
            annualized_pct(144.0, 100.0, 24), 20.0, places=6
        )
        self.assertEqual(annualized_pct(-5.0, 100.0, 12), -100.0)


class TestCalculationContract(unittest.TestCase):
    def test_valid_calculation_passes(self):
        self.assertEqual(validate_calculation(calculation()), [])

    def test_edited_input_breaks_fingerprint(self):
        data = calculation()
        data["inputs"]["price"] = 101.0
        self.assertTrue(
            any("지문과 다르다" in error for error in validate_calculation(data))
        )

    def test_invalid_calculation_is_not_saved(self):
        data = calculation()
        data["calculation_id"] = "CALC-deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContractError):
                save_validated_calculation(tmp, data)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_saved_calculation_uses_content_id(self):
        data = calculation()
        with tempfile.TemporaryDirectory() as tmp:
            path = save_validated_calculation(tmp, data)
            self.assertEqual(path.name, f"{data['calculation_id']}.json")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["model"], MODEL
            )


if __name__ == "__main__":
    unittest.main()
