"""미국 총차입 산출 계약 테스트.

이 계층이 없을 때의 구멍: 수집기는 `LongTermDebt` 계열 태그 **하나**만 골라
그것을 총차입이라고 불렀다. 그래서 유동성차입·리스부채·전환사채가 통째로
빠졌다. 실측(2026-08-03) —

  TSLA  기록 7,721M  실제 16,080M  (유동차입 1,340 + 운용리스 6,738 + 금융리스 281)
  IBM   기록 56,212M 실제 65,272M  (유동차입 5,775 + 운용리스 3,285)
  RXRX  기록 723K    실제 72.4M    (기록값은 2021년치였고 리스가 전부 빠졌다)

us-gaap 은 같은 돈을 총액 태그와 구성요소 태그로 **동시에** 낸다. 그래서 여기서
지키는 성질은 두 가지다 — 겹치지 않는 자리마다 하나씩만 고르고, 고른 값이
언제 것인지 남긴다.
"""

import unittest

from helpers import ROOT  # noqa: F401
from collectors.edgar import resolve_optional_value, resolve_us_debt

PERIOD = "2026-06-30"


class TestSlotSelection(unittest.TestCase):
    def test_components_are_summed_not_one_tag(self):
        """자리마다 하나씩 골라 더한다. 한 태그만 쓰면 나머지가 사라진다."""
        points = {
            "LongTermDebt": (PERIOD, 7_721_000_000),
            "DebtCurrent": (PERIOD, 1_340_000_000),
            "OperatingLeaseLiabilityCurrent": (PERIOD, 1_022_000_000),
            "OperatingLeaseLiabilityNoncurrent": (PERIOD, 5_716_000_000),
            "FinanceLeaseLiabilityCurrent": (PERIOD, 78_000_000),
            "FinanceLeaseLiabilityNoncurrent": (PERIOD, 203_000_000),
        }
        debt, detail, by_category, notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(debt, 16_080_000_000)
        self.assertEqual(by_category["BORROWING"], 9_061_000_000)
        self.assertEqual(by_category["LEASE"], 7_019_000_000)
        self.assertEqual(len(detail), 6)
        self.assertEqual(notes, [])

    def test_freshest_tag_wins_inside_a_slot(self):
        """IBM 은 LongTermDebt 가 2019년에서 멈췄고 Noncurrent 가 현행이다."""
        points = {
            "LongTermDebt": ("2019-09-30", 62_944_000_000),
            "LongTermDebtNoncurrent": (PERIOD, 56_212_000_000),
        }
        debt, detail, _by, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(debt, 56_212_000_000)
        self.assertEqual(detail[0]["tag"], "LongTermDebtNoncurrent")

    def test_unambiguous_tag_wins_on_a_tie(self):
        """같은 날짜면 뜻이 분명한 쪽을 쓴다.

        LongTermDebt 는 회사에 따라 유동성을 포함해 총액일 수 있다.
        """
        points = {
            "LongTermDebt": (PERIOD, 9_000_000_000),
            "LongTermDebtNoncurrent": (PERIOD, 7_721_000_000),
        }
        debt, detail, _by, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(detail[0]["tag"], "LongTermDebtNoncurrent")
        self.assertEqual(debt, 7_721_000_000)

    def test_one_slot_never_counts_two_tags(self):
        """한 자리에서 두 태그를 더하면 같은 돈을 두 번 센다."""
        points = {
            "ShortTermBorrowings": (PERIOD, 5_775_000_000),
            "DebtCurrent": (PERIOD, 5_775_000_000),
            "LongTermDebtCurrent": (PERIOD, 5_775_000_000),
        }
        debt, detail, _by, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(debt, 5_775_000_000)
        self.assertEqual(len(detail), 1)


class TestFinanceLeaseOverlap(unittest.TestCase):
    """IBM 의 장기차입금은 금융리스를 이미 품고 있다.

    LongTermDebtAndCapitalLeaseObligations 가 같은 날짜에 같은 값으로 잡히면
    그 차입 값에 금융리스가 포함돼 있다는 뜻이다. 거기에 금융리스를 또 더하면
    이중계상이다.
    """

    def test_finance_lease_excluded_when_borrowing_already_includes_it(self):
        points = {
            "LongTermDebtNoncurrent": (PERIOD, 56_212_000_000),
            "LongTermDebtAndCapitalLeaseObligations": (PERIOD, 56_212_000_000),
            "FinanceLeaseLiabilityNoncurrent": (PERIOD, 874_000_000),
        }
        debt, detail, by_category, notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(debt, 56_212_000_000)
        self.assertNotIn("LEASE", by_category)
        self.assertTrue(any("금융리스" in n for n in notes))
        self.assertEqual([d["tag"] for d in detail], ["LongTermDebtNoncurrent"])

    def test_finance_lease_kept_when_no_combined_tag(self):
        """테슬라는 결합 태그가 2018년에서 멈췄다 — 리스는 별개다."""
        points = {
            "LongTermDebt": (PERIOD, 7_721_000_000),
            "LongTermDebtAndCapitalLeaseObligations": ("2018-12-31", 9_403_672_000),
            "FinanceLeaseLiabilityNoncurrent": (PERIOD, 203_000_000),
        }
        debt, _detail, by_category, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(debt, 7_924_000_000)
        self.assertEqual(by_category["LEASE"], 203_000_000)

    def test_operating_lease_is_never_dropped_by_the_overlap_rule(self):
        """결합 태그는 금융리스만 품는다. 운용리스는 언제나 별개다."""
        points = {
            "LongTermDebtNoncurrent": (PERIOD, 56_212_000_000),
            "LongTermDebtAndCapitalLeaseObligations": (PERIOD, 56_212_000_000),
            "FinanceLeaseLiabilityNoncurrent": (PERIOD, 874_000_000),
            "OperatingLeaseLiabilityNoncurrent": (PERIOD, 2_515_000_000),
        }
        debt, _detail, by_category, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(by_category["LEASE"], 2_515_000_000)
        self.assertEqual(debt, 56_212_000_000 + 2_515_000_000)


