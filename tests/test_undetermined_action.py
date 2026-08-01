"""`action.direction = UNDETERMINED` 의 경계.

예전 계약은 `INSUFFICIENT` 에서 `action.direction` 을 `HOLD` 로 강제했다. 보유
중인 종목에서 `HOLD` 는 빈 값이 아니라 "이 수량을 계속 들고 있어라"는 지시이므로,
그 강제는 모델의 "모른다"를 시스템이 "보유하라"로 번역하는 것이었다. 여기 있는
테스트가 그 번역이 다시 생기지 않게 막는다.
"""

import json
import unittest

from cio.decision import (
    ACTION_DIRECTIONS,
    UNDETERMINED_REASONS,
    validate_decision,
)
from cio.paths import ROOT

from test_cio_core import valid_context, valid_decision


def undetermined(reason, **overrides):
    data = valid_decision()
    data["action"] = {
        "direction": "UNDETERMINED",
        "quantity": None,
        "sell_reason": None,
        "undetermined_reason": reason,
        "rationale": "무엇이 없어서 정할 수 없는지 본문에 남긴다",
    }
    data.update(overrides)
    return data


def errors_for(data):
    return validate_decision(data, context=valid_context())


class TestUndeterminedIsNotHold(unittest.TestCase):
    def test_insufficient_may_not_instruct_hold(self):
        """INSUFFICIENT 에서 HOLD 는 거부된다 — 그것은 보유 지시다."""
        data = valid_decision()
        data["verdict"] = "INSUFFICIENT"
        data["action"]["direction"] = "HOLD"
        errors = errors_for(data)
        self.assertTrue(
            any("UNDETERMINED여야 한다" in error for error in errors),
            errors,
        )

    def test_undetermined_requires_a_stated_blocker(self):
        """무엇이 없어서 못 정하는지 밝히지 않은 UNDETERMINED 는 거부된다."""
        data = undetermined(None)
        errors = errors_for(data)
        self.assertTrue(
            any("undetermined_reason" in error for error in errors), errors
        )

    def test_reason_without_undetermined_is_rejected(self):
        """행동을 정했으면서 막혔다고 적을 수 없다."""
        data = valid_decision()
        data["action"]["undetermined_reason"] = "NO_INVESTOR_POLICY"
        errors = errors_for(data)
        self.assertTrue(
            any("UNDETERMINED가 아니면" in error for error in errors), errors
        )

    def test_unknown_blocker_is_rejected(self):
        errors = errors_for(undetermined("BECAUSE_I_SAID_SO"))
        self.assertTrue(
            any("undetermined_reason" in error for error in errors), errors
        )


class TestBlockerSeparation(unittest.TestCase):
    """'데이터가 없어 막힘'과 '정책이 없어 막힘'은 해결할 사람이 다르다.

    한 통에 섞이면 집계에서 둘을 나눌 수 없고, 무엇을 고쳐야 하는지도 알 수
    없게 된다.
    """

    def test_insufficient_evidence_requires_insufficient_verdict(self):
        errors = errors_for(undetermined("INSUFFICIENT_EVIDENCE"))
        self.assertTrue(
            any("verdict도 INSUFFICIENT" in error for error in errors), errors
        )

    def test_policy_gap_coexists_with_a_normal_verdict(self):
        """한도가 없어 수량을 못 정하는 것은 WAIT 와 함께 성립한다."""
        self.assertEqual(errors_for(undetermined("NO_INVESTOR_POLICY")), [])

    def test_insufficient_verdict_may_not_blame_policy(self):
        data = undetermined("NO_INVESTOR_POLICY", verdict="INSUFFICIENT")
        errors = errors_for(data)
        self.assertTrue(
            any("INSUFFICIENT_EVIDENCE여야 한다" in error for error in errors),
            errors,
        )


class TestUndeterminedDeploysNothing(unittest.TestCase):
    def test_undetermined_may_not_carry_a_buy_amount(self):
        """행동을 못 정한다면서 새 돈을 넣을 수는 없다."""
        data = undetermined("NO_INVESTOR_POLICY")
        data["verdict"] = "BUY_NOW"
        data["proposed_trade"]["amount"] = 1000.0
        errors = errors_for(data)
        self.assertTrue(
            any("새로 사겠다는 금액이 있다" in error for error in errors), errors
        )

    def test_undetermined_may_not_carry_quantity_or_sell_reason(self):
        data = undetermined("NO_POSITION_DATA")
        data["action"]["quantity"] = 10
        data["action"]["sell_reason"] = "VALUATION"
        errors = errors_for(data)
        self.assertTrue(
            any("매도가 아니면" in error for error in errors), errors
        )


class TestSchemaAndValidatorAgree(unittest.TestCase):
    """모델이 읽는 스키마와 record 가 강제하는 규칙이 갈라지면 안 된다.

    갈라지면 모델은 허용된다고 믿고 낸 출력이 저장 단계에서 격리된다 — 판단
    한 번을 통째로 버리는 실패다.
    """

    def setUp(self):
        self.action = json.loads(
            (ROOT / "schemas" / "cio-decision.schema.json").read_text(
                encoding="utf-8"
            )
        )["properties"]["action"]

    def test_direction_enum_matches(self):
        self.assertEqual(
            set(self.action["properties"]["direction"]["enum"]),
            ACTION_DIRECTIONS,
        )

    def test_blocker_enum_matches(self):
        enum = set(self.action["properties"]["undetermined_reason"]["enum"])
        self.assertEqual(enum - {None}, UNDETERMINED_REASONS)
        self.assertIn(None, enum)

    def test_optional_field_is_not_in_required(self):
        self.assertNotIn("undetermined_reason", self.action["required"])


class TestOldDecisionsStillValidate(unittest.TestCase):
    def test_action_without_the_new_field_is_still_accepted(self):
        """계약을 조일 때마다 과거 기억을 잃는 구조를 만들지 않는다."""
        data = valid_decision()
        self.assertNotIn("undetermined_reason", data["action"])
        self.assertEqual(errors_for(data), [])


if __name__ == "__main__":
    unittest.main()
