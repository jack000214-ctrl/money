"""PC 가 쓴 파일이 계약대로 생겼는가.

`validate_calculation`·`validate_fundamentals` 와 그 아래 필드 검사기들은
**저장된 산출물을 다시 읽을 때 통과시킬지 정하는 자리**다. 여기가 새면 모양이
깨진 파일이 packet 으로 올라가고, 그 값은 화면에서도 packet 에서도 정상으로
보인다.

규칙 22 개가 테스트에서 한 번도 터지지 않았다. 직접 찔러 보니 전부 작동했으므로
**지금 구멍은 없다** — 다만 검사기가 죽어도 결과는 "오류 없음"으로 똑같으므로,
죽었다는 사실을 알 방법이 없었다.

필드 검사기(`_nonempty`·`_number`·`_datetime`·`_id_list`)를 따로 보는 이유는
이들이 두 검증 함수의 **공통 바닥**이기 때문이다. 하나가 조용히 통과하기
시작하면 두 계약이 동시에 헐거워진다.
"""

import json
import tempfile
import unittest
from pathlib import Path

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import (
    ContractError,
    SCHEMA_VERSION,
    _datetime,
    _id_list,
    _nonempty,
    _number,
    calculation_id_for,
    load_raw_snapshot,
    validate_calculation,
    validate_fundamentals,
)
from test_fundamentals import valid_fundamentals


class TestFieldGuards(unittest.TestCase):
    """두 계약이 공유하는 바닥."""

    def errors_from(self, call):
        errors = []
        call(errors)
        return errors

    def test_a_blank_string_is_not_a_value(self):
        self.assertIn("f: 비어 있지 않은 문자열이 필요하다",
                      self.errors_from(lambda e: _nonempty("   ", "f", e)))

    def test_a_non_string_is_not_a_value(self):
        self.assertTrue(self.errors_from(lambda e: _nonempty(123, "f", e)))

    def test_a_string_is_stripped_when_it_passes(self):
        self.assertEqual(_nonempty("  CALC-1  ", "f", []), "CALC-1")

    def test_a_boolean_is_not_a_number(self):
        """`isinstance(True, int)` 는 참이다 — 막지 않으면 True 가 1 이 된다."""
        self.assertTrue(self.errors_from(lambda e: _number(True, "f", e)))

    def test_nan_is_not_a_number(self):
        self.assertIn("f: 유한한 숫자가 필요하다",
                      self.errors_from(lambda e: _number(float("nan"), "f", e)))

    def test_infinity_is_not_a_number(self):
        self.assertTrue(
            self.errors_from(lambda e: _number(float("inf"), "f", e)))

    def test_a_minimum_is_enforced(self):
        self.assertIn("f: 0 이상이어야 한다",
                      self.errors_from(lambda e: _number(-1, "f", e, minimum=0)))

    def test_the_minimum_itself_passes(self):
        self.assertEqual(self.errors_from(lambda e: _number(0, "f", e, minimum=0)),
                         [])

    def test_a_timestamp_must_be_a_string(self):
        self.assertIn("f: 시간대가 포함된 ISO-8601 문자열이 필요하다",
                      self.errors_from(lambda e: _datetime(None, "f", e)))

    def test_a_malformed_timestamp_is_reported(self):
        self.assertIn("f: 올바른 ISO-8601 시각이 아니다",
                      self.errors_from(lambda e: _datetime("2026-13-99", "f", e)))

    def test_a_naive_timestamp_is_refused(self):
        """시간대가 없으면 어느 나라 자정인지 알 수 없다."""
        self.assertIn("f: 시간대가 포함돼야 한다",
                      self.errors_from(
                          lambda e: _datetime("2026-08-08T10:00:00", "f", e)))

    def test_a_zulu_timestamp_is_accepted(self):
        self.assertEqual(
            self.errors_from(lambda e: _datetime("2026-08-08T10:00:00Z", "f", e)),
            [])

    def test_an_empty_id_list_is_refused(self):
        """근거 없는 계산은 계산이 아니다."""
        self.assertIn("f: 하나 이상의 ID가 필요하다",
                      self.errors_from(lambda e: _id_list([], "f", e)))

    def test_a_duplicate_id_is_reported(self):
        self.assertIn("f: 중복 ID가 있다",
                      self.errors_from(lambda e: _id_list(["A", "A"], "f", e)))

    def test_a_blank_entry_in_an_id_list_is_reported(self):
        self.assertTrue(self.errors_from(lambda e: _id_list(["A", ""], "f", e)))


