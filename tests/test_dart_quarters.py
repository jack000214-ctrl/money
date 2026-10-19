"""분기 실적 복원의 산술 계약.

4분기는 별도 보고서가 없어 **연간에서 1~3분기를 뺀다.** 이 뺄셈은
"thstrm_amount 가 누계가 아니라 해당 3개월 금액"이라는 전제 위에 서 있는데,
전제가 깨진 회사에서는 음수가 나온다. 예전에는 그 음수가 그대로 화면에
찍혔고 — 이 숫자들은 저장되지 않고 print 직전에만 만들어졌으므로 — 대조할
대상조차 없었다.

실제로 한 판단이 이 경로에서 나온 "2025년 4분기 매출 32.83조(역산)"를 인용했다.

지키는 성질은 셋이다.
  1. 세 분기가 다 있어야 역산한다 (두 개로 빼면 4분기가 6개월치가 된다)
  2. 역산 결과가 음수면 값을 내지 않는다 (전제 위반이지 관측이 아니다)
  3. 기저가 0 이하면 전년동기 대비를 만들지 않는다 (성장률이 아니라 나눗셈이다)
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.display import ABSENT, display_width
from collectors.dart import (
    back_derive_q4,
    declared_currency,
    financial_period_row,
    income_identity_residual,
    quarter_metrics,
    shares_cross_check_pct,
    to_int,
)


def quarters(*triples):
    """{"1Q": (매출, 영업이익, 출처), ...}"""
    return {label: (rev, op, "All") for label, rev, op in triples}


class TestFourthQuarterBackDerivation(unittest.TestCase):
    def test_annual_minus_three_quarters(self):
        vals = quarters(("1Q", 100, 10), ("2Q", 200, 20), ("3Q", 300, 30))
        self.assertEqual(back_derive_q4((1000, 100), vals), (400, 40))

    def test_two_quarters_are_not_enough(self):
        """두 분기만 빼면 4분기가 6개월치가 된다."""
        vals = quarters(("1Q", 100, 10), ("2Q", 200, 20))
        self.assertIsNone(back_derive_q4((1000, 100), vals))

    def test_a_negative_revenue_result_is_refused(self):
        """분기 금액이 누계로 들어오는 회사에서 나오는 값이다.

        그대로 내면 화면에 진짜 실적처럼 찍히고, 읽는 쪽은 4분기 매출이
        마이너스였다고 믿는다.
        """
        # 누계로 들어오면 3분기 값이 이미 9개월치라 합이 연간을 넘는다.
        vals = quarters(("1Q", 100, 10), ("2Q", 300, 30), ("3Q", 600, 60))
        self.assertIsNone(back_derive_q4((900, 90), vals))

    def test_a_negative_operating_income_is_kept(self):
        """영업손실은 실제로 일어난다. 매출과 달리 음수가 정상이다."""
        vals = quarters(("1Q", 100, 10), ("2Q", 200, 20), ("3Q", 300, 30))
        self.assertEqual(back_derive_q4((1000, 0), vals), (400, -60))

    def test_a_missing_operating_income_stops_the_derivation(self):
        """fetch_is 는 매출만 찾고 영업이익을 못 찾을 수 있다."""
        vals = quarters(("1Q", 100, None), ("2Q", 200, 20), ("3Q", 300, 30))
        self.assertIsNone(back_derive_q4((1000, 100), vals))

    def test_a_missing_annual_figure_stops_the_derivation(self):
        vals = quarters(("1Q", 100, 10), ("2Q", 200, 20), ("3Q", 300, 30))
        self.assertIsNone(back_derive_q4((None, 100), vals))
        self.assertIsNone(back_derive_q4((1000, None), vals))

    def test_zero_fourth_quarter_is_a_value_not_a_refusal(self):
        vals = quarters(("1Q", 100, 10), ("2Q", 200, 20), ("3Q", 300, 30))
        self.assertEqual(back_derive_q4((600, 60), vals), (0, 0))


class TestQuarterMetrics(unittest.TestCase):
    def series(self, **rows):
        return {label: (rev, op, "All") for label, (rev, op) in rows.items()}

    def test_operating_margin_is_income_over_revenue(self):
        rows = quarter_metrics(self.series(**{"2Q26": (79_318_746, 60_542_608)}))
        self.assertEqual(rows[0]["operating_margin_pct"], 76.33)

    def test_year_over_year_compares_the_same_quarter(self):
        rows = quarter_metrics(self.series(
            **{"2Q25": (22_231_952, 9_212_851), "2Q26": (79_318_746, 60_542_608)}
        ))
        latest = next(r for r in rows if r["label"] == "2Q26")
        self.assertEqual(latest["revenue_yoy_pct"], 256.8)

    def test_the_first_year_has_no_prior_and_says_so(self):
        rows = quarter_metrics(self.series(**{"1Q26": (100, 10)}))
        self.assertIsNone(rows[0]["revenue_yoy_pct"])

    def test_a_zero_base_produces_no_growth_rate(self):
        """0 으로 나누면 터지고, 채우면 무한대가 성장률로 찍힌다."""
        rows = quarter_metrics(self.series(
            **{"1Q25": (0, -5), "1Q26": (100, 10)}
        ))
        latest = next(r for r in rows if r["label"] == "1Q26")
        self.assertIsNone(latest["revenue_yoy_pct"])

    def test_a_negative_base_produces_no_growth_rate(self):
        """적자에서 흑자로 돌아선 해의 대비는 부호가 뒤집힌 숫자가 나온다."""
        rows = quarter_metrics(self.series(
            **{"1Q25": (-100, -50), "1Q26": (100, 10)}
        ))
        latest = next(r for r in rows if r["label"] == "1Q26")
        self.assertIsNone(latest["revenue_yoy_pct"])

    def test_zero_revenue_produces_no_margin(self):
        rows = quarter_metrics(self.series(**{"1Q26": (0, -10)}))
        self.assertIsNone(rows[0]["operating_margin_pct"])

    def test_a_missing_operating_income_empties_only_the_margin(self):
        """영업이익을 못 찾았다고 매출까지 버리면 분기 시계열이 끊긴다."""
        rows = quarter_metrics(self.series(**{"1Q26": (1000, None)}))
        self.assertEqual(rows[0]["revenue"], 1000)
        self.assertIsNone(rows[0]["operating_margin_pct"])

    def test_an_operating_loss_gives_a_negative_margin(self):
        rows = quarter_metrics(self.series(**{"1Q23": (1000, -200)}))
        self.assertEqual(rows[0]["operating_margin_pct"], -20.0)

    def test_the_source_label_is_carried_through(self):
        """역산값인지 실측인지가 표에 남아야 한다."""
        rows = quarter_metrics({"4Q25": (100, 10, "All-역산")})
        self.assertEqual(rows[0]["source"], "All-역산")



class TestIncomeIdentity(unittest.TestCase):
    """비용 계정을 못 찾았다는 사실을 드러내는 검사.

    표준코드에 없는 비용 라인이 있으면 영업이익이 매출총이익에서 일부만 뺀
    값이 되고, 그대로 저장되면 마진이 과대계상된다. 숫자는 그럴듯하게 나온다.
    """

    def test_a_balanced_statement_has_no_residual(self):
        residual, _ = income_identity_residual(1000e8, [400e8], 600e8)
        self.assertEqual(residual, 0.0)

    def test_a_missing_expense_line_shows_up_as_residual(self):
        residual, tolerance = income_identity_residual(1000e8, [], 600e8)
        self.assertGreater(abs(residual), tolerance)

    def test_rounding_noise_stays_within_tolerance(self):
        residual, tolerance = income_identity_residual(
            1000e8, [400e8], 600e8 - 1e7
        )
        self.assertLessEqual(abs(residual), tolerance)

    def test_tolerance_scales_with_operating_income(self):
        """조 단위 회사의 반올림 오차가 중소형주의 실제 누락보다 크다."""
        _, small = income_identity_residual(10e8, [4e8], 6e8)
        _, large = income_identity_residual(10000e8, [4000e8], 6000e8)
        self.assertGreater(large, small)

    def test_a_floor_keeps_tiny_companies_from_a_zero_tolerance(self):
        """영업이익이 0 이면 비례 허용오차도 0 이라 모든 반올림이 실패가 된다."""
        _, tolerance = income_identity_residual(100e8, [100e8], 0.0)
        self.assertEqual(tolerance, 1e8)

    def test_several_expense_lines_are_summed(self):
        residual, _ = income_identity_residual(1000e8, [300e8, 100e8], 600e8)
        self.assertEqual(residual, 0.0)


class TestSharesCrossCheck(unittest.TestCase):
    def test_matching_counts_have_no_gap(self):
        self.assertEqual(shares_cross_check_pct(1000.0, 1000.0), 0.0)

    def test_a_gap_is_reported_as_an_absolute_percentage(self):
        self.assertAlmostEqual(shares_cross_check_pct(1100.0, 1000.0), 10.0)  # pyright: ignore[reportArgumentType, reportCallIssue]
        self.assertAlmostEqual(shares_cross_check_pct(900.0, 1000.0), 10.0)  # pyright: ignore[reportArgumentType, reportCallIssue]

    def test_a_missing_side_yields_nothing_rather_than_zero(self):
        """0% 어긋남과 '비교할 수 없다'는 다른 사건이다."""
        self.assertIsNone(shares_cross_check_pct(1000.0, None))
        self.assertIsNone(shares_cross_check_pct(None, 1000.0))
        self.assertIsNone(shares_cross_check_pct(1000.0, 0))

class TestFinancialPeriodRow(unittest.TestCase):
    """`cio` 밖에서 쓰는 `dart financials` 한 줄.

    이 줄은 예전에 **값이 하나만 없어도 통째로 사라졌다.** 사라진 줄은 "그 기간
    공시가 없다"로 읽히지만 실제로는 계정명을 못 찾은 것일 수도 있었다. 같은
    저장소의 `quarter_metrics` 는 반대로 매출을 살리고 마진만 비웠으므로, 두
    함수가 정반대 규칙으로 굴고 있었다.
    """

    def test_a_normal_period_shows_all_three_figures(self):
        line = financial_period_row("제 58 기 1분기", 133_873_444_000_000,
                                    57_232_797_000_000)
        self.assertIn("1,338,734억", line)
        self.assertIn("572,328억", line)
        self.assertIn("42.75%", line)

    def test_the_same_function_handles_a_company_916_times_smaller(self):
        """파마리서치와 삼성전자가 같은 코드를 지난다 — 규모별 분기는 없다."""
        line = financial_period_row("제 79 기 1분기", 146_100_260_508,
                                    57_292_342_573)
        self.assertIn("1,461억", line)
        self.assertIn("39.21%", line)

    def test_a_missing_operating_income_keeps_the_revenue(self):
        line = financial_period_row("제 57 기 3분기", 1000e8, None)
        self.assertIn("1,000억", line)
        self.assertIn(ABSENT, line)

    def test_both_missing_says_so_instead_of_vanishing(self):
        """줄이 없는 것과 '값이 없다'는 다르다 — 앞은 공시 부재로 오독된다."""
        line = financial_period_row("제 56 기", None, None)
        self.assertIn("공시에 없다", line)
        self.assertTrue(line.strip())

    def test_zero_revenue_still_prints_the_period(self):
        line = financial_period_row("제 55 기", 0, -100e8)
        self.assertIn("0억", line)
        self.assertIn("-100억", line)

    def test_a_negative_revenue_produces_no_margin(self):
        """정정 공시로 매출이 음수인 기간. 그대로 나누면 부호가 뒤집힌다."""
        line = financial_period_row("제 54 기", -500e8, -100e8)
        self.assertNotIn("20.00%", line)

    def test_an_operating_loss_keeps_its_negative_margin(self):
        line = financial_period_row("제 53 기", 1000e8, -200e8)
        self.assertIn("-20.00%", line)

    def test_korean_period_labels_do_not_shift_the_columns(self):
        """기간명은 회사가 쓴 문자열이라 한글 폭이 섞인다."""
        wide = financial_period_row("제 58 기 1분기", 1000e8, 100e8)
        narrow = financial_period_row("FY2026", 1000e8, 100e8)
        self.assertEqual(display_width(wide), display_width(narrow))



class TestAmountParsing(unittest.TestCase):
    """공시 금액 문자열이 값이 되는가.

    `to_int` 가 못 읽으면 None 이 되고, None 은 "계정을 못 찾았다"와 구분되지
    않는다. 값이 있는데도 결측으로 처리되고 화면에는 `—` 만 남는다. 현재
    코퍼스(6,224 행)에는 쉼표 섞인 정수뿐이지만, 그건 다른 형태가 오면 조용히
    사라진다는 뜻이기도 하다.
    """

    def test_a_comma_separated_integer(self):
        self.assertEqual(to_int("79,318,746"), 79318746)

    def test_a_negative_sign(self):
        self.assertEqual(to_int("-1,234"), -1234)

    def test_accounting_parentheses_mean_negative(self):
        self.assertEqual(to_int("(1,234)"), -1234)

    def test_a_decimal_is_not_thrown_away(self):
        """주당이익은 원 단위 소수로 올 수 있다."""
        self.assertEqual(to_int("1234.0"), 1234)
        self.assertEqual(to_int("5462.35"), 5462)

    def test_exponent_notation_is_read(self):
        self.assertEqual(to_int("7.9318746E7"), 79318746)

    def test_placeholders_are_absent_not_zero(self):
        for blank in ("", "-", "—", None, "abc", "nan"):
            with self.subTest(blank):
                self.assertIsNone(to_int(blank))


class TestDeclaredCurrency(unittest.TestCase):
    """원 단위 가정을 공시가 확인해 주는가.

    예전에는 `"currency": "KRW"` 하드코딩이었다. 실제 값은 맞았지만 맞는 것과
    확인된 것은 다르다 — 통화가 다른 재무제표가 들어오면 원 단위로 저장된
    값이 그대로 억원으로 환산되고, 자릿수까지 그럴듯하게 찍힌다.
    """

    def test_a_single_declared_currency_is_used(self):
        self.assertEqual(declared_currency([{"currency": "KRW"}] * 3), "KRW")

    def test_rows_without_a_currency_are_missing_not_violations(self):
        """XBRL 로 합성한 행에는 통화 칸이 없다."""
        self.assertEqual(
            declared_currency([{"currency": "KRW"}, {"currency": None}, {}]),
            "KRW")

    def test_nothing_declared_falls_back_to_won(self):
        self.assertEqual(declared_currency([{}, {}]), "KRW")

    def test_mixed_currencies_stop_the_save(self):
        with self.assertRaises(SystemExit):
            declared_currency([{"currency": "KRW"}, {"currency": "USD"}])


if __name__ == "__main__":
    unittest.main()
