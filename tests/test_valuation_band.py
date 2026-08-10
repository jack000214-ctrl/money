"""밸류에이션 밴드 회귀 테스트.

발생한 버그: TTM 을 만들 때 **연속 4분기인지 확인하지 않았다.**
파마리서치 2025 사업보고서가 fnlttSinglAcntAll 에서 조회되지 않아 4Q25 EPS 가 비었는데,
코드가 그 구멍을 건너뛰고 1Q25+2Q25+3Q25+1Q26 을 더해 'TTM' 이라고 불렀다.
그럴듯한 숫자(17,004원)가 나와서 PER 21.00 과 백분위 17.8% 라는 결론까지 냈다.
올바른 값은 TTM 14,539원 / PER 24.55 / 백분위 36.0% 였다.
"""

import contextlib
import io
import unittest
from unittest.mock import patch

from helpers import ROOT  # noqa: F401  (sys.path 설정)
from calculators.valuation_band import (
    is_eps_account, kr_filing_dates, per_history, q_index, ttm_series,
)


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
        """공시일이 없으면 시점 보존이 불가능하므로 제외한다.

        그리고 **말없이 버리지 않는다.** 접수일 조회 연도가 EPS 조회 연도와
        어긋나면 이 갈래가 통째로 켜지는데, 그때 밴드는 실패하지 않고 그냥
        짧아져 나온다. 이유가 안 뜨면 요청한 기간을 못 받았다는 사실 자체가
        화면에서 사라진다.
        """
        quarterly = {
            "2025-03": q(3482, "2025-05-15"),
            "2025-06": q(4563, "2025-08-14"),
            "2025-09": q(4714, "2025-11-14"),
            "2025-12": q(2174, None),
        }
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            self.assertEqual(ttm_series(quarterly), [])
        message = stderr.getvalue()
        self.assertIn("2025-12", message)
        self.assertIn("접수일", message)

    def test_each_skip_says_which_reason_it_was(self):
        """'연속 4분기가 아니다' 와 '접수일이 없다' 는 원인도 대처도 다르다."""
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            ttm_series({
                "2025-03": q(1, "2025-05-15"),
                "2025-06": q(None, "2025-08-14"),
                "2025-09": q(1, "2025-11-14"),
                "2025-12": q(1, "2026-03-19"),
            })
        self.assertIn("분기 EPS", stderr.getvalue())
        self.assertNotIn("접수일", stderr.getvalue())

    def test_known_from_is_latest_filing_in_the_window(self):
        """이전 분기 정정이 늦게 나오면 정정 공개 전에는 새 TTM을 쓰지 않는다."""
        quarterly = {
            "2025-03": q(100, "2025-05-01"),
            "2025-06": q(110, "2026-06-01"),
            "2025-09": q(120, "2025-11-01"),
            "2025-12": q(130, "2026-03-01"),
        }
        out = ttm_series(quarterly)
        self.assertEqual(out[0]["known_from"], "2026-06-01")


class TestFilingDateYears(unittest.TestCase):
    """접수일은 EPS 와 **같은 연도**를 훑어야 한다.

    조회 연도가 2023 부터로 박혀 있었다. `--years` 로 그 이전을 요청하면 EPS 는
    받아 오는데 접수일이 없어 `ttm_series` 가 그 구간을 전부 버렸고, 명령은
    실패하지 않고 짧아진 밴드를 냈다.
    """

    def scanned(self, years):
        seen = []

        def fake_request(endpoint, params, key):
            seen.append(int(params["bgn_de"][:4]))
            return {"list": []}

        with patch("calculators.valuation_band.dart_request", fake_request):
            kr_filing_dates("00123456", "KEY", years)
        return seen

    def test_requested_years_are_all_scanned(self):
        self.assertLessEqual({2019, 2020}, set(self.scanned([2019, 2020])))

    def test_the_year_after_is_scanned_for_the_annual_report(self):
        """4분기를 담는 사업보고서는 이듬해 3월에 접수된다."""
        self.assertIn(2021, self.scanned([2019, 2020]))

    def test_future_years_are_not_requested(self):
        self.assertEqual(
            [year for year in self.scanned([2019, 2020]) if year > 2026], []
        )


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


class TestEpsAccountName(unittest.TestCase):
    """계정명 한 글자에 반년이 걸려 있다.

    삼성E&A 는 1·2분기를 '기본주당순이익', 3분기를 '기본주당순손익'으로 냈다.
    '손익' 표기를 놓치면 3분기가 비고, 3분기가 비면 (연간 − 1~3분기) 로 만드는
    4분기 역산까지 막혀 반년이 사라진다. 그러면 밴드는 실패하지 않고 **1년 묵은
    EPS 로 현재 PER 을 계산해** 조용히 비싸 보이게 만든다.
    """

    def test_accepts_label_variants_of_basic_eps(self):
        for name in ("기본주당이익", "기본주당순이익", "기본주당순손익",
                     "기본주당손익", "기본주당이익(손실)", "기본주당순이익(손실)"):
            self.assertTrue(is_eps_account(name), name)

    def test_ignores_spacing(self):
        self.assertTrue(is_eps_account(" 기본 주당 순손익 "))

    def test_rejects_diluted_and_missing(self):
        for name in ("희석주당이익", "희석주당순손익", "", None):
            self.assertFalse(is_eps_account(name), name)


if __name__ == "__main__":
    unittest.main()
