"""받은 데이터를 다루지 않고 넘어가는 것을 계약이 막는다.

두 가지 실패에서 나왔다.

1. **수급.** 2026-07-29 삼성전자에서 기관이 597만주를 순매수하고 거래량이
   평소의 2.5배였다. 그 값은 `structured_optional.kr_flow` 로 packet 에 실려
   나갔는데 다음 판단의 논지는 판가 얘기만 했다. `drivers` 가 이익 동인만
   100% 로 배분하므로 수급이 들어갈 칸이 없었기 때문이다.
2. **사용자 주장.** 사용자는 이미 사고 싶은 상태에서 묻는다. 그 문장을 전제로
   받으면 결론은 질문에 이미 들어 있다.
"""

import unittest

from cio.decision import validate_decision

from test_cio_core import valid_context, valid_decision


def with_flow(context=None):
    context = context or valid_context()
    context["context"] = {
        "structured_optional": {
            "kr_flow": {"institution_net_shares": 5_971_830},
        }
    }
    from cio.decision import context_id_for

    context["context_id"] = context_id_for(context)
    return context


def with_lending(context=None):
    context = context or valid_context()
    context["context"] = {
        "structured_optional": {
            "kr_lending": {"balance_shares": 12_400_000,
                           "balance_change_shares": 3_100_000},
        }
    }
    from cio.decision import context_id_for

    context["context_id"] = context_id_for(context)
    return context


def with_claims(claims, context=None):
    context = context or valid_context()
    context["request"]["user_assertions"] = [
        {"claim_id": claim_id, "claim": text, "status": "USER_ASSERTED"}
        for claim_id, text in claims
    ]
    from cio.decision import context_id_for

    context["context_id"] = context_id_for(context)
    return context


def decision_for(context, **overrides):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data.update(overrides)
    return data


class TestFlowMustBeAddressed(unittest.TestCase):
    def test_flow_in_packet_makes_positioning_required(self):
        context = with_flow()
        errors = validate_decision(decision_for(context), context=context)
        self.assertTrue(
            any("positioning" in error for error in errors), errors
        )

    def test_skipping_is_allowed_if_the_reason_is_stated(self):
        """건너뛰는 것 자체는 정당하다. 말없이 지나가는 것이 아니다."""
        context = with_flow()
        data = decision_for(
            context,
            positioning={
                "checked": False,
                "skipped_reason": "20세션 누적이라 이번 급등 구간을 담지 못한다",
                "finding": None,
                "unobservable": ["공매도 잔고 — 로컬 수집기 없음"],
                "affects_conclusion": False,
            },
        )
        self.assertEqual(validate_decision(data, context=context), [])

    def test_checked_requires_a_finding(self):
        context = with_flow()
        data = decision_for(
            context,
            positioning={
                "checked": True,
                "finding": None,
                "skipped_reason": None,
                "unobservable": [],
                "affects_conclusion": True,
            },
        )
        errors = validate_decision(data, context=context)
        self.assertTrue(
            any("positioning.finding" in error for error in errors), errors
        )

    def test_cannot_both_check_and_skip(self):
        context = with_flow()
        data = decision_for(
            context,
            positioning={
                "checked": True,
                "finding": "기관 순매수 급증",
                "skipped_reason": "안 봤다",
                "unobservable": [],
                "affects_conclusion": True,
            },
        )
        errors = validate_decision(data, context=context)
        self.assertTrue(
            any("skipped_reason" in error for error in errors), errors
        )

    def test_no_flow_means_positioning_stays_optional(self):
        """수급을 안 준 종목에까지 강제하지 않는다 (미국 종목 등)."""
        context = valid_context()
        self.assertEqual(
            validate_decision(decision_for(context), context=context), []
        )

    def test_lending_alone_also_makes_positioning_required(self):
        """대차잔고는 수급과 같은 이유로 이익 동인 밖에서 빠져나간다.

        `kr_flow` 없이 `kr_lending` 만 실린 종목에서도 막혀야 한다. 둘 중
        하나만 검사하면 대차만 붙은 종목이 조용히 빠져나간다.
        """
        context = with_lending()
        errors = validate_decision(decision_for(context), context=context)
        self.assertTrue(
            any("positioning" in error and "kr_lending" in error
                for error in errors),
            errors,
        )

    def test_lending_skip_is_allowed_with_a_reason(self):
        context = with_lending()
        data = decision_for(
            context,
            positioning={
                "checked": True,
                "finding": "대차잔고 20세션 +310만주. 다만 누가 왜 빌렸는지는 알 수 없다",
                "skipped_reason": None,
                "unobservable": ["공매도 잔고 — 공개 API 없음"],
                "affects_conclusion": False,
            },
        )
        self.assertEqual(validate_decision(data, context=context), [])


