"""밸류에이션 밴드 회귀 테스트.

발생한 버그: TTM 을 만들 때 **연속 4분기인지 확인하지 않았다.**
파마리서치 2025 사업보고서가 fnlttSinglAcntAll 에서 조회되지 않아 4Q25 EPS 가 비었는데,
코드가 그 구멍을 건너뛰고 1Q25+2Q25+3Q25+1Q26 을 더해 'TTM' 이라고 불렀다.
그럴듯한 숫자(17,004원)가 나와서 PER 21.00 과 백분위 17.8% 라는 결론까지 냈다.
올바른 값은 TTM 14,539원 / PER 24.55 / 백분위 36.0% 였다.
"""

import unittest

from helpers import ROOT  # noqa: F401  (sys.path 설정)
from valuation_band import per_history, q_index, ttm_series


def q(eps, filed):
    return {"eps": eps, "filed": filed}


class TestQuarterIndex(unittest.TestCase):
    def test_consecutive_quarters_differ_by_one(self):
        self.assertEqual(q_index("2026-06") - q_index("2026-03"), 1)
        self.assertEqual(q_index("2026-03") - q_index("2025-12"), 1)

    def test_year_boundary(self):
        self.assertEqual(q_index("2026-03") - q_index("2025-03"), 4)


class TestTTMSeries(unittest.TestCase):
    def test_full_four_quarters_sums(self):
        quarterly = {
            "2025-03": q(3482, "2025-05-15"),
            "2025-06": q(4563, "2025-08-14"),
            "2025-09": q(4714, "2025-11-14"),
            "2025-12": q(2174, "2026-03-19"),
        }
        out = ttm_series(quarterly)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["eps_ttm"], 3482 + 4563 + 4714 + 2174)
        self.assertEqual(out[0]["as_of"], "2025-12")

    def test_gap_is_skipped_not_bridged(self):
        """4Q25 가 비면 그 구간을 버린다. 건너뛰고 더하면 안 된다."""
        quarterly = {
            "2025-03": q(3482, "2025-05-15"),
            "2025-06": q(4563, "2025-08-14"),
            "2025-09": q(4714, "2025-11-14"),
            # 2025-12 없음 (사업보고서 조회 실패)
            "2026-03": q(4245, "2026-05-15"),
        }
        out = ttm_series(quarterly)
        # 버그 있을 때: 3482+4563+4714+4245 = 17,004 이 나왔다
        totals = [o["eps_ttm"] for o in out]
        self.assertNotIn(17004, totals, "비연속 4분기를 TTM 으로 합산하면 안 된다")
        self.assertEqual(out, [], "연속 4분기가 없으므로 결과가 없어야 한다")

    def test_none_eps_excluded(self):
        quarterly = {
            "2025-03": q(3482, "2025-05-15"),
            "2025-06": q(None, "2025-08-14"),
            "2025-09": q(4714, "2025-11-14"),
            "2025-12": q(2174, "2026-03-19"),
        }
        self.assertEqual(ttm_series(quarterly), [])

    def test_missing_filing_date_excluded(self):
        """공시일이 없으면 시점 보존이 불가능하므로 제외한다."""
        quarterly = {
            "2025-03": q(3482, "2025-05-15"),
            "2025-06": q(4563, "2025-08-14"),
            "2025-09": q(4714, "2025-11-14"),
            "2025-12": q(2174, None),
        }
        self.assertEqual(ttm_series(quarterly), [])


class TestPerHistoryPointInTime(unittest.TestCase):
    def test_uses_only_already_published_eps(self):
        """미래정보 배제 - 공시 전 가격에는 그 EPS 를 쓰면 안 된다 (ADR-003)."""
        prices = [("2025-11-13", 300000.0), ("2025-11-14", 300000.0)]
        ttm = [
            {"as_of": "2025-06", "eps_ttm": 12238, "known_from": "2025-08-14"},
            {"as_of": "2025-09", "eps_ttm": 14539, "known_from": "2025-11-14"},
        ]
        out = per_history(prices, ttm)
        by_date = {d: (per, eps) for d, _, per, eps, _ in out}
        self.assertEqual(by_date["2025-11-13"][1], 12238, "공시 전에는 이전 EPS 를 쓴다")
        self.assertEqual(by_date["2025-11-14"][1], 14539, "공시일부터 새 EPS 를 쓴다")

    def test_no_eps_published_yet_is_dropped(self):
        prices = [("2024-01-02", 300000.0)]
        ttm = [{"as_of": "2025-09", "eps_ttm": 14539, "known_from": "2025-11-14"}]
        self.assertEqual(per_history(prices, ttm), [])

    def test_negative_eps_excluded(self):
        """적자면 PER 이 의미를 갖지 않는다."""
        prices = [("2026-01-02", 300000.0)]
        ttm = [{"as_of": "2025-09", "eps_ttm": -500, "known_from": "2025-11-14"}]
        self.assertEqual(per_history(prices, ttm), [])


if __name__ == "__main__":
    unittest.main()
