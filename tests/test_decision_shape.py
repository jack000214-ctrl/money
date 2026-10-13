"""형이 틀린 판단은 traceback 이 아니라 계약 오류다.

값 검사는 객체 자리에는 객체가, 열거값 자리에는 문자열이 있다고 믿고 내려간다.
모델이 `"verdict": ["WAIT"]` 나 `"action": "HOLD"` 처럼 형을 틀리면 그 믿음이
깨져 `TypeError`·`AttributeError` 로 끝났다. 저장된 실제 판단 9건의 필드를 하나씩
바꿔 넣었을 때 17곳이 그렇게 죽었다.

죽는 것 자체는 fail-closed 지만 두 가지가 샜다.

· CLI 는 `ContractError` 만 받아 오류 목록으로 바꾼다. 다른 예외는 어느 필드인지
  말하지 않는 traceback 이 되어, 모델이 스스로 고칠 단서가 없다.
· `record` 는 `ContractError` 일 때만 실패한 출력을 quarantine 에 남긴다. 형이
  틀린 출력은 모델 평가 자료로 보존되지 않고 사라졌다.
"""

import json
import unittest
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from calculators.contracts import ContractError
from cio import decision as decision_module
from cio.decision import (
    DECISION_SCHEMA_PATH,
    _shape_errors,
    find_context,
    require_valid_decision,
    validate_decision,
)
from test_cio_core import valid_context, valid_decision


def errors_for(change):
    data = valid_decision()
    change(data)
    return validate_decision(data, context=valid_context())


class TestAWrongShapeNamesItsField(unittest.TestCase):
    def test_a_list_where_an_enum_belongs(self):
        self.assertEqual(
            errors_for(lambda d: d.update(verdict=["WAIT"])),
            ["verdict: 형이 계약과 다르다 — 문자열 자리에 목록"])

    def test_an_object_where_a_nested_enum_belongs(self):
        errors = errors_for(lambda d: d["price_plan"].update(currency={"code": "USD"}))
        self.assertEqual(
            errors, ["price_plan.currency: 형이 계약과 다르다 — 문자열 자리에 객체"])

    def test_every_wrong_field_is_listed_not_only_the_first(self):
        def change(data):
            data["verdict"] = ["WAIT"]
            data["proposed_trade"]["currency"] = []

        errors = errors_for(change)
        self.assertEqual(len(errors), 2)
        self.assertTrue(any(e.startswith("proposed_trade.currency:") for e in errors))

    def test_the_record_path_sees_a_contract_error(self):
        """`record` 가 quarantine 으로 보내는 것은 ContractError 뿐이다."""
        data = valid_decision()
        data["verdict"] = ["WAIT"]
        with self.assertRaises(ContractError) as raised:
            require_valid_decision(data, context=valid_context())
        self.assertIn("verdict", raised.exception.errors[0])


class TestContextLookupRejectsANonStringId(unittest.TestCase):
    def test_it_is_a_contract_error_not_a_type_error(self):
        """`cio validate` 는 context_id 를 감싸지 않고 넘겼고 정규식에서 죽었다."""
        for value in (["CTX-0123456789abcdef0123"], {"id": 1}, None, 3):
            with self.subTest(value=value), self.assertRaises(ContractError):
                find_context(value)


class TestTheDiagnosisDoesNotReplaceTheValueChecks(unittest.TestCase):
    def test_a_decision_the_value_checks_can_read_is_not_touched(self):
        """형 진단은 값 검사가 성립하지 않을 때만 쓴다. 평소 메시지는 그대로다."""
        errors = errors_for(lambda d: d.update(verdict="BUY_SOMEDAY"))
        self.assertTrue(any(e.startswith("verdict:") for e in errors))
        self.assertFalse(any("형이 계약과 다르다" in e for e in errors))

    def test_a_valid_decision_has_no_shape_errors(self):
        schema = json.loads(DECISION_SCHEMA_PATH.read_text(encoding="utf-8"))
        self.assertEqual(_shape_errors(valid_decision(), schema), [])

    def test_a_validator_bug_is_not_dressed_up_as_a_contract_error(self):
        """schema 가 설명하지 못하는 예외는 검증기 결함이다. 숨기지 않는다."""
        with patch.object(decision_module, "_validate_decision_values",
                          side_effect=TypeError("검증기 결함")):
            with self.assertRaises(TypeError):
                validate_decision(valid_decision(), context=valid_context())


class TestShapeErrors(unittest.TestCase):
    SCHEMA = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "count": {"type": "integer"},
            "score": {"type": ["number", "null"]},
            "state": {"enum": ["OPEN", "DONE"]},
            "rows": {"type": "array", "items": {"type": "object", "properties": {
                "value": {"type": "number"}}}},
        },
    }

    def test_matching_types_pass(self):
        self.assertEqual(_shape_errors({
            "name": "x", "count": 3.0, "score": None, "state": "OPEN",
            "rows": [{"value": 1}]}, self.SCHEMA), [])

    def test_true_is_not_a_number(self):
        self.assertEqual(
            _shape_errors({"score": True}, self.SCHEMA),
            ["score: 형이 계약과 다르다 — 숫자·null 자리에 참·거짓"])

    def test_a_fraction_is_not_an_integer(self):
        self.assertEqual(len(_shape_errors({"count": 3.5}, self.SCHEMA)), 1)

    def test_list_items_carry_their_index(self):
        self.assertEqual(
            _shape_errors({"rows": [{"value": 1}, "x"]}, self.SCHEMA),
            ["rows[1]: 형이 계약과 다르다 — 객체 자리에 문자열"])

    def test_an_enum_value_is_not_judged_here(self):
        """열거에 없는 문자열은 형이 맞다. 그것을 거르는 것은 값 검사다."""
        self.assertEqual(_shape_errors({"state": "LATER"}, self.SCHEMA), [])

    def test_the_top_level_is_named(self):
        self.assertEqual(_shape_errors([], self.SCHEMA),
                         ["판단: 형이 계약과 다르다 — 객체 자리에 목록"])


if __name__ == "__main__":
    unittest.main()
