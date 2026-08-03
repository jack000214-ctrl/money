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
from collectors.dart import debt_by_category, resolve_debt, resolve_shares


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


class TestDebtCoverage(unittest.TestCase):
    """총차입에서 통째로 빠지던 계정들.

    실측(파마리서치 214450, 2026-03-31): 인식 4.3억인데 유동성전환사채 57.7억,
    리스부채 30억, 유동성장기차입금 0.5억이 빠져 순부채가 20배 틀렸다. 원인은
    두 가지다 — 전환사채·리스부채가 애초에 목록에 없었고, DART 표준코드
    접두사가 회사마다 갈리는데(ifrs-full_ / ifrs_ / dart_ / 미사용) 코드가
    어긋나면 계정명 폴백도 이름이 달라 못 잡았다.
    """

    def test_convertible_bonds_are_debt(self):
        """전환사채는 상환의무가 있으므로 차입이다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("dart_CurrentPortionOfConvertibleBonds", "유동성전환사채",
               5_770_240_067),
        ]
        debt, detail, basis = resolve_debt(rows)
        self.assertEqual(basis, "MATCHED_LINE_ITEMS")
        self.assertEqual(debt, 5_770_240_067)
        self.assertEqual(detail[0]["category"], "CONVERTIBLE")

    def test_bond_with_warrant_is_debt(self):
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("dart_BondWithWarrant", "신주인수권부사채", 10_271_549_627),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 10_271_549_627)
        self.assertEqual(detail[0]["category"], "CONVERTIBLE")

    def test_lease_liabilities_are_debt(self):
        """IFRS16 이후 리스부채는 차입이다.

        영업현금흐름이 이미 IFRS16 기준(리스료가 재무활동으로 빠진 뒤)이므로,
        부채에서만 리스를 빼면 리스 의존도가 높은 회사가 조용히 싸 보인다.
        """
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_CurrentLeaseLiabilities", "유동리스부채", 1_339_435_239),
            bs("ifrs-full_NoncurrentLeaseLiabilities", "비유동리스부채",
               1_652_137_575),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 1_339_435_239 + 1_652_137_575)
        self.assertEqual({d["category"] for d in detail}, {"LEASE"})

    def test_current_portion_of_bonds_is_debt(self):
        """'유동성사채'. 표준코드가 dart_CurrentPortionOfBonds 라 사채와 다르다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("dart_CurrentPortionOfBonds", "유동성사채", 30_000_000_000),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 30_000_000_000)
        self.assertEqual(detail[0]["category"], "BOND")

    def test_ifrs_prefix_variant_is_matched(self):
        """접두사가 'ifrs_' 인 변형도 같은 계정이다 (실제 데이터에 6건 있다)."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs_ShorttermBorrowings", "단기차입금", 250_000_000),
            bs("ifrs_CurrentPortionOfLongtermBorrowings", "유동성장기차입금",
               34_129_992),
        ]
        debt, _, _ = resolve_debt(rows)
        self.assertEqual(debt, 250_000_000 + 34_129_992)

    def test_unstandardized_code_falls_back_to_account_name(self):
        """표준계정코드를 안 쓰면 이름으로 잡아야 한다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("-표준계정코드 미사용-", "유동성장기차입금", 50_749_992),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 50_749_992)
        self.assertEqual(detail[0]["category"], "BORROWING")

    def test_combined_line_is_counted_once(self):
        """'유동 차입금(사채 포함)' 은 합산행이다. 코드·이름 양쪽에 걸려도 한 번만 센다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_ShorttermBorrowings", "유동 차입금(사채 포함)",
               103_600_000_000),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 103_600_000_000)
        self.assertEqual(len(detail), 1)

    def test_asset_side_accounts_are_not_debt(self):
        """이름에 '리스'·'대여금'이 들어가도 자산은 차입이 아니다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_FinanceLeaseReceivables", "금융리스채권", 9_000_000_000),
            bs("dart_ShortTermLoans", "단기대여금", 4_000_000_000),
        ]
        debt, detail, basis = resolve_debt(rows)
        self.assertEqual(basis, "NO_DEBT_LINE_ITEMS")
        self.assertEqual(debt, 0.0)
        self.assertEqual(detail, [])

    def test_total_never_exceeds_total_liabilities(self):
        """차입 합이 부채총계를 넘으면 이중계상이다. 틀린 값을 쓰느니 멈춘다."""
        rows = [
            bs("ifrs-full_Liabilities", "부채총계", 1_000_000_000),
            bs("ifrs-full_CurrentLiabilities", "유동부채", 600_000_000),
            bs("ifrs-full_NoncurrentLiabilities", "비유동부채", 400_000_000),
            bs("ifrs-full_ShorttermBorrowings", "단기차입금", 900_000_000),
            bs("dart_ConvertibleBonds", "전환사채", 800_000_000),
        ]
        with self.assertRaises(SystemExit):
            resolve_debt(rows)

    def test_categories_sum_to_total(self):
        """분류별 합은 총차입과 같아야 한다 — 모델이 리스를 빼고 다시 셀 수 있게."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_ShorttermBorrowings", "단기차입금", 100_000_000),
            bs("dart_BondsIssued", "사채", 200_000_000),
            bs("dart_ConvertibleBonds", "전환사채", 300_000_000),
            bs("ifrs-full_CurrentLeaseLiabilities", "유동리스부채", 400_000_000),
        ]
        debt, detail, _ = resolve_debt(rows)
        by_category = debt_by_category(detail)
        self.assertEqual(sum(by_category.values()), debt)
        self.assertEqual(by_category["BORROWING"], 100_000_000)
        self.assertEqual(by_category["BOND"], 200_000_000)
        self.assertEqual(by_category["CONVERTIBLE"], 300_000_000)
        self.assertEqual(by_category["LEASE"], 400_000_000)

    def test_samsung_eanda_stays_debt_free(self):
        """회귀: 무차입 회사가 새 계정 때문에 차입이 생기면 안 된다."""
        debt, detail, basis = resolve_debt(NO_DEBT_BALANCE_SHEET)
        self.assertEqual(basis, "NO_DEBT_LINE_ITEMS")
        self.assertEqual(debt, 0.0)
        self.assertEqual(detail, [])


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
