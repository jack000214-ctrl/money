"""판단 계약의 개별 규칙이 실제로 발화하는지 전수 확인한다.

`cio/decision.py` 에는 오류를 만드는 지점이 200개 넘게 있는데, 그중 상당수는
어떤 테스트도 밟은 적이 없었다. 그 규칙들은 **판단을 저장할 때 걸러내는 유일한
안전장치**다 — 규칙이 조용히 망가지면 계약 미달 판단이 통과해 다음 컨텍스트로
실려 나가고, 그때는 이미 모델이 그것을 근거로 읽은 뒤다.

여기서 하는 일은 하나다. 유효한 판단 하나를 놓고 **필드를 하나씩만 망가뜨린 뒤
그 규칙이 정확히 발화하는지** 본다. 규칙을 지우거나 조건을 뒤집으면 해당 항목이
실패한다.

한 항목이 여러 오류를 낼 수 있으므로 '정확히 하나'를 요구하지 않는다. 다만
**기대한 문구가 반드시 들어 있어야** 하며, 유효한 원본은 오류가 0개여야 한다 —
그래야 이 표가 '아무거나 넣으면 아무 오류나 난다'가 되지 않는다.
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision


def broken(mutate):
    """유효한 판단을 하나만 망가뜨려 오류 목록을 낸다."""
    data = valid_decision()
    mutate(data)
    return validate_decision(data, context=valid_context())


def web_source(**overrides):
    """모델이 조사해 등록한 source 한 개."""
    base = {
        "source_id": "WEBSNAP:aaaaaaaaaaaa",
        "type": "NEWS",
        "locator": "https://example.com/a",
        "published_at": "2026-07-01T00:00:00+09:00",
        "retrieved_at": "2026-07-28T09:30:00+09:00",
    }
    base.update(overrides)
    return base


def set_action(data, **fields):
    data["action"] = {**data["action"], **fields}


def set_plan(data, **fields):
    data["price_plan"] = {**data["price_plan"], **fields}


class RuleTable:
    """표를 돌리는 공통 실행기.

    `unittest.TestCase` 를 상속하지 **않는다** — 상속하면 이 빈 껍데기 자체가
    수집돼 매 실행마다 skip 한 건이 찍힌다. skip 은 '이번엔 아무것도 안 봤다'는
    뜻이라 아껴 써야 하고, 실행기 하나 때문에 쓰는 것은 낭비다.
    """

    table: tuple = ()

    def test_each_rule_fires(self):
        self.assertTrue(self.table, "표가 비어 있다")
        for label, mutate, expected in self.table:
            with self.subTest(label):
                errors = broken(mutate)
                joined = "\n".join(errors)
                self.assertIn(
                    expected,
                    joined,
                    f"[{label}] 규칙이 발화하지 않았다. 나온 오류:\n{joined}",
                )


class TestValidFixtureIsClean(unittest.TestCase):
    def test_the_untouched_fixture_produces_no_errors(self):
        """표의 전제. 원본이 이미 더러우면 어떤 항목도 증명하지 못한다."""
        self.assertEqual(
            validate_decision(valid_decision(), context=valid_context()), []
        )


class TestTopLevelRules(RuleTable, unittest.TestCase):
    table = (
        ("모르는 최상위 필드",
         lambda d: d.update(무엇인가="값"),
         "알 수 없는 최상위 필드"),
        ("schema_version 불일치",
         lambda d: d.update(schema_version="cio-decision/0.9.0"),
         "schema_version"),
        ("asset_id 경로 주입",
         lambda d: d.update(asset_id="../../etc/passwd"),
         "안전한 자산 ID 형식"),
        ("verdict 오타",
         lambda d: d.update(verdict="BUY"),
         "verdict:"),
        ("outcome_metric 교체",
         lambda d: d.update(outcome_metric="PRICE_ONLY"),
         "outcome_metric:"),
        ("assessment_id 형식 위반",
         lambda d: d.update(assessment_id="CIO-짧다"),
         "CIO-<20자리 16진수>"),
        ("assessment_id 지문 위조",
         lambda d: d.update(assessment_id="CIO-" + "a" * 20),
         "내용 지문은"),
        ("horizon_months 소수",
         lambda d: d.update(horizon_months=1.5),
         "정수여야 한다"),
        ("horizon_months 하한 위반",
         lambda d: d.update(horizon_months=0),
         "1 이상이어야 한다"),
        ("승리 문턱이 실패 문턱 이하",
         lambda d: d.update(win_return_threshold_pct=-20.0),
         "win_return_threshold_pct는"),
        ("created_at 시간대 없음",
         lambda d: d.update(created_at="2026-07-28T10:00:00"),
         "시간대가 포함돼야 한다"),
        ("created_at 이 컨텍스트보다 앞섬",
         lambda d: d.update(created_at="2026-07-27T10:00:00+09:00"),
         "컨텍스트보다 앞설 수 없다"),
        ("created_at 이 먼 미래",
         lambda d: d.update(created_at="2099-01-01T00:00:00+09:00"),
         "5분 이상 미래"),
        # context 는 2026-07-28 09:00 KST. 그보다 12시간 넘게 뒤의 판단은 옛 packet
        # 위에 선 것이다 — 그 사이의 가격·보유·계약 변화가 packet 에 없다.
        ("created_at 이 context 보다 12시간 넘게 뒤",
         lambda d: d.update(created_at="2026-07-28T21:00:01+09:00"),
         "12시간까지만"),
        ("unknowns 가 배열이 아님",
         lambda d: d.update(unknowns="문자열 하나"),
         "문자열 배열이 필요하다"),
    )


class TestAPacketIsUsedOnlyWhileItsPricesAreCurrent(unittest.TestCase):
    """context 는 2026-07-28 09:00 KST 에 만들어졌다."""

    def errors(self, created_at):
        data = valid_decision()
        data["created_at"] = created_at
        return [error for error in validate_decision(data, context=valid_context())
                if "12시간까지만" in error]

    def test_a_decision_within_the_window_passes(self):
        self.assertEqual(self.errors("2026-07-28T09:05:00+09:00"), [])
        self.assertEqual(self.errors("2026-07-28T21:00:00+09:00"), [])

    def test_the_same_instant_in_another_timezone_is_the_same_age(self):
        self.assertEqual(self.errors("2026-07-28T12:00:00+00:00"), [])
        self.assertEqual(len(self.errors("2026-07-28T12:00:01+00:00")), 1)

    def test_an_old_packet_cannot_carry_a_new_decision(self):
        self.assertEqual(len(self.errors("2026-08-30T10:00:00+09:00")), 1)


class TestModelBlockRules(RuleTable, unittest.TestCase):
    table = (
        ("model 이 객체가 아님",
         lambda d: d.update(model="claude"),
         "model: 객체가 필요하다"),
        ("model 에 모르는 필드",
         lambda d: d["model"].update(온도=0.7),
         "model: 알 수 없는 필드"),
        ("model.provider 비어 있음",
         lambda d: d["model"].update(provider=""),
         "model.provider"),
        ("prompt_version 이 컨텍스트와 다름",
         lambda d: d["model"].update(prompt_version="9.9"),
         "context의 prompt version과 다르다"),
    )


class TestScenarioRules(RuleTable, unittest.TestCase):
    table = (
        ("expected_return_pct 키 누락",
         lambda d: d["expected_return_pct"].pop("bull"),
         "bear·base·bull·probability_weighted"),
        ("scenario_probability_pct 키 누락",
         lambda d: d["scenario_probability_pct"].pop("bear"),
         "bear·base·bull이 필요하다"),
        ("확률 합이 100이 아님",
         lambda d: d["scenario_probability_pct"].update(bear=30.0),
         "합은 100이어야 한다"),
        ("기대수익 순서가 뒤집힘",
         lambda d: d["expected_return_pct"].update(bear=80.0),
         "bear <= base <= bull"),
    )


class TestPricePlanRules(RuleTable, unittest.TestCase):
    """가격계획은 이 판단의 유일한 집행 지시다. 모순은 여기서 걸러야 한다."""

    table = (
        ("계약 필드 누락",
         lambda d: d["price_plan"].pop("reask_events"),
         "계약 필드가 정확히 필요하다"),
        ("통화가 KRW·USD 가 아님",
         lambda d: set_plan(d, currency="EUR"),
         "KRW 또는 USD여야 한다"),
        ("진입 범위 상하 역전",
         lambda d: set_plan(d, entry_range_low=110.0),
         "entry_range_low는 entry_range_high 이하"),
        ("선호 진입가가 범위 밖",
         lambda d: set_plan(d, preferred_entry=50.0),
         "진입 범위 안이어야 한다"),
        ("손절가가 진입 범위 안",
         lambda d: set_plan(d, stop_price=95.0),
         "진입 범위 하단보다 낮아야 한다"),
        ("분할매도 시작가가 진입 범위 안",
         lambda d: set_plan(d, scale_out_start=95.0),
         "진입 범위 상단보다 높아야 한다"),
        ("재질문 하단이 검토가보다 높음",
         lambda d: set_plan(d, reask_below=120.0),
         "reask_below: 입력 가격보다 낮아야 한다"),
        ("재질문 상단이 검토가보다 낮음",
         lambda d: set_plan(d, reask_above=90.0),
         "reask_above: 입력 가격보다 높아야 한다"),
        ("통화가 검토 가격 통화와 다름",
         lambda d: set_plan(d, currency="KRW"),
         "입력 가격 통화와 달라선 안 된다"),
    )


class TestProposedTradeRules(RuleTable, unittest.TestCase):
    table = (
        ("계약 필드 누락",
         lambda d: d["proposed_trade"].pop("rationale"),
         "amount·currency·rationale"),
        ("통화가 KRW·USD 가 아님",
         lambda d: d["proposed_trade"].update(currency="EUR"),
         "proposed_trade.currency"),
        ("통화가 입력 금액 통화와 다름",
         lambda d: d["proposed_trade"].update(currency="KRW"),
         "입력 금액 통화와 달라선 안 된다"),
        ("사유가 비어 있음",
         lambda d: d["proposed_trade"].update(rationale="  "),
         "proposed_trade.rationale"),
    )


class TestActionRules(RuleTable, unittest.TestCase):
    """`action` 은 보유분에 대한 지시다. 상태가 틀리면 다음 판단이 오독한다."""

    table = (
        ("계약 필드 누락",
         lambda d: d["action"].pop("sell_reason"),
         "direction·quantity·sell_reason·rationale"),
        ("모르는 direction",
         lambda d: set_action(d, direction="SELL"),
         "action.direction:"),
        ("모르는 sell_reason",
         lambda d: set_action(d, direction="TRIM", quantity=10.0,
                              sell_reason="기분"),
         "action.sell_reason:"),
        ("매도 수량이 0 이하",
         lambda d: set_action(d, direction="TRIM", quantity=0.0,
                              sell_reason="VALUATION"),
         "매도 수량은 0보다 커야 한다"),
        ("매도가 아닌데 수량이 있음",
         lambda d: set_action(d, direction="HOLD", quantity=10.0),
         "매도가 아니면 null이어야 한다"),
        ("ADD 인데 집행액이 0",
         lambda d: set_action(d, direction="ADD"),
         "ADD면 제안 금액이 0보다 커야 한다"),
        ("사유가 비어 있음",
         lambda d: set_action(d, rationale=""),
         "action.rationale"),
    )


class TestMethodAndCounterRules(RuleTable, unittest.TestCase):
    table = (
        ("method 필드 누락",
         lambda d: d["method"].pop("assumptions"),
         "valuation_family·why_fit·assumptions"),
        ("valuation_family 비어 있음",
         lambda d: d["method"].update(valuation_family=""),
         "method.valuation_family"),
        ("반대 논지 필드 누락",
         lambda d: d["strongest_counter_thesis"].pop("evidence_refs"),
         "text·evidence_refs가 필요하다"),
        ("반대 논지 본문 비어 있음",
         lambda d: d["strongest_counter_thesis"].update(text=""),
         "strongest_counter_thesis.text"),
        ("반대 논지 근거 중복",
         lambda d: d["strongest_counter_thesis"].update(
             evidence_refs=["PRICE:TSLA", "PRICE:TSLA"]),
         "strongest_counter_thesis.evidence_refs: 중복"),
    )


class TestSourceAndEvidenceRules(RuleTable, unittest.TestCase):
    """근거는 개수가 아니라 결속이다 — 가리키는 것이 없으면 문자열일 뿐이다."""

    table = (
        ("sources 가 배열이 아님",
         lambda d: d.update(sources={"하나": "만"}),
         "sources: 배열이 필요하다"),
        ("source 계약 필드 위반",
         lambda d: d.update(sources=[{"source_id": "WEBSNAP:a"}]),
         "source 계약 필드가 정확히 필요하다"),
        ("source_id 중복",
         lambda d: d.update(sources=[
             web_source(), web_source(locator="https://example.com/b"),
         ]),
         "source_id: 중복"),
        ("source 회수시각이 판단보다 늦음",
         lambda d: d.update(sources=[
             web_source(retrieved_at="2026-07-28T23:00:00+09:00")]),
         "decision created_at보다 늦을 수 없다"),
        ("evidence_refs 중복",
         lambda d: d.update(evidence_refs=["PRICE:TSLA", "PRICE:TSLA"]),
         "evidence_refs: 중복 ID가 있다"),
        ("어디에도 없는 근거 ID",
         lambda d: d.update(evidence_refs=["PRICE:없는것"]),
         "evidence ref가 source index에 없다"),
        ("evidence_refs 가 비어 있음",
         lambda d: d.update(evidence_refs=[]),
         "evidence_refs: 최소 1개가 필요하다"),
    )


class TestContextCrossCheckRules(RuleTable, unittest.TestCase):
    """packet 과 판단이 다른 것을 가리키면 그 판단은 다른 질문의 답이다."""

    table = (
        ("context_id 가 컨텍스트와 다름",
         lambda d: d.update(context_id="CTX-" + "b" * 20),
         "context_id: 컨텍스트와 결정이 다르다"),
        ("asset_id 가 컨텍스트와 다름",
         lambda d: d.update(asset_id="IBM"),
         "asset_id: 컨텍스트와 결정이 다르다"),
    )


class TestBlockedContextRule(unittest.TestCase):
    def test_blocked_context_accepts_only_insufficient(self):
        """입력이 막힌 packet 위에서 숫자 판단을 만들면 안 된다."""
        context = valid_context()
        context["validation"]["status"] = "BLOCKED"
        errors = validate_decision(valid_decision(), context=context)
        self.assertTrue(
            any("BLOCKED context에서는 INSUFFICIENT만" in e for e in errors),
            errors,
        )

    def test_unknown_validation_status_is_refused(self):
        context = valid_context()
        context["validation"]["status"] = "MAYBE"
        errors = validate_decision(valid_decision(), context=context)
        self.assertTrue(
            any("PASS 또는 BLOCKED여야 한다" in e for e in errors), errors
        )


class TestRecurringPlanRules(RuleTable, unittest.TestCase):
    """적립 중에는 침묵이 CONTINUE 와 같다. 그래서 따로 답하게 한다."""

    table = (
        ("객체가 아님",
         lambda d: d.update(recurring_plan="계속한다"),
         "recurring_plan: 객체 또는 null이어야 한다"),
        ("모르는 필드",
         lambda d: d.update(recurring_plan={
             "state": "STOP", "rationale": "중단", "빈도": "매일"}),
         "recurring_plan: 알 수 없는 필드"),
        ("필수 필드 누락",
         lambda d: d.update(recurring_plan={"state": "STOP"}),
         "state·rationale이 필요하다"),
        ("모르는 state",
         lambda d: d.update(recurring_plan={
             "state": "PAUSE_MAYBE", "rationale": "고민 중"}),
         "recurring_plan.state:"),
        ("사유가 비어 있음",
         lambda d: d.update(recurring_plan={"state": "STOP", "rationale": " "}),
         "recurring_plan.rationale"),
        ("모르는 주기",
         lambda d: d.update(recurring_plan={
             "state": "STOP", "rationale": "중단", "period": "격주"}),
         "recurring_plan.period:"),
        ("모르는 통화",
         lambda d: d.update(recurring_plan={
             "state": "STOP", "rationale": "중단", "currency": "EUR"}),
         "recurring_plan.currency:"),
        ("금액이 0 이하",
         lambda d: d.update(recurring_plan={
             "state": "STOP", "rationale": "중단", "amount_per_period": 0}),
         "0보다 큰 수 또는 null"),
        ("CONTINUE 인데 금액·주기가 없음",
         lambda d: d.update(recurring_plan={
             "state": "CONTINUE", "rationale": "계속한다"}),
         "amount_per_period 와 period 가"),
    )


class TestPositioningRules(RuleTable, unittest.TestCase):
    """수급을 받고도 안 본 척하는 것을 계약이 허용하지 않는다."""

    table = (
        ("객체가 아님",
         lambda d: d.update(positioning="봤다"),
         "positioning: 객체 또는 null이어야 한다"),
        ("모르는 필드",
         lambda d: d.update(positioning={
             "checked": True, "finding": "외국인 순매도",
             "affects_conclusion": False, "느낌": "나쁨"}),
         "positioning: 알 수 없는 필드"),
        ("checked 가 불리언이 아님",
         lambda d: d.update(positioning={
             "checked": "yes", "affects_conclusion": False}),
         "positioning.checked"),
        ("확인했다면서 결과가 없음",
         lambda d: d.update(positioning={
             "checked": True, "affects_conclusion": False}),
         "확인했다면 무엇을 봤는지"),
        ("확인했는데 건너뛴 이유가 있음",
         lambda d: d.update(positioning={
             "checked": True, "finding": "외국인 순매도",
             "skipped_reason": "안 봤다", "affects_conclusion": False}),
         "확인했으면 건너뛴 이유가 없다"),
        ("건너뛰었는데 이유가 없음",
         lambda d: d.update(positioning={
             "checked": False, "affects_conclusion": False}),
         "건너뛰었다면 왜 결론을 바꾸지"),
        ("건너뛰었는데 결과가 있음",
         lambda d: d.update(positioning={
             "checked": False, "skipped_reason": "수급은 이익 동인이 아니다",
             "finding": "외국인 순매도", "affects_conclusion": False}),
         "확인하지 않았으면 결과가 없다"),
        ("affects_conclusion 이 불리언이 아님",
         lambda d: d.update(positioning={
             "checked": False, "skipped_reason": "결론을 못 바꾼다",
             "affects_conclusion": "아마도"}),
         "affects_conclusion"),
    )


class TestInvalidationSignalRules(RuleTable, unittest.TestCase):
    table = (
        ("신호가 하나도 없음",
         lambda d: d.update(invalidation_signals=[]),
         "invalidation_signals"),
        ("신호가 객체가 아님",
         lambda d: d.update(invalidation_signals=["현금흐름 악화"]),
         "invalidation_signals[0]"),
        ("모르는 행동",
         lambda d: d.update(invalidation_signals=[{
             "signal": "현금흐름 악화", "action": "생각한다",
             "rationale": "이유"}]),
         "invalidation_signals[0]"),
    )


class TestAssertionCheckRules(unittest.TestCase):
    """사용자 주장은 전제가 아니라 검증 대상이다. 열린 채로 저장될 수 없다."""

    def context_with_claim(self):
        context = valid_context()
        context["request"]["user_assertions"] = [
            {"claim_id": "UA-1", "claim": "2일 연속 하락했다"}
        ]
        context["context_id"] = context_id_for(context)
        return context

    def errors_for(self, checks):
        context = self.context_with_claim()
        data = valid_decision()
        data["context_id"] = context["context_id"]
        if checks is not None:
            data["user_assertion_checks"] = checks
        return validate_decision(data, context=context)

    def test_unanswered_claim_blocks_the_decision(self):
        errors = self.errors_for(None)
        self.assertTrue(
            any("각 주장을 확인·반박·확인불가" in e for e in errors), errors
        )

    def test_claim_id_not_in_the_packet_is_refused(self):
        errors = self.errors_for([{
            "claim_id": "UA-없는것", "status": "UNVERIFIABLE",
            "claim_kind": "PRICE", "basis": None, "finding": "확인 불가",
        }])
        self.assertTrue(
            any("packet에 없는 주장이다" in e for e in errors), errors
        )

    def test_unknown_status_is_refused(self):
        errors = self.errors_for([{
            "claim_id": "UA-1", "status": "아마도", "claim_kind": "PRICE",
            "basis": None, "finding": "글쎄",
        }])
        self.assertTrue(any(".status:" in e for e in errors), errors)

    def test_unknown_field_in_a_check_is_refused(self):
        errors = self.errors_for([{
            "claim_id": "UA-1", "status": "UNVERIFIABLE", "claim_kind": "PRICE",
            "basis": None, "finding": "확인 불가", "확신도": 0.5,
        }])
        self.assertTrue(
            any("claim_id·status·claim_kind" in e for e in errors), errors
        )

    def test_confirmed_needs_evidence(self):
        errors = self.errors_for([{
            "claim_id": "UA-1", "status": "CONFIRMED", "claim_kind": "PRICE",
            "basis": {"metric": "종가", "period": "2일", "comparison": "전일"},
            "finding": "확인했다",
        }])
        self.assertTrue(
            any("근거 없이 확인했다고 말할 수 없다" in e for e in errors), errors
        )

    def test_left_open_check_is_still_reported(self):
        """빈 배열로 '닫았다'고 우기는 경로."""
        errors = self.errors_for([])
        self.assertTrue(
            any("닫지 않은 사용자 주장이 있다" in e for e in errors), errors
        )


class TestMutationsAreIndependent(unittest.TestCase):
    """표의 각 항목이 서로를 오염시키지 않는지 — 픽스처는 매번 새것이어야 한다."""

    def test_fixture_is_rebuilt_for_every_row(self):
        first = valid_decision()
        first["price_plan"]["currency"] = "KRW"
        self.assertEqual(valid_decision()["price_plan"]["currency"], "USD")

    def test_deep_structures_are_not_shared(self):
        one, two = valid_decision(), valid_decision()
        one["drivers"][0]["weight_pct"] = 1
        self.assertNotEqual(
            one["drivers"][0]["weight_pct"], two["drivers"][0]["weight_pct"]
        )
        self.assertIsNot(one["expected_return_pct"], two["expected_return_pct"])


if __name__ == "__main__":
    unittest.main()