class TestUserClaimsMustBeClosed(unittest.TestCase):
    CLAIMS = [("UA-aaa1", "실적이 1,860% 나왔다"), ("UA-bbb2", "2일 연속 하락했다")]

    # 실적 주장은 재무·공시로, 가격 주장은 가격 자료로 닫는다. 예전 이 파일은
    # 두 주장을 모두 PRICE:005930 로 닫는 것을 성공으로 고정했다 — 그 테스트가
    # 틀렸다. 종류가 맞는지는 tests/test_evidence_binding.py 가 지킨다.
    EARNINGS = {
        "claim_kind": "EARNINGS",
        "basis": {
            "metric": "영업이익",
            "period": "2026 회계연도 2분기",
            "comparison": "전년동기 대비",
        },
        "evidence_refs": ["FUNDAMENTALS:005930"],
    }
    PRICE = {
        "claim_kind": "PRICE",
        "basis": {
            "metric": "종가",
            "period": "직전 2거래일",
            "comparison": "전일 대비",
        },
        "evidence_refs": ["PRICE:005930"],
    }

    def context_with(self, claims):
        context = with_claims(claims)
        context["evidence_index"] = [
            {"source_id": "PRICE:005930", "kind": "MARKET_DATA"},
            {"source_id": "FUNDAMENTALS:005930", "kind": "FILING_DERIVED"},
        ]
        from cio.decision import context_id_for

        context["context_id"] = context_id_for(context)
        return context

    def test_open_claim_blocks_the_decision(self):
        context = self.context_with(self.CLAIMS)
        errors = validate_decision(decision_for(context), context=context)
        self.assertTrue(
            any("user_assertion_checks" in error for error in errors), errors
        )

    def test_partially_closed_is_still_blocked(self):
        context = self.context_with(self.CLAIMS)
        data = decision_for(
            context,
            user_assertion_checks=[
                {
                    "claim_id": "UA-aaa1",
                    "status": "CONFIRMED",
                    "finding": "2Q 영업이익 전년동기 +1810%",
                    **self.EARNINGS,
                }
            ],
        )
        errors = validate_decision(data, context=context)
        self.assertTrue(
            any("닫지 않은" in error and "UA-bbb2" in error for error in errors),
            errors,
        )

    def test_refuting_the_user_is_a_valid_close(self):
        """사용자의 전제가 틀렸다고 말하는 것이 이 시스템의 값어치다."""
        context = self.context_with(self.CLAIMS)
        data = decision_for(
            context,
            user_assertion_checks=[
                {
                    "claim_id": "UA-aaa1",
                    "status": "CONFIRMED",
                    "finding": "2Q 영업이익 전년동기 +1810%. 다만 기저가 거의 0",
                    **self.EARNINGS,
                },
                {
                    "claim_id": "UA-bbb2",
                    "status": "REFUTED",
                    "finding": "직전 거래일은 +21.5% 상승이다",
                    **self.PRICE,
                },
            ],
        )
        self.assertEqual(validate_decision(data, context=context), [])

    def test_confirmed_without_evidence_is_rejected(self):
        """근거 없이 '확인했다'고 말하는 것은 확인이 아니다."""
        context = self.context_with([self.CLAIMS[0]])
        data = decision_for(
            context,
            user_assertion_checks=[
                {
                    "claim_id": "UA-aaa1",
                    "status": "CONFIRMED",
                    "finding": "맞다",
                    **{**self.EARNINGS, "evidence_refs": []},
                }
            ],
        )
        errors = validate_decision(data, context=context)
        self.assertTrue(
            any("근거가 필요하다" in error for error in errors), errors
        )

    def test_unverifiable_needs_no_evidence(self):
        context = self.context_with([self.CLAIMS[0]])
        data = decision_for(
            context,
            user_assertion_checks=[
                {
                    "claim_id": "UA-aaa1",
                    "status": "UNVERIFIABLE",
                    "claim_kind": "EARNINGS",
                    "basis": None,
                    "finding": "어떤 지표의 1,860%인지 사용자에게 확인이 필요하다",
                    "evidence_refs": [],
                }
            ],
        )
        self.assertEqual(validate_decision(data, context=context), [])

    def test_answering_a_claim_the_packet_never_had(self):
        context = self.context_with([self.CLAIMS[0]])
        data = decision_for(
            context,
            user_assertion_checks=[
                {
                    "claim_id": "UA-aaa1",
                    "status": "UNVERIFIABLE",
                    "claim_kind": "EARNINGS",
                    "basis": None,
                    "finding": "확인 불가",
                    "evidence_refs": [],
                },
                {
                    "claim_id": "UA-ghost",
                    "status": "CONFIRMED",
                    "finding": "지어낸 주장",
                    **self.EARNINGS,
                },
            ],
        )
        errors = validate_decision(data, context=context)
        self.assertTrue(
            any("packet에 없는 주장" in error for error in errors), errors
        )


if __name__ == "__main__":
    unittest.main()
