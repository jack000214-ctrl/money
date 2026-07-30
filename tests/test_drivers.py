"""이익 동인 배분 계약의 회귀 테스트.

삼성전자는 반도체·모바일·디스플레이·하만이 다 있고, 파운드리 수주와 판가와
폴드 판매가 동시에 움직인다. 무엇부터 볼지 정하지 않으면 이익에 거의 영향이
없는 이야기에 판단의 무게가 실린다. 비중 합을 100으로 강제하는 것은 "다
중요하다"를 배분으로 인정하지 않기 위해서다.
"""

import unittest

from cio.decision import _validate_drivers


def driver(**overrides):
    base = {
        "name": "메모리 사이클",
        "weight_pct": 100,
        "basis": "DS 부문이 영업이익의 대부분",
        "researched": True,
        "finding": "재고가 세 분기 연속 늘었다",
    }
    base.update(overrides)
    return base


def errors_for(drivers):
    found: list[str] = []
    _validate_drivers(drivers, found)
    return found


class TestShape(unittest.TestCase):
    def test_minimal_valid_allocation(self):
        self.assertEqual(errors_for([driver()]), [])

    def test_missing_drivers_is_refused(self):
        self.assertTrue(errors_for(None))
        self.assertTrue(errors_for([]))

    def test_unknown_field_is_refused(self):
        self.assertTrue(errors_for([driver(guess="아무거나")]))

    def test_too_many_drivers_is_refused(self):
        self.assertTrue(
            errors_for([driver(name=f"동인 {i}", weight_pct=11) for i in range(9)])
        )

    def test_duplicate_names_are_refused(self):
        found = errors_for([
            driver(weight_pct=50),
            driver(weight_pct=50, finding="다른 발견"),
        ])
        self.assertTrue(any("중복" in message for message in found))


class TestWeights(unittest.TestCase):
    def test_weights_must_sum_to_one_hundred(self):
        found = errors_for([
            driver(name="메모리", weight_pct=60),
            driver(name="모바일", weight_pct=30, researched=False,
                   finding=None, skipped_reason="이익 기여가 작다"),
        ])
        self.assertTrue(any("100이어야" in message for message in found))

    def test_exact_hundred_across_several_drivers(self):
        self.assertEqual(
            errors_for([
                driver(name="메모리", weight_pct=55),
                driver(name="파운드리", weight_pct=20,
                       finding="Tesla 수주가 2033년까지 걸려 있다"),
                driver(name="모바일", weight_pct=15, researched=False,
                       finding=None, skipped_reason="영업이익 기여가 한 자리다"),
                driver(name="디스플레이", weight_pct=10, researched=False,
                       finding=None, skipped_reason="분기 변동이 결론을 못 바꾼다"),
            ]),
            [],
        )

    def test_fractional_weights_are_allowed_within_tolerance(self):
        self.assertEqual(
            errors_for([
                driver(name="A", weight_pct=33.33),
                driver(name="B", weight_pct=33.33, finding="둘째"),
                driver(name="C", weight_pct=33.34, finding="셋째"),
            ]),
            [],
        )

    def test_zero_weight_is_refused(self):
        # 비중 0이면 동인이 아니다. 목록만 채우는 것을 막는다.
        self.assertTrue(errors_for([driver(weight_pct=0)]))


class TestResearchAccounting(unittest.TestCase):
    def test_researched_driver_needs_a_finding(self):
        found = errors_for([driver(finding=None)])
        self.assertTrue(any("finding" in message for message in found))

    def test_skipped_driver_needs_a_reason(self):
        found = errors_for([
            driver(researched=False, finding=None, skipped_reason=None)
        ])
        self.assertTrue(any("skipped_reason" in message for message in found))

    def test_cannot_claim_both_researched_and_skipped(self):
        found = errors_for([driver(skipped_reason="건너뛰었다")])
        self.assertTrue(any("skipped_reason" in message for message in found))

    def test_skipped_driver_cannot_report_a_finding(self):
        found = errors_for([
            driver(researched=False, skipped_reason="작다", finding="그런데 봤다")
        ])
        self.assertTrue(any("finding" in message for message in found))

    def test_researched_must_be_boolean_not_truthy(self):
        found = errors_for([driver(researched="yes")])
        self.assertTrue(any("researched" in message for message in found))

    def test_blank_reason_is_not_a_reason(self):
        found = errors_for([
            driver(researched=False, finding=None, skipped_reason="   ")
        ])
        self.assertTrue(any("skipped_reason" in message for message in found))


class TestSkippingIsLegitimate(unittest.TestCase):
    def test_a_decision_may_skip_every_minor_driver(self):
        # 모든 동인을 매번 조사할 수는 없다. 계약이 요구하는 것은 조사가 아니라
        # 배분과 그 이유의 기록이다.
        self.assertEqual(
            errors_for([
                driver(name="핵심", weight_pct=80),
                driver(name="주변1", weight_pct=10, researched=False,
                       finding=None, skipped_reason="규모가 작다"),
                driver(name="주변2", weight_pct=10, researched=False,
                       finding=None, skipped_reason="이번 기간에 사건이 없다"),
            ]),
            [],
        )


if __name__ == "__main__":
    unittest.main()