class TestStaleness(unittest.TestCase):
    def test_years_old_value_is_excluded_not_used_as_current(self):
        """RXRX 의 LongTermDebt 는 2021년치다. 2026년 순부채에 쓰면 안 된다."""
        points = {
            "LongTermDebt": ("2021-12-31", 723_000),
            "OperatingLeaseLiabilityCurrent": ("2026-03-31", 13_087_000),
        }
        debt, detail, _by, notes = resolve_us_debt(points, "2026-03-31")
        self.assertEqual(debt, 13_087_000)
        self.assertTrue(any("2021-12-31" in n for n in notes))
        self.assertNotIn("LongTermDebt", [d["tag"] for d in detail])

    def test_last_annual_disclosure_is_still_used(self):
        """분기마다 안 내는 항목도 있다. 1년 안쪽이면 최신 공시로 본다."""
        points = {
            "LongTermDebtNoncurrent": (PERIOD, 56_212_000_000),
            "FinanceLeaseLiabilityNoncurrent": ("2025-12-31", 874_000_000),
        }
        debt, _detail, by_category, _notes = resolve_us_debt(points, PERIOD)
        self.assertEqual(by_category["LEASE"], 874_000_000)
        self.assertEqual(debt, 56_212_000_000 + 874_000_000)

    def test_every_component_records_its_own_date(self):
        points = {
            "LongTermDebtNoncurrent": (PERIOD, 56_212_000_000),
            "FinanceLeaseLiabilityNoncurrent": ("2025-12-31", 874_000_000),
        }
        _debt, detail, _by, _notes = resolve_us_debt(points, PERIOD)
        dates = {d["tag"]: d["as_of"] for d in detail}
        self.assertEqual(dates["FinanceLeaseLiabilityNoncurrent"], "2025-12-31")


class TestOptionalValueStaleness(unittest.TestCase):
    """묵은 값을 현재값으로 쓰지 않는다.

    실측: IBM 의 short_term_investments 600M 은 2021-03-31 자다. 1,917일 묵은
    값이 2026년 순부채의 현금 쪽에 그대로 실려 있었다. 차입에서 고친 것과
    같은 결함이 자산 쪽에도 있었다.

    값은 0 으로 내리되 `OBSERVED`(정말 0) 와 `STALE_EXCLUDED`(묵어서 뺐다) 를
    섞지 않는다 — 이 저장소가 차입에서 이미 정한 규칙이다.
    """

    def test_stale_value_is_excluded_and_labelled(self):
        value, basis, note = resolve_optional_value(
            600_000_000, "2021-03-31", "2026-06-30", label="단기투자자산"
        )
        self.assertEqual(value, 0.0)
        self.assertEqual(basis, "STALE_EXCLUDED")
        self.assertIn("2021-03-31", note)

    def test_fresh_value_is_kept(self):
        value, basis, note = resolve_optional_value(
            28_305_000_000, "2026-06-30", "2026-06-30", label="단기투자자산"
        )
        self.assertEqual(value, 28_305_000_000)
        self.assertEqual(basis, "OBSERVED")
        self.assertIsNone(note)

    def test_observed_zero_is_not_a_gap(self):
        """정말 0 인 것과 묵어서 뺀 것은 다른 관측이다."""
        value, basis, _note = resolve_optional_value(
            0, "2026-06-30", "2026-06-30", label="단기투자자산"
        )
        self.assertEqual(value, 0.0)
        self.assertEqual(basis, "OBSERVED")

    def test_missing_tag_is_not_disclosed(self):
        value, basis, note = resolve_optional_value(
            None, None, "2026-06-30", label="단기투자자산"
        )
        self.assertEqual(value, 0.0)
        self.assertEqual(basis, "NOT_DISCLOSED")
        self.assertIsNotNone(note)

    def test_last_annual_disclosure_is_still_fresh_enough(self):
        value, basis, _note = resolve_optional_value(
            600_000_000, "2025-12-31", "2026-06-30", label="단기투자자산"
        )
        self.assertEqual(value, 600_000_000)
        self.assertEqual(basis, "OBSERVED")


class TestEmpty(unittest.TestCase):
    def test_no_debt_tags_at_all_is_zero_with_a_note(self):
        debt, detail, by_category, notes = resolve_us_debt({}, PERIOD)
        self.assertEqual(debt, 0.0)
        self.assertEqual(detail, [])
        self.assertEqual(by_category, {})
        self.assertEqual(notes, [])


if __name__ == "__main__":
    unittest.main()
