"""역산 밸류에이션과 계산 산출물 계약 테스트.

네트워크를 타지 않는다. reverse_dcf 는 입력을 전부 명시로 받으므로
외부 호출이 없다 — 숫자의 출처를 흐리지 않으려는 설계이기도 하다.
"""

import json
import tempfile
import unittest
from pathlib import Path

from contracts import (ContractError, save_validated_calculation,
                       validate_analysis, validate_calculation,
                       verify_expected_return_calculation)
from reverse_dcf import (MODEL, MODEL_VERSION, annualized_pct,
                         equity_value_per_share, solve)
from test_contracts import valid_analysis, valid_calculation

# 손으로 검산한 기준 입력: FCF 3.6e9, g 46.8%, r 10%, gt 2.5%, N 10,
# 순현금 30e9, 주식수 3.5e9 → 주당 약 330
BASE = dict(discount_rate=0.10, terminal_growth=0.025, years=10,
            net_debt=-30e9, shares=3.5e9)


class TestModel(unittest.TestCase):
    def test_value_matches_hand_calculation(self):
        value = equity_value_per_share(3.6e9, 0.468, **BASE)
        self.assertAlmostEqual(value, 330.0, delta=1.0)

    def test_value_increases_with_growth(self):
        """이분법이 성립하려면 성장률에 대해 단조증가여야 한다."""
        values = [equity_value_per_share(3.6e9, g / 100, **BASE)
                  for g in range(0, 60, 5)]
        self.assertEqual(values, sorted(values))

    def test_solve_recovers_the_growth_that_produced_a_price(self):
        price = equity_value_per_share(3.6e9, 0.22, **BASE)
        answer, status = solve(price, -0.95, 2.0,
                               lambda g: equity_value_per_share(3.6e9, g, **BASE))
        self.assertEqual(status, "SOLVED")
        self.assertAlmostEqual(answer, 0.22, places=4)

    def test_price_beyond_the_model_is_reported_not_guessed(self):
        answer, status = solve(1e9, -0.95, 2.0,
                               lambda g: equity_value_per_share(3.6e9, g, **BASE))
        self.assertEqual(status, "ABOVE_RANGE")

    def test_annualization_uses_the_convergence_horizon(self):
        """IPS 요구수익률이 연율이므로 시나리오도 연율이어야 비교가 된다."""
        # 2년에 걸쳐 +44% → 연 20%
        self.assertAlmostEqual(annualized_pct(144.0, 100.0, 24), 20.0, places=6)
        self.assertAlmostEqual(annualized_pct(120.0, 100.0, 12), 20.0, places=6)

    def test_worthless_equity_floors_at_total_loss(self):
        self.assertEqual(annualized_pct(-5.0, 100.0, 12), -100.0)


calculation = valid_calculation


class TestCalculationContract(unittest.TestCase):
    def test_valid_calculation_passes(self):
        self.assertEqual(validate_calculation(calculation()), [])

    def test_fixture_tracks_the_shipped_model_version(self):
        """모델 버전을 올리면 이 테스트가 먼저 깨져야 한다.

        픽스처가 옛 버전에 머무르면 지문 검사가 옛 모델만 검사하게 된다.
        """
        data = calculation()
        self.assertEqual(data["model"], MODEL)
        self.assertEqual(data["model_version"], MODEL_VERSION)

    def test_edited_inputs_break_the_fingerprint(self):
        """가정을 바꾸면 ID 가 바뀐다."""
        data = calculation()
        data["inputs"]["discount_rate_pct"] = 6.0
        errors = validate_calculation(data)
        self.assertTrue(any("지문과 다르다" in error for error in errors))

    def test_edited_outputs_break_the_fingerprint(self):
        """가정을 그대로 두고 결과 숫자만 고치는 것이 막으려던 구멍이다."""
        data = calculation()
        data["outputs"]["scenarios"]["base"]["return_pct"] = 99.0
        errors = validate_calculation(data)
        self.assertTrue(any("지문과 다르다" in error for error in errors))

    def test_calculation_needs_a_source(self):
        data = calculation()
        data["source_ids"] = []
        self.assertTrue(any("source_ids" in error for error in validate_calculation(data)))

    def test_invalid_calculation_is_never_saved(self):
        data = calculation()
        data["calculation_id"] = "CALC-deadbeef"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContractError):
                save_validated_calculation(tmp, data)
            self.assertEqual(list(Path(tmp).iterdir()), [])

    def test_saved_calculation_is_named_by_its_id(self):
        data = calculation()
        with tempfile.TemporaryDirectory() as tmp:
            path = save_validated_calculation(tmp, data)
            self.assertEqual(path.name, f"{data['calculation_id']}.json")
            self.assertEqual(
                json.loads(path.read_text(encoding="utf-8"))["model"], MODEL)


class TestAnalysisCrossCheck(unittest.TestCase):
    """분석의 기대수익이 계산 산출물과 일치하는지 대조한다.

    이 검사가 없으면 계약은 calculation_id 라는 문자열이 있는지만 보고,
    정작 매매를 결정하는 숫자는 손으로 적을 수 있다.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.calculation = calculation()
        save_validated_calculation(self.dir, self.calculation)
        self.analysis = valid_analysis()
        self.analysis["expected_return"]["calculation_id"] = \
            self.calculation["calculation_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_matching_analysis_passes(self):
        self.assertEqual(validate_analysis(self.analysis), [])
        self.assertEqual(
            verify_expected_return_calculation(self.analysis, self.dir), [])

    def test_return_that_disagrees_with_the_calculation_is_caught(self):
        self.analysis["expected_return"]["scenarios"]["base"]["return_pct"] = 25.0
        self.analysis["expected_return"]["probability_weighted_pct"] = 17.5
        self.assertEqual(validate_analysis(self.analysis), [])
        errors = verify_expected_return_calculation(self.analysis, self.dir)
        self.assertTrue(any("계산 산출물은" in error for error in errors))

    def test_missing_calculation_is_caught(self):
        self.analysis["expected_return"]["calculation_id"] = "CALC-" + "1" * 20
        errors = verify_expected_return_calculation(self.analysis, self.dir)
        self.assertTrue(any("계산 산출물" in error and "없다" in error
                            for error in errors))

    def test_calculation_for_another_instrument_is_caught(self):
        self.analysis["instrument_id"] = "BBB"
        errors = verify_expected_return_calculation(self.analysis, self.dir)
        self.assertTrue(any("분석은 BBB" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
