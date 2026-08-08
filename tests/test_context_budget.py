"""예산을 넘을 때 packet 이 무엇을 먼저 버리는가.

이 파일이 생긴 이유는 `_fit_context_budget` 이 **실데이터로 한 번도 실행된 적이
없다**는 것이었다. 재 보니 구조적 최대치(가장 큰 packet + 살아 있는 판단 상한)
에서도 54,240 바이트로 예산의 68% 였다 — 트리머는 지금 도달하지 않는다.

도달하지 않는 코드를 테스트하는 이유는 두 가지다.

첫째, **도달하지 않는 이유가 상수 하나**다. `MAX_LIVE_ASSESSMENTS` 를 늘리면
그날 트리머가 처음 돌고, 그 경로의 마지막 단계는 직전 판단의 무효화 신호를
버린다. 사용자의 stop_style 이 THESIS 면 무효화 신호가 유일한 매도 규율이므로,
그것을 버린 packet 은 '조금 덜 실린 packet' 이 아니라 매도 판단의 축이 빠진
packet 이다. 처음 도는 날 그 순서가 맞는지 확인하기엔 늦다.

둘째, 버리는 순서는 **조용히** 틀릴 수 있다. 트리머는 성공하든 실패하든
packet 을 돌려주고, 빠진 자리는 결측과 구분되지 않는다.

여기서 고정하는 것은 순서다 — 편의값 → 서사 → 요청 목록 → 무효화 신호.
"""

import copy
import json
import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.context import (
    MAX_CONTEXT_JSON_BYTES,
    MAX_LIVE_ASSESSMENTS,
    _compact_size,
    _fit_context_budget,
)


def carried(**overrides):
    block = {
        "drivers": [{"name": "HBM", "weight_pct": 60}],
        "thesis": "가" * 400,
        "strongest_counter_thesis": "나" * 300,
        "price_plan": {"stop_price": 790000, "preferred_entry": 950000},
        "invalidation_signals": [
            {"signal": "다" * 200, "action": "EXIT"},
            {"signal": "라" * 200, "action": "TRIM_HALF"},
        ],
    }
    block.update(overrides)
    return block


def packet(*, filler=0, ladder=True, requests=1, carried_forward=None):
    """예산을 넘도록 `filler` 로 부풀린 packet."""
    live = [{"assessment_id": "CIO-old", "verdict": "WAIT"}]
    if carried_forward is not False:
        live.append({
            "assessment_id": "CIO-latest",
            "verdict": "BUY_NOW",
            "carried_forward": carried_forward or carried(),
        })
    return {
        "validation": {"optional_gaps": []},
        "context": {
            "prior_assessments": {"live_count": len(live), "live": live},
            "portfolio": ({"add_ladder": [{"qty": i} for i in range(40)]}
                          if ladder else {"add_ladder": None}),
            "open_research_requests": [
                {"request_id": f"REQ-{i}", "text": "마" * 60}
                for i in range(requests)
            ],
            # ASCII 로 채운다 — `_compact_size` 는 바이트를 세므로
            # 한글을 쓰면 문자당 3 바이트가 되어 초과량이 어긋난다.
            "_filler": "x" * filler,
        },
    }


def gap_of(result):
    return next(
        (g for g in result["validation"]["optional_gaps"]
         if g.get("state") == "TRIMMED_FOR_BUDGET"),
        None,
    )


def oversized(extra, **kwargs):
    """예산을 정확히 `extra` 바이트만큼 넘는 packet.

    filler 를 따로 계산하면 다른 인자(요청 개수 등)가 바뀔 때 초과량이 어긋난다.
    같은 인자로 한 번 재고 그 위에 채운다.
    """
    base = _compact_size(packet(filler=0, **kwargs))
    return packet(filler=MAX_CONTEXT_JSON_BYTES - base + extra, **kwargs)


class TestNothingIsTrimmedWhenItFits(unittest.TestCase):
    def test_a_packet_within_budget_is_untouched(self):
        original = packet(filler=100)
        result = copy.deepcopy(original)
        _fit_context_budget(result)
        self.assertEqual(result, original)

    def test_no_gap_is_recorded_when_nothing_was_trimmed(self):
        result = packet(filler=100)
        _fit_context_budget(result)
        self.assertIsNone(gap_of(result))


class TestTrimmingOrder(unittest.TestCase):
    """편의값 → 서사 → 요청 목록 → 무효화 신호."""

    # 실측한 계단이다. 초과 0 이면 사다리만, 800 이면 서사까지, 60,000 이면
    # 무효화 신호까지 간다. 트리밍 기록 자체가 packet 을 키우므로 사다리
    # 427 바이트로는 "조금 넘은" 상태도 다 못 메운다.
    def trim(self, extra, requests=6):
        result = oversized(extra, requests=requests)
        _fit_context_budget(result)
        return result

    def test_the_ladder_goes_first(self):
        """추가 수량별 평단은 CLI 로 다시 낼 수 있는 편의값이다."""
        result = self.trim(0)
        self.assertIsNone(result["context"]["portfolio"]["add_ladder"])
        self.assertEqual(gap_of(result)["trimmed"], ["portfolio.add_ladder"])

    def test_the_narrative_goes_before_the_thresholds(self):
        """지난번 논지의 문장은 원문을 열면 되지만 가격 문턱은 지금 대조한다."""
        result = self.trim(800)
        cf = result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]
        self.assertIsNone(cf["thesis"])
        self.assertIsNotNone(cf["price_plan"])

    def test_the_price_plan_is_never_trimmed(self):
        """가격 문턱은 이번 판단에서 바로 대조해야 한다."""
        result = self.trim(60_000)
        cf = result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]
        self.assertIsNotNone(cf["price_plan"])

    def test_invalidation_signals_are_the_last_thing_to_go(self):
        result = self.trim(800)
        cf = result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]
        self.assertIsNone(cf["thesis"])
        self.assertIsNotNone(cf["invalidation_signals"])

    def test_trimming_stops_as_soon_as_it_fits(self):
        """맞았는데도 계속 버리면 필요 없던 근거까지 사라진다."""
        result = self.trim(0)
        cf = result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]
        self.assertIsNotNone(cf["thesis"])

    def test_what_was_trimmed_is_recorded(self):
        result = self.trim(800)
        gap = gap_of(result)
        self.assertIn("prior_assessments.carried_forward.thesis",
                      gap["trimmed"])
        self.assertIn("knowledge/assessments/", gap["reason"])