def calculation(**overrides):
    inputs = {"fcf_ttm": 1000.0, "wacc_pct": 9.0}
    outputs = {"scenarios": {
        name: {"total_return_pct": value, "annualized_return_pct": value / 2,
               "probability_pct": 33.0}
        for name, value in (("bear", -40.0), ("base", 10.0), ("bull", 60.0))
    }}
    data = {
        "schema_version": SCHEMA_VERSION,
        "model": "claude", "model_version": "opus-5",
        "instrument_id": "000660",
        "created_at": "2026-08-08T10:00:00+09:00",
        "source_ids": ["FUND-abc"],
        "inputs": inputs, "outputs": outputs,
    }
    data.update(overrides)
    data.setdefault(
        "calculation_id",
        calculation_id_for(data["model"], data["model_version"],
                           data.get("inputs"), data.get("outputs")),  # pyright: ignore[reportArgumentType]
    )
    return data


class TestCalculationContract(unittest.TestCase):
    def assertFlags(self, data, needle):
        errors = validate_calculation(data)
        self.assertTrue(any(needle in error for error in errors),
                        f"{needle!r} 가 없다 — 실제: {errors}")

    def test_a_valid_calculation_passes(self):
        self.assertEqual(validate_calculation(calculation()), [])

    def test_an_old_schema_version_is_refused(self):
        self.assertFlags(calculation(schema_version="cio-calc/0.1.0"),
                         "schema_version")

    def test_a_calculation_without_inputs_is_refused(self):
        """입력 없는 산출물은 재현할 수 없다."""
        self.assertFlags(calculation(inputs={}), "inputs: 하나 이상의 입력")

    def test_outputs_must_be_an_object(self):
        self.assertFlags(calculation(outputs=["bear"]), "outputs: 객체가 필요하다")

    def test_the_three_scenarios_are_required(self):
        self.assertFlags(calculation(outputs={"scenarios": None}),
                         "bear·base·bull")

    def test_a_missing_scenario_is_named(self):
        outputs = {"scenarios": {
            "bear": {"total_return_pct": -40.0,
                     "annualized_return_pct": -20.0, "probability_pct": 50.0},
            "base": {"total_return_pct": 10.0,
                     "annualized_return_pct": 5.0, "probability_pct": 50.0},
        }}
        self.assertFlags(calculation(outputs=outputs),
                         "outputs.scenarios.bull")

    def test_an_edited_calculation_fails_its_own_fingerprint(self):
        """저장된 뒤 입력을 고치면 id 가 더 이상 그 내용의 지문이 아니다."""
        data = calculation()
        data["inputs"]["fcf_ttm"] = 9999.0
        self.assertFlags(data, "내용 지문과 다르다")

    def test_a_malformed_id_is_reported(self):
        self.assertFlags(calculation(calculation_id="CALC-짧음"),
                         "CALC-<20자리 16진수>")

    def test_unhashable_inputs_are_reported(self):
        """nan 은 표준 JSON 으로 만들 수 없어 지문 자체가 나오지 않는다."""
        data = calculation()
        data["inputs"] = {"fcf_ttm": float("nan")}
        self.assertFlags(data, "표준 JSON으로 만들 수 없다")

    def test_a_naive_created_at_is_refused(self):
        self.assertFlags(calculation(created_at="2026-08-08T10:00:00"),
                         "시간대가 포함돼야 한다")

    def test_a_calculation_without_sources_is_refused(self):
        self.assertFlags(calculation(source_ids=[]), "source_ids")


