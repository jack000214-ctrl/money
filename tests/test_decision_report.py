"""`cio show` 는 판단을 옮기기만 한다.

이 화면이 존재하는 이유는 하나다 — `cio record` 뒤에 판단을 읽을 방법이 raw
JSON 뿐이었다. 그런데 사용자의 `stop_style` 이 `THESIS` 면 **무효화 신호가
유일한 매도 규율**이므로, 그 신호가 화면에서 빠지면 안전장치가 통째로 사라진다.

그래서 여기서 고정하는 것은 미관이 아니라 두 가지다.
1. 모델이 쓴 것이 빠짐없이 나오는가 (무효화 신호·모르는 것·반대 논지)
2. 화면이 스스로 판정을 만들지 않는가
"""

import unittest
from unittest.mock import patch

from cio.decision import normalize_decision
from cio.report import cell_width, format_assessment
from test_cio_core import valid_decision

ASSET = {"asset_id": "TSLA", "name": "Tesla", "price_symbol": "TSLA"}


def _render(decision=None, *, status="LIVE", warnings=None, evaluation=None):
    data = normalize_decision(decision or valid_decision())
    with (
        patch("cio.report.get_asset", return_value=ASSET),
        patch("cio.report._find_evaluation", return_value=evaluation),
    ):
        return format_assessment(data, status=status, warnings=warnings or [])


class TestNothingTheModelWroteIsDropped(unittest.TestCase):
    def test_every_invalidation_signal_is_shown(self):
        """하나라도 빠지면 THESIS 매도 규율에 구멍이 난다."""
        decision = valid_decision()
        decision["invalidation_signals"] = [
            {
                "signal": "분기 영업이익률이 20% 아래로 내려간다",
                "action": "EXIT",
                "rationale": "정상화 이익의 근거가 그 범위였다",
            },
            {
                "signal": "주요 고객사가 공급 계약을 해지한다",
                "action": "TRIM_HALF",
                "rationale": "매출의 절반이 그 고객에서 나온다",
            },
            {
                "signal": "재고자산 회전일수가 4분기 연속 늘어난다",
                "action": "REASK",
                "rationale": "선행 신호이지 확정 사실이 아니다",
            },
        ]
        output = _render(decision)
        for row in decision["invalidation_signals"]:
            self.assertIn(row["signal"], output)
        self.assertIn("무효화 신호 (3)", output)

    def test_the_committed_action_is_shown_with_the_signal(self):
        """신호만 읽히고 약속이 안 읽히면 매도를 만드는 쪽이 빠진다."""
        decision = valid_decision()
        decision["invalidation_signals"] = [
            {
                "signal": "공사손실충당부채가 설정된다",
                "action": "TRIM_HALF",
                "rationale": "반대 논지가 현실이 되는 자리다",
            }
        ]
        output = _render(decision)
        self.assertIn("절반 축소", output)
        self.assertIn("반대 논지가 현실이 되는 자리다", output)

    def test_all_reask_signals_are_flagged_as_having_no_sell_rule(self):
        """전부 REASK 면 THESIS 에서 매도 지시가 나올 자리가 없다."""
        decision = valid_decision()
        decision["invalidation_signals"] = [
            {
                "signal": "수주잔고가 두 분기 연속 감소한다",
                "action": "REASK",
                "rationale": "추세를 더 봐야 한다",
            }
        ]
        output = _render(decision)
        self.assertIn("보유분을 줄이는 신호가 하나도 없다", output)

    def test_breaking_a_prior_commitment_is_shown_as_broken(self):
        """약속을 뒤집는 것은 허용하되 조용히 뒤집는 것은 허용하지 않는다."""
        decision = valid_decision()
        decision["prior_signal_checks"] = [
            {
                "signal": "분기 영업이익률이 8% 아래로 내려간다",
                "state": "FIRED",
                "committed_action": "EXIT",
                "taken_action": "TRIM_HALF",
                "override_reason": "일회성 충당금이며 현금 유출이 아니다",
                "note": "2Q 영업이익률 7.4%로 확인",
            }
        ]
        output = _render(decision)
        self.assertIn("직전 약속 정산 (1)", output)
        self.assertIn("약속 파기", output)
        self.assertIn("일회성 충당금이며 현금 유출이 아니다", output)

    def test_unknowns_are_never_collapsed(self):
        """모르는 것을 접으면 확신도가 근거 없이 올라간다."""
        decision = valid_decision()
        decision["unknowns"] = [f"모르는 항목 {n}" for n in range(1, 6)]
        output = _render(decision)
        for item in decision["unknowns"]:
            self.assertIn(item, output)
        self.assertIn("모르는 것 (5)", output)

    def test_counter_thesis_is_rendered_not_summarized(self):
        """반대 논지를 줄이면 화면이 확증 편향의 편을 든다."""
        decision = valid_decision()
        decision["strongest_counter_thesis"]["text"] = (
            "수요가 구조적이면 정상화 회귀라는 틀 자체가 틀렸다"
        )
        output = _render(decision)
        self.assertIn("가장 강한 반대 논지", output)
        self.assertIn(
            "수요가 구조적이면 정상화 회귀라는 틀 자체가 틀렸다", output
        )

    def test_probability_sits_next_to_the_verdict(self):
        """승률을 따로 떼면 45% 판단이 확신처럼 읽힌다."""
        output = _render()
        conclusion = output.split("■ 논지")[0]
        self.assertIn("WAIT", conclusion)
        self.assertIn("55.0%", conclusion)
        self.assertIn("60.0%", conclusion)


