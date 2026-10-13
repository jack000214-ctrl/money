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
from collectors.dart import (
    debt_by_category,
    resolve_debt,
    resolve_shares,
    resolve_short_investments,
    unclassified_debt_rows,
)


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

    # 무차입 회귀(삼성E&A)는 TestDebtBasis 의
    # test_readable_sheet_without_debt_lines_is_zero_not_failure 가 이미 지킨다.
    # 같은 본문을 한 번 더 두면 한쪽만 고쳐지는 날이 온다.


class TestDebtIsNotCompleteJustBecauseSomethingMatched(unittest.TestCase):
    """몇 줄을 찾았다는 것이 전부 찾았다는 뜻으로 바뀌면 안 된다.

    실측(SK하이닉스 000660, 2026-03-31): 차입금을 IFRS 표준코드
    `CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings`·`LongtermBorrowings`
    에 계정명 '차입금'으로만 낸다. 둘 다 목록에 없어서 19.3조가 통째로 빠졌고,
    리스부채 2.5조만 잡힌 채 `MATCHED_LINE_ITEMS` 로 통과했다. 같은 분류기를
    2026 반기 원문에 돌려도 18.6조가 빠졌다 — 재수집으로 고쳐지지 않는 결함이었다.
    """

    HYNIX = NO_DEBT_BALANCE_SHEET + [
        bs("ifrs-full_CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings",
           "차입금", 5_890_940_000_000),
        bs("ifrs-full_LongtermBorrowings", "차입금", 13_426_725_000_000),
        bs("ifrs-full_CurrentLeaseLiabilities", "리스부채", 526_303_000_000),
        bs("ifrs-full_NoncurrentLeaseLiabilities", "리스부채", 1_988_448_000_000),
    ]

    def setUp(self):
        # 원문의 부채총계(58.4조)로 맞춘다. 차입 합이 부채총계를 넘으면 멈춘다.
        self.rows = [bs("ifrs-full_Liabilities", "부채총계", 58_448_945_000_000),
                     *self.HYNIX[1:]]

    def test_standard_borrowing_concepts_are_debt(self):
        debt, detail, basis = resolve_debt(self.rows)
        self.assertEqual(debt, 21_832_416_000_000)
        self.assertEqual(basis, "MATCHED_LINE_ITEMS")
        self.assertEqual(debt_by_category(detail), {
            "BORROWING": 19_317_665_000_000, "LEASE": 2_514_751_000_000})

    def test_same_name_and_amount_on_two_lines_are_two_lines(self):
        """유동·비유동이 같은 이름·같은 금액이어도 행이 다르면 둘 다 센다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            {**bs("-표준계정코드 미사용-", "차입금", 100), "ord": "45"},
            {**bs("-표준계정코드 미사용-", "차입금", 100), "ord": "56"},
        ]
        self.assertEqual(resolve_debt(rows)[0], 200)

    def test_exchangeable_bonds_are_debt(self):
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("dart_CurrentPortionOfExchangeableBond", "유동성교환사채", 7_000),
            bs("dart_ExchangeableBonds", "장기교환사채", 3_000),
        ]
        debt, detail, _ = resolve_debt(rows)
        self.assertEqual(debt, 10_000)
        self.assertEqual({d["category"] for d in detail}, {"CONVERTIBLE"})

    def test_redeemable_preferred_liability_is_debt_in_its_own_bucket(self):
        """파마리서치(214450) 2026-06-30. 전환상환우선주부채 2,073억이 빠져 있었다.

        회사의 자본관리 주석도 차입금을 '전환상환우선주부채 + 전환사채 + 차입금'
        213,712,495,353원으로 적는다 — 아래 세 분류의 합과 원 단위까지 같다.
        """
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("dart_CurrentPortionOfConvertibleBonds", "유동성전환사채",
               5_976_167_604),
            bs("dart_CurrentPortionOfConvertibleRedeemablePreferredStockLiabilities",
               "유동성전환상환우선주부채", 708_269_092),
            bs("-표준계정코드 미사용-", "비유동전환상환우선주부채", 206_551_141_121),
            bs("dart_CurrentPortionOfNoncurrentLoansReceived",
               "장기 금융기관 차입금(사채 제외)의 유동성 대체 부분", 53_239_992),
            bs("ifrs-full_NoncurrentPortionOfNoncurrentLoansReceived", "장기차입금",
               423_677_544),
        ]
        debt, detail, basis = resolve_debt(rows)
        by_category = debt_by_category(detail)
        self.assertEqual(by_category["REDEEMABLE_PREFERRED"], 207_259_410_213)
        self.assertEqual(by_category["BORROWING"], 476_917_536)
        self.assertEqual(debt, 213_712_495_353)
        self.assertEqual(basis, "MATCHED_LINE_ITEMS")

    def test_unlisted_debt_like_row_is_surfaced_not_summed(self):
        """목록에 없는 차입성 계정은 더하지 않되, 있다는 사실을 값과 함께 남긴다."""
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_CurrentLeaseLiabilities", "리스부채", 10),
            bs("-표준계정코드 미사용-", "차입금및사채", 300),
        ]
        debt, _detail, basis = resolve_debt(rows)
        self.assertEqual(debt, 10)
        self.assertEqual(basis, "UNCLASSIFIED_CANDIDATES")
        self.assertEqual(unclassified_debt_rows(rows), [
            {"label": "차입금및사채", "account_id": "-표준계정코드 미사용-",
             "value": 300.0}])

    def test_only_unlisted_debt_is_not_an_observation_of_no_debt(self):
        rows = NO_DEBT_BALANCE_SHEET + [bs("ifrs-full_Borrowings", "총차입", 300)]
        debt, detail, basis = resolve_debt(rows)
        self.assertEqual((debt, detail), (0.0, []))
        self.assertEqual(basis, "UNCLASSIFIED_CANDIDATES")

    def test_assets_and_zero_balances_are_not_candidates(self):
        rows = NO_DEBT_BALANCE_SHEET + [
            bs("ifrs-full_FinanceLeaseReceivables", "금융리스채권", 9_000),
            bs("dart_ShortTermLoans", "단기대여금", 4_000),
            bs("-표준계정코드 미사용-", "회사채투자", 2_000),
            bs("-표준계정코드 미사용-", "기타차입부채", 0),
        ]
        self.assertEqual(unclassified_debt_rows(rows), [])
        self.assertEqual(resolve_debt(rows)[2], "NO_DEBT_LINE_ITEMS")


CURRENT_ASSETS = bs("ifrs-full_CurrentAssets", "유동자산", 10_000_000)


class TestShortTermInvestments(unittest.TestCase):
    """순부채의 현금 쪽에도 같은 구멍이 있었다.

    코드 셋만 보던 때 SK하이닉스의 단기투자자산 14.9조(`CurrentInvestments`)와
    파마리서치의 유동성금융자산 3,753억(`OtherCurrentFinancialAssets`)이 0 으로
    잡혔다. 차입 누락과 방향이 반대라 두 오차가 서로를 가렸고, 한쪽만 고치면
    순부채가 더 틀린다. 파마리서치 판단은 이 누락을 여섯 번 요청했다.
    """

    def test_current_investments_are_counted(self):
        total, detail, candidates = resolve_short_investments([
            CURRENT_ASSETS,
            bs("ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents",
               "단기금융상품", 1_822),
            bs("ifrs-full_CurrentInvestments", "단기투자자산", 1_494),
        ])
        self.assertEqual(total, 3_316)
        self.assertEqual([d["label"] for d in detail], ["단기금융상품", "단기투자자산"])
        self.assertEqual(candidates, [])

    def test_a_generic_code_counts_when_the_name_says_short_term_investment(self):
        total, _detail, candidates = resolve_short_investments([
            CURRENT_ASSETS,
            bs("ifrs-full_OtherCurrentFinancialAssets", "유동성금융자산", 3_753),
        ])
        self.assertEqual(total, 3_753)
        self.assertEqual(candidates, [])

    def test_measurement_category_variants_are_the_same_line(self):
        total, _detail, _ = resolve_short_investments([
            CURRENT_ASSETS,
            bs("-표준계정코드 미사용-", "유동성 상각후원가 측정 금융자산", 38),
            bs("ifrs-full_CurrentFinancialAssetsAtFairValueThroughProfitOrLoss"
               "MandatorilyMeasuredAtFairValue", "당기손익공정가치금융자산", 7),
        ])
        self.assertEqual(total, 45)

    def test_other_financial_assets_are_surfaced_not_summed(self):
        """'기타금융자산'에는 대여금·보증금이 섞인다. 세지 않고 후보로 남긴다."""
        total, detail, candidates = resolve_short_investments([
            CURRENT_ASSETS,
            bs("ifrs-full_OtherCurrentFinancialAssets", "기타금융자산", 318),
        ])
        self.assertEqual((total, detail), (0.0, []))
        self.assertEqual(candidates[0]["value"], 318.0)

    def test_derivatives_receivables_and_noncurrent_are_neither(self):
        total, _detail, candidates = resolve_short_investments([
            CURRENT_ASSETS,
            bs("ifrs-full_CurrentDerivativeFinancialAssets", "유동파생상품자산", 530),
            bs("ifrs-full_OtherCurrentFinancialAssets", "유동성기타채권", 5),
            bs("ifrs-full_OtherNoncurrentFinancialAssets", "금융자산", 500),
            bs("ifrs-full_NoncurrentFinancialAssetsAtAmortisedCost",
               "상각후원가 측정 금융자산", 6),
        ])
        self.assertEqual((total, candidates), (0.0, []))

    def test_a_total_and_its_parts_counted_together_stops(self):
        with self.assertRaises(SystemExit):
            resolve_short_investments([
                bs("ifrs-full_CurrentAssets", "유동자산", 100),
                bs("ifrs-full_OtherCurrentFinancialAssets", "유동성금융자산", 90),
                bs("ifrs-full_ShorttermDepositsNotClassifiedAsCashEquivalents",
                   "단기금융상품", 80),
            ])


PARENT_NI = is_("ifrs-full_ProfitLossAttributableToOwnersOfParent",
                "지배기업 소유주지분", 155_598_170_026)


class TestEpsBasis(unittest.TestCase):
    def test_consolidated_total_or_comprehensive_income_cannot_replace_parent_profit(self):
        rows = [is_("ifrs-full_ProfitLoss", "당기순이익", 1000),
                is_("ifrs-full_ProfitLossAttributableToNoncontrollingInterests", "비지배지분", 200),
                is_("ifrs-full_BasicEarningsLossPerShare", "기본주당순이익", 10)]
        with self.assertRaises(SystemExit):
            resolve_shares(rows)
        rows.append(is_("ifrs-full_ComprehensiveIncomeAttributableToOwnersOfParent", "지배기업 소유주지분", 1200))
        with self.assertRaises(SystemExit):
            resolve_shares(rows)
        # Total profit is appropriate only for an explicitly separate statement.
        shares, _ = resolve_shares(rows[:1] + rows[2:3], fs_div="OFS")
        self.assertEqual(shares, 100)

    def test_continuing_and_discontinued_eps_are_added(self):
        rows = [is_("ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", -120000),
                is_("ifrs-full_DilutedEarningsLossPerShareFromContinuingOperations", "계속영업희석주당손익", -100),
                is_("ifrs-full_DilutedEarningsLossPerShareFromDiscontinuedOperations", "중단영업희석주당손익", -20)]
        self.assertEqual(resolve_shares(rows), (1000.0, "DILUTED"))

    def test_zero_discontinued_is_observed_but_missing_is_not_zero(self):
        rows = [is_("ifrs-full_ProfitLossAttributableToOwnersOfParent", "지배기업 소유주지분", -27998804598),
                is_("ifrs-full_DilutedEarningsLossPerShareFromContinuingOperations", "계속영업희석주당손익", -1059)]
        with self.assertRaises(SystemExit):
            resolve_shares(rows)
        rows.append(is_("ifrs-full_DilutedEarningsLossPerShareFromDiscontinuedOperations", "중단영업희석주당손익", 0))
        shares, basis = resolve_shares(rows)
        self.assertAlmostEqual(shares, 26438908.968838528)
        self.assertEqual(basis, "DILUTED")

    def test_basic_and_diluted_components_cannot_be_mixed(self):
        rows = [PARENT_NI,
                is_("ifrs-full_DilutedEarningsLossPerShareFromContinuingOperations", "계속영업희석주당손익", 700),
                is_("ifrs-full_BasicEarningsLossPerShareFromDiscontinuedOperations", "중단영업기본주당손익", 0)]
        with self.assertRaises(SystemExit):
            resolve_shares(rows)

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
