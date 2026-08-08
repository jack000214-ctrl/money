"""정상화 이익 계산기의 산술 계약.

이 계산기의 출력(`NORM-...`)은 판단의 `method`에 근거 ID로 인용된다 —
"현재 TTM 이 전기간 평균의 5.02배" 같은 문장이 그대로 밸류에이션 방법 선택의
이유가 된다. 그런데 테스트가 한 줄도 없었다.

여기서 지키는 성질은 셋이다.
  1. **끊긴 분기를 잇지 않는다** — 연속 4분기가 없으면 TTM 을 만들지 않는다
  2. **저점이 음수면 진폭을 내지 않는다** — 배수가 성립하지 않는다고 말한다
  3. **정상화 기준을 고르지 않는다** — 평균·중앙값·마진 기반을 나란히 낸다
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.normalized_earnings import (
    normalize,
    normalized_id_for,
    scale_for,
    ttm_series,
)


def series(pairs):
    """{(fy, quarter): 값} 형태로 접는다."""
    return {(fy, q): float(v) for (fy, q), v in pairs}


def four_quarters(fy, ocf, capex, revenue=None):
    o = series([((fy, q), ocf[q - 1]) for q in (1, 2, 3, 4)])
    c = series([((fy, q), capex[q - 1]) for q in (1, 2, 3, 4)])
    r = series([((fy, q), revenue[q - 1]) for q in (1, 2, 3, 4)]) if revenue else {}
    return o, c, r


class TestTtmSeries(unittest.TestCase):
    def test_four_continuous_quarters_make_one_ttm_point(self):
        ocf, capex, revenue = four_quarters(
            2025, [10, 20, 30, 40], [1, 2, 3, 4], [100, 100, 100, 100]
        )
        rows = ttm_series(ocf, capex, revenue)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ocf_ttm"], 100.0)
        self.assertEqual(rows[0]["capex_ttm"], 10.0)
        self.assertEqual(rows[0]["fcf_ttm"], 90.0)
        self.assertEqual(rows[0]["fcf_margin_pct"], 22.5)

    def test_a_hole_in_the_window_drops_the_point_instead_of_bridging_it(self):
        """빠진 분기를 건너뛰고 이어 붙이면 TTM 이 3분기 합이 된다."""
        ocf, capex, _ = four_quarters(2025, [10, 20, 30, 40], [1, 2, 3, 4])
        del ocf[(2025, 2)]
        self.assertEqual(ttm_series(ocf, capex, {}), [])

    def test_the_window_crosses_the_fiscal_year_boundary(self):
        """2025 4분기 ~ 2026 3분기도 연속 네 분기다."""
        ocf, capex, _ = four_quarters(2025, [10, 10, 10, 10], [1, 1, 1, 1])
        ocf2, capex2, _ = four_quarters(2026, [20, 20, 20, 20], [2, 2, 2, 2])
        ocf.update(ocf2)
        capex.update(capex2)
        rows = ttm_series(ocf, capex, {})
        ends = [row["period_end"] for row in rows]
        self.assertIn("2026-03-31", ends)
        crossing = next(r for r in rows if r["period_end"] == "2026-06-30")
        # 2025Q3·Q4(10+10) + 2026Q1·Q2(20+20)
        self.assertEqual(crossing["ocf_ttm"], 60.0)

    def test_revenue_gap_empties_the_margin_but_keeps_the_point(self):
        """매출이 없어도 FCF 는 있다. 마진만 비운다."""
        ocf, capex, revenue = four_quarters(
            2025, [10, 10, 10, 10], [1, 1, 1, 1], [100, 100, 100, 100]
        )
        del revenue[(2025, 3)]
        rows = ttm_series(ocf, capex, revenue)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["fcf_ttm"], 36.0)
        self.assertIsNone(rows[0]["revenue_ttm"])
        self.assertIsNone(rows[0]["fcf_margin_pct"])

    def test_collector_supplied_period_end_wins_over_the_calendar(self):
        """결산월이 12월이 아닌 회사의 분기말을 달력으로 되돌리면 안 된다."""
        ocf, capex, _ = four_quarters(2025, [10, 10, 10, 10], [1, 1, 1, 1])
        rows = ttm_series(ocf, capex, {}, ends={(2025, 4): "2026-01-31"})
        self.assertEqual(rows[0]["period_end"], "2026-01-31")


class TestNormalize(unittest.TestCase):
    def rows(self, fcfs):
        return [
            {"period_end": f"2025-{i:02d}-30", "fcf_ttm": float(v),
             "revenue_ttm": 1000.0, "fcf_margin_pct": round(v / 1000 * 100, 2)}
            for i, v in enumerate(fcfs, start=1)
        ]

    def test_amplitude_is_peak_over_trough_when_the_trough_is_positive(self):
        result = normalize(self.rows([10, 40, 20]))
        self.assertEqual(result["amplitude_ratio"], 4.0)
        self.assertEqual(result["peak"]["fcf_ttm"], 40.0)
        self.assertEqual(result["trough"]["fcf_ttm"], 10.0)

    def test_negative_trough_refuses_the_ratio_and_says_why(self):
        """저점 FCF 가 음수인 회사는 정상화 없이 DCF 가 음수 가치를 낸다.

        여기서 0 이나 임의의 큰 수를 넣으면 그 사실이 출력에서 사라진다.
        """
        result = normalize(self.rows([-10, 40, 20]))
        self.assertIsNone(result["amplitude_ratio"])
        self.assertIn("음수", result["amplitude_note"])

    def test_zero_trough_also_refuses_the_ratio(self):
        """0으로 나누지 않는다."""
        result = normalize(self.rows([0, 40, 20]))
        self.assertIsNone(result["amplitude_ratio"])

    def test_three_bases_are_offered_and_none_is_chosen(self):
        """어느 것을 쓸지는 이 계산기의 일이 아니다."""
        result = normalize(self.rows([10, 40, 20, 50]))
        self.assertEqual(
            set(result["bases"]),
            {"mean_ttm", "median_ttm", "mean_margin_on_current_revenue"},
        )
        self.assertEqual(result["bases"]["mean_ttm"], 30.0)
        self.assertEqual(result["bases"]["median_ttm"], 30.0)

    def test_current_over_mean_is_the_cycle_position_gauge(self):
        result = normalize(self.rows([10, 10, 10, 50]))
        self.assertEqual(result["current_fcf_ttm"], 50.0)
        self.assertEqual(result["current_vs_mean_x"], 2.5)

    def test_negative_mean_leaves_the_gauge_empty_rather_than_flipping_sign(self):
        """평균이 음수면 배수의 부호가 뒤집혀 '싸 보이는' 값이 나온다."""
        result = normalize(self.rows([-40, -20, -30, 10]))
        self.assertIsNone(result["current_vs_mean_x"])

    def test_window_reports_the_span_it_actually_used(self):
        result = normalize(self.rows([10, 20, 30]))
        self.assertEqual(result["window"]["ttm_points"], 3)
        self.assertEqual(result["window"]["from"], "2025-01-30")
        self.assertEqual(result["window"]["to"], "2025-03-30")


class TestDisplayScale(unittest.TestCase):
    """조원 고정이면 수천억짜리 회사는 전 구간이 '0.1조'로 뭉개진다."""

    def test_scale_follows_the_biggest_absolute_value(self):
        for biggest, expected in ((5e13, "조"), (5e11, "억"), (5e6, "백만")):
            with self.subTest(biggest=biggest):
                _factor, unit = scale_for([{"fcf_ttm": biggest}])
                self.assertEqual(unit, expected)

    def test_a_large_negative_trough_still_sets_the_scale(self):
        """절댓값으로 봐야 -9.7조가 '0백만'으로 찍히지 않는다."""
        _factor, unit = scale_for([{"fcf_ttm": -9.7e12}, {"fcf_ttm": 1e9}])
        self.assertEqual(unit, "억")


class TestStableId(unittest.TestCase):
    def payload(self, **overrides):
        """저장되는 산출물과 같은 모양. 키 이름이 다르면 이 테스트는 아무것도
        증명하지 못한다 — 제외 목록이 실제 필드명과 맞는지가 요점이다."""
        base = {
            "schema_version": "normalized-earnings/1.0.0",
            "instrument_id": "000660",
            "fs": "CFS",
            "created_at": "2026-08-01T00:00:00+09:00",
            "ttm_series": [{"period_end": "2025-12-31", "fcf_ttm": 1.0}],
            "normalization": {"current_fcf_ttm": 1.0},
        }
        base.update(overrides)
        return base

    def test_id_ignores_creation_time_but_tracks_the_numbers(self):
        """수집 시각이 바뀌었다고 ID 가 바뀌면 같은 계산이 매번 새것이 된다."""
        first = normalized_id_for(self.payload())
        same = normalized_id_for(
            self.payload(created_at="2026-08-08T00:00:00+09:00")
        )
        self.assertEqual(first, same)
        self.assertTrue(first.startswith("NORM-"))

    def test_id_is_not_affected_by_a_previously_written_id(self):
        """산출물을 다시 지문화할 때 이전 ID 가 입력에 섞이면 안 된다."""
        self.assertEqual(
            normalized_id_for(self.payload()),
            normalized_id_for(self.payload(normalized_id="NORM-" + "0" * 20)),
        )

    def test_changing_a_number_changes_the_id(self):
        self.assertNotEqual(
            normalized_id_for(self.payload()),
            normalized_id_for(
                self.payload(normalization={"current_fcf_ttm": 2.0})
            ),
        )


if __name__ == "__main__":
    unittest.main()