class TestTheScreenDoesNotJudge(unittest.TestCase):
    def test_verdict_is_copied_verbatim_and_not_relabelled(self):
        """모델의 verdict 를 화면이 다른 말로 바꾸지 않는다.

        이전 대시보드는 PC 가 낸 `BLOCKED` 를 '추가 매수 차단'이라는 투자
        지시로 렌더링했다. 데이터 신선도 검사가 헤드라인에서 매매 지시가 되는
        것이 이 저장소가 가장 공들여 없앤 경계 침범이다.
        """
        for verdict in ("BUY_NOW", "WAIT", "AVOID"):
            decision = valid_decision()
            decision["verdict"] = verdict
            if verdict == "BUY_NOW":
                decision["proposed_trade"]["amount"] = 1000.0
            output = _render(decision)
            self.assertIn(verdict, output)
            for forbidden in ("차단", "매수 금지", "추천", "안전"):
                self.assertNotIn(forbidden, output)

    def test_no_computed_grade_or_threshold_appears(self):
        """'싸다/비싸다', 등급, 자체 임계값을 화면이 만들어 내지 않는다."""
        output = _render()
        for invented in ("저평가", "고평가", "적정주가", "등급", "점수"):
            self.assertNotIn(invented, output)


class TestContractStateIsVisible(unittest.TestCase):
    def test_contract_failing_decision_is_marked(self):
        """파일은 남아 있어 직접 열면 살아 있는 판단처럼 보인다."""
        output = _render(status="CONTRACT_FAILURE", warnings=["사유: 필드 누락"])
        self.assertIn("계약 미달", output)
        self.assertIn("사유: 필드 누락", output)

    def test_wait_points_at_the_counterfactual_not_the_ask_return(self):
        """WAIT 를 질문가 수익률로 읽으면 안 산 포지션을 채점하게 된다."""
        evaluation = {
            "window": {
                "start_date": "2026-01-01",
                "latest_available_date": "2026-03-01",
                "complete": False,
            },
            "price": {"return_to_end_pct": 12.0},
            "counterfactual": {
                "waited_for_entry": {
                    "entry_price": 95.0,
                    "filled": False,
                    "fill": None,
                    "return_to_end_pct": None,
                    "advantage_pct": -12.0,
                }
            },
        }
        output = _render(evaluation=evaluation)
        self.assertIn("미체결 — 포지션이 없었다", output)
        self.assertIn("WAIT·AVOID 는 질문가 수익률이 아니라", output)

    def test_missing_evaluation_says_so_instead_of_implying_success(self):
        output = _render(evaluation=None)
        self.assertIn("아직 없다", output)


class TestKoreanWidth(unittest.TestCase):
    def test_hangul_counts_as_two_cells(self):
        """한글을 한 칸으로 세면 접는 폭이 두 배로 어긋난다."""
        self.assertEqual(cell_width("abc"), 3)
        self.assertEqual(cell_width("한글"), 4)
        self.assertEqual(cell_width("한글 abc"), 8)

    def test_wrapped_body_stays_within_the_column(self):
        decision = valid_decision()
        decision["thesis"] = "현재 가격보다 낮은 진입가에서 보상비가 낫다. " * 12
        output = _render(decision)
        body = output.split("■ 논지")[1].split("■")[0]
        for line in body.split("\n"):
            self.assertLessEqual(cell_width(line), 78, line)


if __name__ == "__main__":
    unittest.main()
