"""역산 DCF 산술과 계산 산출물 무결성."""

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from calculators.contracts import (
    ContractError,
    SCHEMA_VERSION,
    calculation_id_for,
    save_validated_calculation,
    validate_calculation,
)
from test_fundamentals import valid_fundamentals
from calculators.reverse_dcf import (
    MODEL,
    MODEL_VERSION,
    annualized_pct,
    equity_value_per_share,
    cmd_implied,
    resolve_inputs,
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


class TestValuationShareBasis(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "fundamentals.json"

    def arguments(self, **overrides):
        args = dict(
            fundamentals=str(self.path), instrument="TSLA", shares=None,
            shares_reason=None, fcf=None, net_debt=None, price=100,
            revenue=None, fcf_margin=None, inputs_unverified_reason=None,
            discount_rate=10, terminal_growth=2, years=10,
            solve_for="growth", json=True,
        )
        args.update(overrides)
        return SimpleNamespace(**args)

    def write_snapshot(self, **overrides):
        snapshot = valid_fundamentals(**overrides)
        self.path.write_text(json.dumps(snapshot), encoding="utf-8")
        return snapshot

    def test_eps_or_unspecified_share_basis_requires_explicit_selection(self):
        variants = (
            {},  # Older snapshots remain readable, but their basis is not known.
            {"tags": {"shares": "WeightedAverageNumberOfDilutedSharesOutstanding"}},
            {"share_count_basis": {"kind": "WEIGHTED_AVERAGE_DILUTED_EPS",
                                    "is_current_outstanding": False}},
        )
        for variant in variants:
            with self.subTest(variant=variant):
                self.write_snapshot(**variant)
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    with self.assertRaises(SystemExit):
                        resolve_inputs(self.arguments())
                self.assertIn("--shares-reason", errors.getvalue())

    def test_explicit_choice_keeps_snapshot_financials_and_distinct_provenance(self):
        snapshot = self.write_snapshot(share_count_basis={
            "kind": "WEIGHTED_AVERAGE_DILUTED_EPS", "is_current_outstanding": False,
        })
        args = self.arguments(shares=4e9, shares_reason="모형 예시: 기준일 이후 발행을 반영")
        fcf, provenance = resolve_inputs(args)
        self.assertEqual(fcf, snapshot["values"]["fcf_ttm"])
        self.assertEqual(args.net_debt, snapshot["values"]["net_debt"])
        self.assertEqual(args.shares, 4e9)
        self.assertEqual(provenance["fundamentals_id"], snapshot["fundamentals_id"])
        self.assertFalse(provenance["verified"])
        self.assertEqual(provenance["share_count"]["origin"], "MODEL_CHOICE")
        self.assertEqual(provenance["share_count"]["snapshot_shares"], 3540e6)
        self.assertEqual(provenance["share_count"]["selected_shares"], 4e9)

    def test_explicit_choice_requires_reason_and_positive_finite_value(self):
        self.write_snapshot()
        for options in (
            {"shares": 4e9}, {"shares": 4e9, "shares_reason": " "},
            {"shares_reason": "reason without value"},
            *({"shares": value, "shares_reason": "fixture"}
              for value in (0, -1, float("nan"), float("inf"))),
        ):
            with self.subTest(options=options), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    resolve_inputs(self.arguments(**options))

    def test_confirmed_current_share_basis_can_be_used_without_override(self):
        self.write_snapshot(share_count_basis={
            "kind": "CURRENT_OUTSTANDING", "is_current_outstanding": True,
        })
        args = self.arguments()
        _, provenance = resolve_inputs(args)
        self.assertEqual(args.shares, 3540e6)
        self.assertTrue(provenance["verified"])
        self.assertEqual(provenance["share_count"]["origin"], "SNAPSHOT")

    def test_implied_json_carries_selected_basis_and_snapshot_identity(self):
        snapshot = self.write_snapshot()
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            cmd_implied(self.arguments(shares=4e9, shares_reason="fixture assumption"))
        data = json.loads(output.getvalue())
        self.assertEqual(data["inputs"]["shares"], 4e9)
        provenance = data["inputs"]["input_provenance"]
        self.assertEqual(provenance["fundamentals_id"], snapshot["fundamentals_id"])
        self.assertEqual(provenance["share_count"]["reason"], "fixture assumption")

    def test_snapshot_financials_cannot_be_overridden_with_share_choice(self):
        self.write_snapshot()
        for field in ("fcf", "net_debt"):
            with self.subTest(field=field), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    resolve_inputs(self.arguments(shares=4e9, shares_reason="fixture",
                                                  **{field: 100}))


if __name__ == "__main__":
    unittest.main()
