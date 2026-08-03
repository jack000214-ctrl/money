"""차입 0 과 주당이익 기준을 결측과 구분하는 계약 테스트.

이 계층이 없을 때의 구멍: 수집기는 차입 계정이 안 잡히면 무조건 멈췄다.
"무차입이라고 단정하지 않는다"는 옳은 본능이지만, 그 반대편 — 정말로 무차입인
회사 — 이 통째로 막힌다는 사실은 보이지 않았다. 삼성E&A 2026 1Q 는 부채
5.7조가 전부 매입채무·초과청구공사이고 차입은 0원이라, 이 회사는 영원히
fundamentals 스냅샷을 만들 수 없었다.

주당이익도 같은 모양이다. 희석증권이 없는 회사는 희석주당이익을 공시하지
않는다. 기본으로 내려오는 것 자체는 옳지만 **어느 쪽을 썼는지 남기지 않으면**
전환사채가 있는 회사에서 주식수가 조용히 적게 잡힌다.

그래서 여기서 지키는 성질은 하나다 — 0 과 결측을 섞지 않고, 내려온 사실을
값 옆에 남긴다.
"""

import unittest

from helpers import ROOT  # noqa: F401
from collectors.dart import resolve_debt, resolve_shares


def bs(account_id, name, value):
    return {"sj_div": "BS", "account_id": account_id, "account_nm": name,
            "thstrm_amount": str(int(value))}


def is_(account_id, name, value):
    return {"sj_div": "IS", "account_id": account_id, "account_nm": name,
            "thstrm_amount": str(int(value))}


# 삼성E&A 2026-03-31 연결 재무상태표의 부채 쪽 (억원 아님, 원 단위 원문)
NO_DEBT_BALANCE_SHEET = [
    bs("ifrs-full_Liabilities", "부채총계", 5_711_286_589_852),
    bs("ifrs-full_CurrentLiabilities", "유동부채", 5_374_479_061_622),
    bs("ifrs-full_NoncurrentLiabilities", "비유동부채", 336_807_528_230),
    bs("ifrs-full_TradeAndOtherCurrentPayablesToTradeSuppliers", "매입채무",
       1_464_927_078_578),
    bs("dart_ShortTermDueToCustomersForContractWork", "초과청구공사",
       1_914_877_111_507),
]


class TestDebtBasis(unittest.TestCase):
    def test_matched_line_items_are_summed(self):
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_ShorttermBorrowings", "단기차입금", 300_000_000_000),
            bs("ifrs-full_Bonds", "사채", 700_000_000_000),
        ]
        debt, detail, basis = resolve_debt(rows)
        self.assertEqual(basis, "MATCHED_LINE_ITEMS")
        self.assertEqual(debt, 1_000_000_000_000)
        self.assertEqual([d["label"] for d in detail], ["단기차입금", "사채"])

    def test_readable_sheet_without_debt_lines_is_zero_not_failure(self):
        """무차입은 결측이 아니라 관측이다."""
        debt, detail, basis = resolve_debt(NO_DEBT_BALANCE_SHEET)
        self.assertEqual(basis, "NO_DEBT_LINE_ITEMS")
        self.assertEqual(debt, 0.0)
        self.assertEqual(detail, [])

    def test_unreadable_sheet_still_fails(self):
        """부채 구분조차 못 읽었으면 무차입이라고 단정하지 않는다."""
        rows = [bs("ifrs-full_Assets", "자산총계", 10_225_127_569_560)]
        with self.assertRaises(SystemExit):
            resolve_debt(rows)

    def test_partial_anchors_fail(self):
        """부채총계만 있고 유동·비유동 구분이 없으면 읽은 것으로 치지 않는다."""
        rows = [bs("ifrs-full_Liabilities", "부채총계", 5_711_286_589_852)]
        with self.assertRaises(SystemExit):
            resolve_debt(rows)


PARENT_NI = is_("ifrs-full_ProfitLossAttributableToOwnersOfParent",
                "지배기업 소유주지분", 155_598_170_026)


class TestEpsBasis(unittest.TestCase):
    def test_diluted_is_preferred(self):
        rows = [PARENT_NI,
                is_("ifrs-full_DilutedEarningsLossPerShare", "희석주당이익(손실)", 700),
                is_("ifrs-full_BasicEarningsLossPerShare", "기본주당순이익", 794)]
        shares, basis = resolve_shares(rows)
        self.assertEqual(basis, "DILUTED")
        self.assertAlmostEqual(shares, 155_598_170_026 / 700)

    def test_basic_fallback_is_labelled(self):
        """희석 공시가 없으면 기본으로 내려오되 그 사실을 값과 함께 남긴다."""
        rows = [PARENT_NI,
                is_("ifrs-full_BasicEarningsLossPerShare", "기본주당순이익", 794)]
        shares, basis = resolve_shares(rows)
        self.assertEqual(basis, "BASIC_NO_DILUTED_DISCLOSED")
        # 삼성E&A 발행주식 196.0M 과 맞는다
        self.assertAlmostEqual(shares / 1e6, 196.0, places=1)

    def test_no_eps_at_all_fails(self):
        with self.assertRaises(SystemExit):
            resolve_shares([PARENT_NI])

    def test_no_parent_income_fails(self):
        rows = [is_("ifrs-full_BasicEarningsLossPerShare", "기본주당순이익", 794)]
        with self.assertRaises(SystemExit):
            resolve_shares(rows)


if __name__ == "__main__":
    unittest.main()
