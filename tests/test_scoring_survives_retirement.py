"""폐기해도 track record 는 남는다 — 채점 계약과 인용 계약의 분리.

실제로 이렇게 잃을 뻔했다. 삼성전자 2026-07-29/07-30 판단 두 건이 계약 변경으로
폐기됐고, `cio score 005930` 은 0건을 돌려줬다. 그 사이 가격은 +20% 였다.

폐기의 뜻은 **"근거로 인용하지 않는다"**이지 "그런 예측을 한 적이 없다"가
아니다. 둘을 하나로 묶으면 계약을 개선할 때마다 과거를 잃는다 — 모델이
좋아질수록 강해지는 시스템의 정반대다.
"""
from __future__ import annotations

import datetime as dt
import json
import tempfile
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.decision import normalize_decision
from cio.memory import (
    FIXED_HORIZON_MONTHS,
    SCORING_REQUIRED,
    record_decision_file,
    score_asset,
    scoring_contract_errors,
)
from test_cio_core import valid_context, valid_decision


def measurable():
    """채점에 필요한 최소 형태. 현재 판단 계약은 통과하지 못한다."""
    return {
        "assessment_id": "CIO-3b54fdbb429e750d6ed1",
        "context_id": "CTX-00000000000000000000",
        "asset_id": "005930",
        "verdict": "WAIT",
        "outcome_metric": "TERMINAL_TOTAL_RETURN_ASSET_CURRENCY",
        "horizon_months": 18,
        "win_return_threshold_pct": 30.0,
        "failure_return_threshold_pct": -20.0,
        "expected_return_pct": {"probability_weighted": -3.2},
        "price_plan": {"stop_price": 152000, "scale_out_start": 285000},
    }


class TestScoringContractIsSmaller(unittest.TestCase):
    def test_measurable_judgment_passes_scoring_contract(self):
        self.assertEqual(scoring_contract_errors(measurable()), [])

    def test_it_does_not_demand_the_full_decision_contract(self):
        """채점은 뒤를 보는 측정이다. 인용에 필요한 필드까지 요구하지 않는다.

        drivers·method·thesis·sources 가 없어도 '그때 무엇을 예측했는가'는
        계산할 수 있다.
        """
        for field in ("drivers", "method", "thesis", "sources", "action"):
            self.assertNotIn(field, SCORING_REQUIRED)

    def test_missing_horizon_is_not_measurable(self):
        data = measurable()
        data["horizon_months"] = None
        self.assertTrue(scoring_contract_errors(data))

    def test_different_outcome_metric_cannot_be_compared(self):
        """같은 자로 재지 않으면 나란히 놓을 수 없다."""
        data = measurable()
        data["outcome_metric"] = "SOMETHING_ELSE"
        errors = scoring_contract_errors(data)
        self.assertTrue(any("같은 자로" in error for error in errors), errors)

    def test_non_numeric_threshold_is_rejected(self):
        data = measurable()
        data["win_return_threshold_pct"] = "많이"
        self.assertTrue(scoring_contract_errors(data))


class TestCitabilityIsSeparateFromMeasurability(unittest.TestCase):
    """계약 미달이 곧 '채점 불가'는 아니다.

    폐기된 판단에만 관대한 채점 계약을 쓰고, 계약 미달인 live 판단은 통째로
    버리면 같은 원칙이 반쪽만 적용된다. 실제로 `positioning` 을 필수로 만들자
    파마리서치 판단이 채점에서 사라졌고, 그것이 이 구분을 만든 계기다.
    """

    ROWS = [
        {"date": day, "close": close, "adjusted_close": close,
         "has_adjusted_close": True, "split_adjustment_factor": 1.0,
         "high": close + 1, "low": close - 1}
        for day, close in (("2025-12-31", 100.0), ("2026-01-02", 104.0),
                           ("2026-02-02", 112.0))
    ]

    def score(self):
        """살아 있는 판단 하나와 폐기된 판단 하나를 임시 저장소에 두고 채점한다.

        예전에는 실제 저장소의 214450·005930 을 채점했다. 그러면 테스트를 돌릴
        때마다 진짜 평가 파일이 다시 쓰이고, 사용자가 삼성전자 판단을 새로 하나
        기록하는 순간 "살아 있는 평가가 없다"는 단언이 깨진다 — 코드가 아니라
        그날의 보유 기록을 시험하는 테스트였다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            knowledge = root / "knowledge"
            context = valid_context()
            context_path = (
                knowledge / "contexts" / "005930" / f"{context['context_id']}.json")
            context_path.parent.mkdir(parents=True)
            context_path.write_text(json.dumps(context), encoding="utf-8")
            decision_path = root / "decision.json"
            decision_path.write_text(json.dumps(valid_decision()), encoding="utf-8")
            old = valid_decision()
            old["thesis"] = "폐기된 옛 판단의 논지"
            retired = normalize_decision(old)
            retired_path = (
                knowledge / "retired" / "005930" / f"{retired['assessment_id']}.json")
            retired_path.parent.mkdir(parents=True)
            retired_path.write_text(json.dumps({**retired, "_retired": {
                "retired_at": "2026-08-01T09:00:00+09:00", "class": "VALID",
                "reason": "구버전 산물"}}), encoding="utf-8")
            with patch("cio.memory.ROOT", root), \
                    patch("cio.memory.KNOWLEDGE_DIR", knowledge), \
                    patch("cio.decision.ROOT", root), \
                    patch("cio.decision.KNOWLEDGE_DIR", knowledge), \
                    patch("cio.memory._price_rows", return_value=self.ROWS), \
                    patch("cio.memory._price_session", return_value=None):
                live = json.loads(record_decision_file(
                    decision_path,
                    now=dt.datetime.fromisoformat("2026-07-28T10:05:00+09:00"),
                ).read_text(encoding="utf-8"))
                report = score_asset("005930")
                stored = {
                    path.stem: json.loads(path.read_text(encoding="utf-8"))
                    for path in (knowledge / "evaluations" / "005930").glob("EVAL-*.json")
                }
        return report, live["assessment_id"], retired["assessment_id"], stored

    def test_score_result_separates_the_three_buckets(self):
        report, _live, _retired, _stored = self.score()
        for bucket in (
            "evaluated",
            "scored_but_not_citable",
            "retired_evaluated",
            "skipped",
        ):
            self.assertIn(bucket, report)

    def test_retired_judgments_are_not_mixed_into_evaluated(self):
        """그때의 계약과 prompt 가 지금과 달랐으므로 같은 통에 넣지 않는다."""
        report, live, retired, stored = self.score()
        self.assertEqual([row["assessment_id"] for row in report["evaluated"]], [live])
        self.assertEqual(
            [row["assessment_id"] for row in report["retired_evaluated"]], [retired])
        sources = {data["assessment_id"]: (data["source"], data["citable"])
                   for data in stored.values()}
        self.assertEqual(sources, {live: ("LIVE", True), retired: ("RETIRED", False)})


class TestFixedHorizonsAreModelIndependent(unittest.TestCase):
    def test_the_fixed_ladder_is_what_the_review_asked_for(self):
        """모델이 기간을 스스로 정하면 모델끼리 비교가 불가능하다.

        쉬운 문턱을 고르는 것만으로 점수가 좋아지므로, 승격 비교는 고정된
        자로 한다.
        """
        self.assertEqual(FIXED_HORIZON_MONTHS, (1, 3, 6, 12, 24))


if __name__ == "__main__":
    unittest.main()
