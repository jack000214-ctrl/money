"""재고 시계열의 파생값 계약.

DIO·QoQ·YoY 는 판단의 **무효화 신호 문턱**으로 인용된다 — "DIO 가 145일을
넘으면 절반 축소" 같은 약속이 이 숫자 위에 걸린다. 문턱이 걸린 값이 조용히
틀리면 신호가 켜져야 할 날 켜지지 않는다. 그런데 테스트가 한 줄도 없었다.

여기서 지키는 성질은 셋이다.
  1. **DIO 는 분기 매출원가 기준**이다 — 연환산 원가를 쓰면 4배로 어긋난다
  2. **1분기의 직전 분기는 전년 4분기**다 — 해 경계에서 QoQ 가 비면 안 된다
  3. **없는 값은 0 이 아니라 없음**이다 — 매출원가가 없으면 DIO 만 비운다
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors.kr_inventory import DAYS_PER_QUARTER, derive_rows


def stock(pairs):
    """{(fy, quarter): 잔액} 을 수집기 형태로 접는다."""
    return {key: (float(value), "report") for key, value in pairs.items()}


def cost(pairs):
    return {key: {"val": float(value), "derived": False}
            for key, value in pairs.items()}


class TestDio(unittest.TestCase):
    def test_dio_is_days_of_one_quarter_of_cost(self):
        rows = derive_rows(
            stock({(2026, 1): 1000.0}),
            cost({(2026, 1): 1000.0}),
            [2026],
        )
        # 재고 = 분기 원가 → 딱 한 분기치 = 91.25일
        self.assertEqual(rows[0]["dio_days"], round(DAYS_PER_QUARTER, 1))

    def test_dio_scales_linearly_with_the_balance(self):
        rows = derive_rows(
            stock({(2026, 1): 2000.0}),
            cost({(2026, 1): 1000.0}),
            [2026],
        )
        self.assertEqual(rows[0]["dio_days"], round(DAYS_PER_QUARTER * 2, 1))

    def test_missing_cost_empties_dio_but_keeps_the_balance(self):
        """매출원가를 못 찾았다고 재고 잔액까지 버리면 사이클을 못 본다."""
        rows = derive_rows(stock({(2026, 1): 1500.0}), {}, [2026])
        self.assertEqual(rows[0]["inventories"], 1500.0)
        self.assertIsNone(rows[0]["dio_days"])
        self.assertIsNone(rows[0]["cogs_quarter"])

    def test_zero_or_negative_cost_does_not_divide(self):
        for bad in (0.0, -100.0):
            with self.subTest(cogs=bad):
                rows = derive_rows(
                    stock({(2026, 1): 1000.0}),
                    cost({(2026, 1): bad}),
                    [2026],
                )
                self.assertIsNone(rows[0]["dio_days"])

    def test_derived_fourth_quarter_cost_is_flagged(self):
        """4분기 원가는 연간 − 3분기누계 역산이다. 실측과 섞으면 안 된다."""
        rows = derive_rows(
            stock({(2026, 4): 1000.0}),
            {(2026, 4): {"val": 1000.0, "derived": True}},
            [2026],
        )
        self.assertTrue(rows[0]["cogs_derived"])


class TestQuarterOverQuarter(unittest.TestCase):
    def test_first_quarter_compares_against_last_years_fourth(self):
        """같은 해 안에서만 찾으면 매년 1분기 QoQ 가 빈다."""
        rows = derive_rows(
            stock({(2025, 4): 1000.0, (2026, 1): 1200.0}),
            {},
            [2025, 2026],
        )
        first_quarter = next(r for r in rows if r["period_end"] == "2026-03-31")
        self.assertEqual(first_quarter["qoq_pct"], 20.0)

    def test_within_year_quarters_chain_normally(self):
        rows = derive_rows(
            stock({(2026, 1): 1000.0, (2026, 2): 900.0}),
            {},
            [2026],
        )
        self.assertIsNone(rows[0]["qoq_pct"])  # 직전 분기가 없다
        self.assertEqual(rows[1]["qoq_pct"], -10.0)

    def test_year_over_year_uses_the_same_quarter(self):
        rows = derive_rows(
            stock({(2025, 2): 800.0, (2026, 2): 1000.0}),
            {},
            [2025, 2026],
        )
        latest = next(r for r in rows if r["fy"] == 2026)
        self.assertEqual(latest["yoy_pct"], 25.0)

    def test_zero_base_does_not_produce_a_percentage(self):
        """기저가 0이면 변화율은 성장이 아니라 나눗셈 오류다."""
        rows = derive_rows(
            stock({(2026, 1): 0.0, (2026, 2): 500.0}),
            {},
            [2026],
        )
        self.assertIsNone(rows[1]["qoq_pct"])


class TestWindowAndProvenance(unittest.TestCase):
    def test_years_outside_the_request_are_used_but_not_listed(self):
        """비교표시로 채운 전년 4분기는 QoQ 의 기저로만 쓰고 표에는 안 넣는다."""
        rows = derive_rows(
            stock({(2025, 4): 1000.0, (2026, 1): 1100.0}),
            {},
            [2026],
        )
        self.assertEqual([r["fy"] for r in rows], [2026])
        self.assertEqual(rows[0]["qoq_pct"], 10.0)

    def test_comparative_source_is_preserved(self):
        """같은 공시의 다른 칸에서 온 값인지가 표에 남아야 한다."""
        rows = derive_rows(
            {(2026, 1): (1000.0, "comparative")}, {}, [2026]
        )
        self.assertEqual(rows[0]["inventories_source"], "comparative")

    def test_rows_come_out_in_chronological_order(self):
        rows = derive_rows(
            stock({(2026, 3): 3.0, (2026, 1): 1.0, (2026, 2): 2.0}),
            {},
            [2026],
        )
        self.assertEqual(
            [r["period_end"] for r in rows],
            ["2026-03-31", "2026-06-30", "2026-09-30"],
        )


if __name__ == "__main__":
    unittest.main()
