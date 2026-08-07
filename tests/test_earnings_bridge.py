"""분기 실적 브리지 — 이미 로컬에 있는 것을 packet 이 지우지 않게 한다.

수집기는 8분기를 만들어 저장하는데 packet 은 TTM 네 숫자만 실어 보냈다.
그래서 005930 의 영업이익 4.68조 → 12.17조 → 20.07조 → 57.23조 가속이
packet 에서 사라졌고, 직전 세션은 그 가속을 확인하려고 DART 원문을 다시
뒤졌다. **근거의 절반이 이미 로컬에 있었고 packet 이 지웠다.**

두 번째 구멍은 TTM 이 언제까지의 숫자인지가 그 숫자 옆에 없다는 것이다.
로컬 TTM 영업이익 94.1조 vs 이미 공시된 실제 179.0조 — 1.9배 차이가
`validation.optional_gaps` 한 줄에만 있었고, 모델이 주의 깊게 읽어서 겨우
잡았다. **주의력에 의존하는 안전장치는 안전장치가 아니다.** 기준일과
그 뒤로 지나간 결산 분기·나온 공시는 표 안에 붙어 있어야 한다.

브리지는 산술만 한다 — 합·차·비율이고 "가속"·"좋다"는 여기서 나오지 않는다.
"""

from __future__ import annotations

import datetime as dt
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.context import _earnings_bridge, _mark_bridge_disclosures

# 005930 의 실제 분기 영업이익(조원). 이 8개만 봐도 가속이 보인다.
OPERATING = [10.443878, 9.183371, 6.492703, 6.685272,
             4.676057, 12.166062, 20.073660, 57.232797]
REVENUE = [74.068, 79.100, 75.799, 79.140, 74.611, 86.100, 93.400, 112.700]
PERIODS = [(2024, 2), (2024, 3), (2024, 4), (2025, 1),
           (2025, 2), (2025, 3), (2025, 4), (2026, 1)]
TRILLION = 1e12


def _quarter_rows(values, derived_quarters=(4,)):
    rows = []
    for (fy, quarter), value in zip(PERIODS, values):
        end = dt.date(fy, quarter * 3, 30 if quarter in (2, 3) else 31)
        rows.append(
            {
                "fy": fy,
                "quarter": quarter,
                "end": end.isoformat(),
                "filed": None,
                "val": round(value * TRILLION, 6),
                "derived": quarter in derived_quarters,
            }
        )
    return rows


def fundamentals(**overrides):
    data = {
        "instrument_id": "005930",
        "currency": "KRW",
        "period_end": "2026-03-31",
        "income_statement": {
            "revenue": {"tag": "dart_Revenue", "quarters": _quarter_rows(REVENUE)},
            "operating_income": {
                "tag": "dart_OperatingIncomeLoss",
                "quarters": _quarter_rows(OPERATING),
            },
        },
        "quarters": {
            "ocf": _quarter_rows([0] * 4 + [20.0, 22.5, 28.8, 40.2]
                                 )[4:],
            "capex": _quarter_rows([0] * 4 + [12.0, 10.8, 11.5, 17.1])[4:],
        },
    }
    data.update(overrides)
    return data


def _rows(bridge):
    return {row["period"]: row for row in bridge["rows"]}


class TestQuartersSurviveCompression(unittest.TestCase):
    """압축은 삭제가 아니라 변환이다."""

    def test_all_eight_quarters_reach_the_packet(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        self.assertEqual(len(bridge["rows"]), 8)
        self.assertEqual(
            [row["period"] for row in bridge["rows"]],
            ["2024Q2", "2024Q3", "2024Q4", "2025Q1",
             "2025Q2", "2025Q3", "2025Q4", "2026Q1"],
        )

    def test_the_acceleration_is_readable_without_reopening_dart(self):
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        actual = [
            round(rows[period]["operating_income"] / TRILLION, 2)
            for period in ("2025Q2", "2025Q3", "2025Q4", "2026Q1")
        ]
        self.assertEqual(actual, [4.68, 12.17, 20.07, 57.23])

    def test_derived_quarters_are_labelled(self):
        """4분기는 연간 − 3분기누계로 만든 값이라 실측과 같이 두면 안 된다."""
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        self.assertIn("operating_income", rows["2025Q4"]["derived"])
        self.assertNotIn("derived", rows["2026Q1"])

    def test_bridge_is_small_enough_to_carry_every_time(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        size = len(json.dumps(bridge, ensure_ascii=False,
                              separators=(",", ":")).encode("utf-8"))
        self.assertLess(size, 4_000, f"브리지가 {size} bytes 다 — 예산의 5%를 넘는다")


class TestArithmetic(unittest.TestCase):
    def test_yoy_compares_the_same_quarter(self):
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        expected = (57.232797 / 6.685272 - 1) * 100
        self.assertAlmostEqual(
            rows["2026Q1"]["operating_income_yoy_pct"], round(expected, 1), places=1
        )

    def test_qoq_compares_the_previous_quarter(self):
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        expected = (57.232797 / 20.073660 - 1) * 100
        self.assertAlmostEqual(
            rows["2026Q1"]["operating_income_qoq_pct"], round(expected, 1), places=1
        )

    def test_first_year_has_no_yoy(self):
        """앞선 4분기에는 비교할 상대가 없다. 지어내지 않는다."""
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        self.assertNotIn("operating_income_yoy_pct", rows["2024Q2"])

    def test_operating_margin(self):
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        self.assertAlmostEqual(
            rows["2026Q1"]["operating_margin_pct"],
            round(57.232797 / 112.700 * 100, 1),
            places=1,
        )

    def test_fcf_is_ocf_minus_capex_per_quarter(self):
        rows = _rows(_earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7)))
        self.assertAlmostEqual(
            rows["2026Q1"]["fcf"], (40.2 - 17.1) * TRILLION, places=0
        )

    def test_growth_from_a_loss_is_not_a_percentage(self):
        """적자에서 흑자로 가는 변화율은 해석 불가능한 수다.

        -100 → +50 을 "+150%"로 적으면 숫자는 나오지만 뜻이 없다. 원값이
        바로 옆에 있으므로 비율을 지어내는 대신 비운다.
        """
        loss = list(OPERATING)
        loss[3] = -3.0
        data = fundamentals()
        data["income_statement"]["operating_income"]["quarters"] = _quarter_rows(loss)
        rows = _rows(_earnings_bridge(data, today=dt.date(2026, 8, 7)))
        self.assertNotIn("operating_income_yoy_pct", rows["2026Q1"])
        self.assertLess(rows["2025Q1"]["operating_income"], 0)