class TestDroppingTheSellDiscipline(unittest.TestCase):
    """무효화 신호를 버렸다면 그 자리에 표시가 남는가.

    stop_style 이 THESIS 인 사용자에게 무효화 신호는 **유일한 매도 규율**이다.
    경고를 optional_gaps 에만 남기면 다른 잘라낸 항목들 사이에 묻히므로, 구멍이
    난 자리에 직접 쓴다.
    """

    def trim_hard(self):
        result = oversized(60_000, requests=8)
        _fit_context_budget(result)
        return result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]

    def test_the_signals_are_dropped_only_under_extreme_pressure(self):
        cf = self.trim_hard()
        self.assertIsNone(cf["invalidation_signals"])

    def test_a_marker_is_left_where_the_hole_is(self):
        cf = self.trim_hard()
        self.assertIn("invalidation_signals_dropped", cf)

    def test_the_marker_says_what_may_no_longer_be_answered(self):
        """'덜 실렸다'가 아니라 '무엇을 하면 안 되는지'가 있어야 한다."""
        note = self.trim_hard()["invalidation_signals_dropped"]
        self.assertIn("UNDETERMINED", note)
        self.assertIn("knowledge/assessments/", note)

    def test_open_requests_are_thinned_before_the_signals(self):
        result = oversized(4000, requests=20)
        _fit_context_budget(result)
        cf = result["context"]["prior_assessments"]["live"][-1][
            "carried_forward"]
        self.assertLess(len(result["context"]["open_research_requests"]), 20)
        self.assertIsNotNone(cf["invalidation_signals"])

    def test_at_least_two_requests_survive(self):
        """전부 지우면 '요청이 없다'로 읽힌다 — 목록은 남기고 개수를 줄인다."""
        result = oversized(60_000, requests=8)
        _fit_context_budget(result)
        self.assertGreaterEqual(
            len(result["context"]["open_research_requests"]), 2)


class TestTheInvariantThatKeepsTheBudgetSafe(unittest.TestCase):
    """트리머가 도달하지 않는 이유는 상수 하나다."""

    def test_the_live_assessment_cap_is_small(self):
        """이 숫자를 늘리면 그날 트리머가 처음 돌고 매도 규율을 버린다.

        실측으로 판단 1건은 carried_forward 를 달면 5,786 바이트, 없으면
        1,659 바이트다. 상한이 3 이라 `prior_assessments` 는 9 KB 근처에서
        멈추고, 그래서 가장 큰 packet 도 예산의 68% 였다.
        """
        self.assertLessEqual(MAX_LIVE_ASSESSMENTS, 3)

    def test_a_packet_at_the_structural_maximum_still_fits(self):
        """상한만큼 판단을 채운 packet 이 예산 안에 있는가."""
        live = [{"assessment_id": f"CIO-{i}", "verdict": "WAIT",
                 "drivers": [{"name": "가" * 40}] * 3}
                for i in range(MAX_LIVE_ASSESSMENTS - 1)]
        live.append({"assessment_id": "CIO-latest", "verdict": "BUY_NOW",
                     "carried_forward": carried()})
        result = {
            "validation": {"optional_gaps": []},
            "context": {"prior_assessments": {"live": live},
                        "portfolio": {"add_ladder": None},
                        "open_research_requests": []},
        }
        _fit_context_budget(result)
        self.assertIsNone(gap_of(result))
        self.assertLess(_compact_size(result), MAX_CONTEXT_JSON_BYTES)


class TestTrimmingIsAppliedToTheLatestJudgement(unittest.TestCase):
    """서사를 버리는 대상이 '지금 살아 있는 계획'인가.

    `carried_forward` 는 가장 최근 판단 한 건에만 붙는다. 트리머는 `next()` 로
    첫 번째 보유 행을 고르므로, 그 전제가 깨지면 엉뚱한 판단의 논지를 버리고
    최신 판단은 그대로 남는다 — packet 은 정상으로 보인다.
    """

    def test_only_the_row_that_carries_forward_is_touched(self):
        result = oversized(800, requests=6)
        _fit_context_budget(result)
        rows = result["context"]["prior_assessments"]["live"]
        self.assertNotIn("carried_forward", rows[0])
        self.assertIsNone(rows[-1]["carried_forward"]["thesis"])

    def test_a_packet_without_any_carried_forward_does_not_crash(self):
        """직전 판단이 없거나 계약 미달이면 실을 논지 자체가 없다."""
        result = oversized(800, carried_forward=False)
        _fit_context_budget(result)
        self.assertIsNotNone(gap_of(result))


if __name__ == "__main__":
    unittest.main()
