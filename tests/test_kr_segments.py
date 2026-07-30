"""사업부문별 요약 재무 현황 파싱의 회귀 테스트.

이 표가 없으면 폴드8 뉴스와 D램 판가 뉴스에 같은 무게를 싣게 된다. 삼성전자
2026년 1분기 영업이익은 DS 93.8%·DX 5.2%이고, 그 격차가 조사 순서를 정한다.

가장 위험한 실패는 파싱이 안 되는 것이 아니라 **엉뚱한 표를 부문표로 착각해
그럴듯한 배분을 지어내는 것**이다. 그래서 판별에 실패하면 멈춘다.
"""

import unittest

from calculators.contracts import ContractError
from collectors.kr_segments import (
    _amount,
    _share,
    find_segment_table,
    parse_segment_table,
)

SEGMENT_TABLE = """<TABLE ACLASS="NORMAL"><TBODY>
<TR><TD>부문</TD><TD>구 분</TD><TD>제58기 1분기</TD><TD>제57기</TD></TR>
<TR><TD>금액</TD><TD>비중</TD><TD>금액</TD><TD>비중</TD></TR>
<TR><TD>DX 부문</TD><TD>매출액</TD><TD>526,547</TD><TD>39.3%</TD><TD>1,879,673</TD><TD>56.3%</TD></TR>
<TR><TD>영업이익</TD><TD>29,677</TD><TD>5.2%</TD><TD>128,527</TD><TD>29.5%</TD></TR>
<TR><TD>총자산</TD><TD>3,174,942</TD><TD>36.0%</TD><TD>2,810,397</TD><TD>36.0%</TD></TR>
<TR><TD>DS 부문</TD><TD>매출액</TD><TD>817,156</TD><TD>61.0%</TD><TD>1,301,282</TD><TD>39.0%</TD></TR>
<TR><TD>영업이익</TD><TD>536,633</TD><TD>93.8%</TD><TD>248,581</TD><TD>57.0%</TD></TR>
<TR><TD>총자산</TD><TD>4,364,341</TD><TD>49.5%</TD><TD>3,719,620</TD><TD>47.6%</TD></TR>
</TBODY></TABLE>"""

DECOY_REVENUE_TABLE = """<TABLE><TBODY>
<TR><TD>부문</TD><TD>주요 제품</TD><TD>매출액</TD><TD>비중</TD></TR>
<TR><TD>DX 부문</TD><TD>TV, 스마트폰</TD><TD>526,547</TD><TD>39.3%</TD></TR>
</TBODY></TABLE>"""


class TestNumbers(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertEqual(_amount("536,633"), 536633.0)

    def test_loss_marked_with_triangle(self):
        # DART 는 적자를 △ 로 적는다. 부호를 놓치면 적자 부문이 흑자로 뒤집힌다.
        self.assertEqual(_amount("△12,345"), -12345.0)

    def test_loss_in_parentheses(self):
        self.assertEqual(_amount("(12,345)"), -12345.0)

    def test_dash_is_absent_not_zero(self):
        self.assertIsNone(_amount("-"))

    def test_share_percentage(self):
        self.assertEqual(_share("93.8%"), 93.8)

    def test_negative_share(self):
        self.assertEqual(_share("△5.2%"), -5.2)

    def test_value_without_percent_is_not_a_share(self):
        self.assertIsNone(_share("536,633"))


class TestTableSelection(unittest.TestCase):
    def test_picks_the_one_table_with_all_three_markers(self):
        document = DECOY_REVENUE_TABLE + SEGMENT_TABLE
        self.assertIsNotNone(find_segment_table(document))

    def test_revenue_only_table_is_not_mistaken_for_it(self):
        self.assertIsNone(find_segment_table(DECOY_REVENUE_TABLE))

    def test_ambiguity_refuses_rather_than_guesses(self):
        # 후보가 둘이면 어느 쪽인지 알 수 없다. 지어내는 대신 비운다.
        self.assertIsNone(find_segment_table(SEGMENT_TABLE + SEGMENT_TABLE))

    def test_no_table_is_none(self):
        self.assertIsNone(find_segment_table("<html>부문 이야기만 있다</html>"))


class TestParse(unittest.TestCase):
    def setUp(self):
        self.periods, self.segments = parse_segment_table(SEGMENT_TABLE)

    def test_reads_every_period_column(self):
        self.assertEqual(self.periods, ["제58기 1분기", "제57기"])

    def test_reads_every_segment(self):
        self.assertEqual(
            [segment["name"] for segment in self.segments],
            ["DX 부문", "DS 부문"],
        )

    def test_segment_name_carries_across_its_rows(self):
        # 부문 이름은 rowspan 이라 첫 행에만 나온다. 이어붙이지 못하면
        # 영업이익이 어느 부문 것인지 사라진다.
        ds = self.segments[1]
        self.assertEqual(ds["operating_income"][0]["amount"], 536633.0)
        self.assertEqual(ds["operating_income"][0]["share_pct"], 93.8)

    def test_the_number_that_settles_the_fold8_question(self):
        dx = self.segments[0]
        self.assertEqual(dx["operating_income"][0]["share_pct"], 5.2)
        # 매출 비중과 영업이익 비중이 크게 다른 것이 이 표의 요점이다.
        self.assertEqual(dx["revenue"][0]["share_pct"], 39.3)

    def test_prior_period_is_kept_so_the_trend_is_visible(self):
        ds = self.segments[1]
        self.assertEqual(ds["operating_income"][1]["share_pct"], 57.0)

    def test_all_three_rows_are_captured(self):
        for segment in self.segments:
            self.assertIn("revenue", segment)
            self.assertIn("operating_income", segment)
            self.assertIn("total_assets", segment)

    def test_missing_operating_income_is_refused(self):
        broken = SEGMENT_TABLE.replace("<TD>영업이익</TD>", "<TD>기타</TD>")
        with self.assertRaises(ContractError):
            parse_segment_table(broken)

    def test_table_without_period_columns_is_refused(self):
        broken = "<TABLE><TR><TD>부문</TD><TD>구 분</TD></TR>" \
                 "<TR><TD>DX</TD><TD>영업이익</TD></TR></TABLE>"
        with self.assertRaises(ContractError):
            parse_segment_table(broken)


if __name__ == "__main__":
    unittest.main()