class TestMissingIsSaidOutLoud(unittest.TestCase):
    def test_metric_with_no_quarters_is_named(self):
        """IBM 표본에는 영업이익이 통째로 없다. 조용히 비어 있으면 안 된다."""
        data = fundamentals()
        data["income_statement"]["operating_income"] = {"tag": None, "quarters": []}
        bridge = _earnings_bridge(data, today=dt.date(2026, 8, 7))
        self.assertIn("operating_income", bridge["metrics_absent"])

    def test_no_quarterly_data_at_all_yields_no_bridge(self):
        data = fundamentals(income_statement={}, quarters={})
        self.assertIsNone(_earnings_bridge(data, today=dt.date(2026, 8, 7)))


class TestStalenessSitsNextToTheNumbers(unittest.TestCase):
    """94.1조를 읽는 자리에서 그것이 3월까지의 숫자임을 보게 한다."""

    def test_covered_through_is_the_local_period_end(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        self.assertEqual(bridge["covered_through"], "2026-03-31")

    def test_completed_quarters_after_the_local_period_are_counted(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        self.assertEqual(bridge["quarter_ends_after_covered"], ["2026-06-30"])

    def test_no_elapsed_quarter_means_an_empty_list(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 4, 20))
        self.assertEqual(bridge["quarter_ends_after_covered"], [])

    def test_disclosures_after_the_local_period_are_attached_to_the_table(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        _mark_bridge_disclosures(
            bridge,
            {
                "local_period_end": "2026-03-31",
                "disclosures_after_local_period": [
                    {
                        "form": "PROVISIONAL_RESULT",
                        "filing_date": "2026-07-31",
                        "description": "연결재무제표기준영업(잠정)실적",
                        "document_url": "https://dart.fss.or.kr/x",
                    }
                ],
            },
        )
        self.assertEqual(len(bridge["unreflected_result_disclosures"]), 1)
        self.assertIn("2026-07-31", json.dumps(bridge, ensure_ascii=False))
        self.assertIn("_warning", bridge)

    def test_without_newer_disclosures_no_warning_is_manufactured(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 4, 20))
        _mark_bridge_disclosures(
            bridge,
            {"local_period_end": "2026-03-31", "disclosures_after_local_period": []},
        )
        self.assertEqual(bridge["unreflected_result_disclosures"], [])
        self.assertNotIn("_warning", bridge)


class TestJudgmentBoundary(unittest.TestCase):
    """브리지는 합·차·비율만 낸다. 추세 판정은 모델의 몫이다."""

    FORBIDDEN = (
        "가속", "둔화", "개선", "악화", "호실적", "어닝쇼크", "서프라이즈",
        "싸다", "비싸다", "저평가", "고평가", "매수", "매도", "추천",
        "ACCELERAT", "IMPROV", "DETERIORAT", "BEAT", "MISS", "TREND",
    )

    @staticmethod
    def _data_only(node):
        if isinstance(node, dict):
            return {
                key: TestJudgmentBoundary._data_only(value)
                for key, value in node.items()
                if not key.startswith("_")
            }
        if isinstance(node, list):
            return [TestJudgmentBoundary._data_only(item) for item in node]
        return node

    def test_no_verdict_vocabulary_in_the_data(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        blob = json.dumps(self._data_only(bridge), ensure_ascii=False)
        for word in self.FORBIDDEN:
            self.assertNotIn(word, blob, f"브리지가 판정 어휘 '{word}' 를 냈다")

    def test_no_trend_field(self):
        bridge = _earnings_bridge(fundamentals(), today=dt.date(2026, 8, 7))
        blob = json.dumps(bridge, ensure_ascii=False)
        for field in ("trend", "direction", "momentum", "is_", "should_", "verdict"):
            self.assertNotIn(f'"{field}', blob)


if __name__ == "__main__":
    unittest.main()