class TestFundamentalsContract(unittest.TestCase):
    def assertFlags(self, data, needle):
        errors = validate_fundamentals(data)
        self.assertTrue(any(needle in error for error in errors),
                        f"{needle!r} 가 없다 — 실제: {errors}")

    def test_the_repository_fixture_passes(self):
        self.assertEqual(validate_fundamentals(valid_fundamentals()), [])

    def test_an_old_schema_version_is_refused(self):
        self.assertFlags(valid_fundamentals(schema_version="0.9.0"),
                         "schema_version")

    def test_a_malformed_fundamentals_id_is_refused(self):
        data = valid_fundamentals()
        data["fundamentals_id"] = "FUND-짧음"
        self.assertFlags(data, "FUND-<20자리 16진수>")

    def test_values_must_be_an_object(self):
        self.assertFlags(valid_fundamentals(values=[]), "values: 객체가 필요하다")

    def test_the_cash_flow_quarters_are_required(self):
        """TTM 은 분기 넷의 합이다 — 내역 없이 합만 있으면 대조할 수 없다."""
        self.assertFlags(valid_fundamentals(quarters={}), "분기 4개가 필요하다")

    def test_quarters_must_be_an_object(self):
        self.assertFlags(valid_fundamentals(quarters=[]),
                         "ocf·capex 분기 내역이 필요하다")

    def test_a_non_object_quarter_is_reported(self):
        data = valid_fundamentals()
        data["quarters"]["ocf"][0] = "2026-03-31"
        self.assertFlags(data, "객체가 필요하다")

    def income(self, **lines):
        return valid_fundamentals(income_statement=lines)

    def test_an_income_statement_line_must_be_an_object(self):
        self.assertFlags(self.income(revenue=1000),
                         "income_statement.revenue: 객체가 필요하다")

    def test_a_line_with_a_ttm_passes(self):
        self.assertEqual(
            validate_fundamentals(self.income(revenue={"ttm": 1000.0})), [])

    def test_a_line_with_only_quarters_passes(self):
        self.assertEqual(
            validate_fundamentals(
                self.income(revenue={"quarters": [{"val": 250.0}]})), [])

    def test_an_empty_line_must_say_why_it_is_empty(self):
        """IBM 표본이 operating_income 이 통째로 빈 채 통과한 적이 있다.

        0 이 아니라 결측인데 계약이 아무 말도 하지 않으면, 모델은 이 회사가
        영업이익을 공시하지 않는다는 사실 자체를 모른 채 판단한다.
        """
        self.assertFlags(self.income(operating_income={}),
                         "왜 없는지(absent_reason) 없이")

    def test_a_stated_reason_makes_the_gap_legible(self):
        self.assertEqual(
            validate_fundamentals(self.income(operating_income={
                "absent_reason": "이 회사는 영업이익을 별도 공시하지 않는다"})),
            [])

    def test_a_blank_reason_is_not_a_reason(self):
        self.assertFlags(self.income(operating_income={"absent_reason": "  "}),
                         "왜 없는지(absent_reason) 없이")

    def test_a_boolean_ttm_is_not_a_value(self):
        """`isinstance(True, int)` 가 참이라 True 가 TTM 자리를 채울 수 있다."""
        self.assertFlags(self.income(revenue={"ttm": True}),
                         "왜 없는지(absent_reason) 없이")


class TestRawSnapshotBinding(unittest.TestCase):
    """내용 주소형 원문이 자기가 말한 그 원문인가."""

    def snapshot(self, source="opendart.fss.or.kr", payload=None):
        return {"source": source, "payload": payload or {"list": []}}

    def test_a_snapshot_from_another_source_is_refused(self):
        """DART 원문 자리에 EDGAR 원문이 오면 계정 해석이 통째로 다르다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "raw.json"
            body = json.dumps(self.snapshot(source="sec.gov/edgar"))
            path.write_text(body, encoding="utf-8")
            import hashlib
            digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
            with self.assertRaises(ContractError) as caught:
                load_raw_snapshot(path, expected_digest=digest,
                                  expected_source="opendart.fss.or.kr")
            self.assertIn("원문 출처가 다르다", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
