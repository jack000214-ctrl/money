"""ETF 구성종목 들여다보기 — 재료를 읽는 자리와 그 위의 산술.

2026-10-09 판단은 두 숫자에 섰다. "3개월 -34% 중 환율이 얼마인가"와 "흑자
묶음이 이익의 몇 배인가". 둘 다 세션 임시 스크립트로 냈고, 그때 실제로 틀릴 뻔한
자리들이 있었다.

  1. **적자 종목을 순이익÷시가총액으로 내면 통화가 섞인다.** 홍콩 상장 본토
     기업은 실적이 위안, 시가총액이 홍콩달러다.
  2. **환율을 한 통화로 대표시켰다.** 구성의 65%가 위안인데 홍콩달러/원 하나로
     재서 환율 몫이 실제보다 크게 나왔다.
  3. **통계 페이지의 같은 라벨이 내비게이션에도 있다.** 첫 일치를 읽으면 값이
     아니라 링크 텍스트를 읽는다.
  4. **이력이 한 행뿐인 환율쌍이 있다.** 야후의 CNYKRW=X 가 그랬다.

계산기는 판정을 내지 않는다. 그 경계도 여기서 고정한다.
"""

import json
import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators import etf_lookthrough as lookthrough
from collectors import etf_constituents as constituents


def days(*pairs):
    return [(date, float(value)) for date, value in pairs]


class TestWindowReturns(unittest.TestCase):
    SERIES = days(("2026-07-01", 100), ("2026-07-09", 90), ("2026-09-08", 80),
                  ("2026-10-08", 72))

    def test_the_window_is_calendar_days_back_from_the_last_bar(self):
        result = constituents.window_returns(self.SERIES, {"1m": 30, "3m": 91})
        self.assertEqual(result["1m"], -10.0)   # 9/8 의 80 → 72
        self.assertEqual(result["3m"], -20.0)   # 7/9 의 90 → 72

    def test_a_window_longer_than_the_history_is_null(self):
        """없는 12개월을 가장 오래된 봉으로 대신하면 상장 후 수익률이 12개월로 읽힌다."""
        result = constituents.window_returns(self.SERIES, {"12m": 365})
        self.assertIsNone(result["12m"])

    def test_a_hole_in_the_history_is_not_bridged(self):
        """창 시작일 근처에 봉이 없으면 훨씬 이전 봉으로 재지 않는다."""
        series = days(("2026-01-02", 100), ("2026-10-08", 50))
        self.assertIsNone(constituents.window_returns(series, {"3m": 91})["3m"])

    def test_one_bar_gives_nothing(self):
        result = constituents.window_returns(days(("2026-10-08", 1)), {"1m": 30})
        self.assertEqual(result, {"1m": None})


class TestDrawdown(unittest.TestCase):
    def test_measured_from_the_high_inside_twelve_months(self):
        series = days(("2024-01-02", 500), ("2026-01-05", 200), ("2026-10-08", 150))
        self.assertEqual(constituents.drawdown_12m(series), -25.0)

    def test_one_bar_is_not_a_drawdown_of_zero(self):
        self.assertIsNone(constituents.drawdown_12m(days(("2026-10-08", 1))))


class TestCrossRate(unittest.TestCase):
    """구성종목 통화 → ETF 통화. 달러를 거쳐 한 가지 방식으로만 낸다."""

    KRW = days(("2026-10-07", 1350), ("2026-10-08", 1340))
    CNY = days(("2026-10-07", 6.75), ("2026-10-08", 6.70))

    def test_two_non_dollar_currencies_cross_through_the_dollar(self):
        points = constituents.cross_rate(self.KRW, self.CNY)
        self.assertEqual(points[-1][0], "2026-10-08")
        self.assertAlmostEqual(points[-1][1], 200.0)

    def test_dates_missing_on_either_side_are_dropped(self):
        points = constituents.cross_rate(self.KRW, self.CNY[:1])
        self.assertEqual([date for date, _ in points], ["2026-10-07"])

    def test_a_dollar_holding_uses_the_quote_series_as_is(self):
        self.assertEqual(constituents.cross_rate(self.KRW, None), self.KRW)

    def test_a_dollar_quote_inverts_the_holding_series(self):
        points = constituents.cross_rate(None, self.CNY)
        self.assertAlmostEqual(points[-1][1], 1 / 6.70)


STATS_PAGE = """
<html><head><title>Shenzhen Inovance Technology Co.,Ltd (SHE:300124) Statistics &amp; Valuation Metrics</title></head>
<nav><ul><li><a href="/x/">PE Ratio</a></li><li><a href="/y/">Forward PE</a></li></ul></nav>
<table>
<tr><td><span>PE Ratio<!----></span></td><td class="px" title="27.970">27.97</td></tr>
<tr><td><span>Forward PE<!----></span></td><td class="px" title="20.481">20.48</td></tr>
<tr><td><span>PS Ratio<!----></span></td><td class="px" title="2.739">2.74</td></tr>
<tr><td><span>PB Ratio<!----></span></td><td class="px">3.50</td></tr>
<tr><td><span>Profit Margin<!----></span></td><td class="px" title="9.93%">9.93%</td></tr>
</table></html>
"""

LOSS_PAGE = STATS_PAGE.replace('title="27.970">27.97', ">n/a").replace(
    'title="20.481">20.48', ">n/a").replace('title="9.93%">9.93%', 'title="-22.64%">-22.64%')


class TestReadingTheStatisticsPage(unittest.TestCase):
    def test_values_come_from_the_table_not_the_navigation(self):
        values = constituents.parse_statistics(STATS_PAGE)
        self.assertEqual(values["trailing_pe"], 27.97)
        self.assertEqual(values["forward_pe"], 20.481)

    def test_a_cell_without_a_title_attribute_is_read_from_its_text(self):
        self.assertEqual(constituents.parse_statistics(STATS_PAGE)["pb"], 3.5)

    def test_the_percent_sign_is_dropped_and_the_sign_kept(self):
        self.assertEqual(
            constituents.parse_statistics(LOSS_PAGE)["profit_margin_pct"], -22.64)

    def test_a_loss_maker_has_no_multiple_rather_than_zero(self):
        values = constituents.parse_statistics(LOSS_PAGE)
        self.assertIsNone(values["forward_pe"])
        self.assertIsNone(values["trailing_pe"])

    def test_a_page_with_nothing_gives_all_nulls(self):
        values = constituents.parse_statistics("<html>Not found</html>")
        self.assertEqual(set(values.values()), {None})

    def test_the_title_identifies_the_company(self):
        """주소를 잘못 만들면 다른 종목 페이지가 정상처럼 온다."""
        self.assertTrue(constituents.page_matches(STATS_PAGE, "300124"))
        self.assertFalse(constituents.page_matches(STATS_PAGE, "300125"))
        self.assertFalse(constituents.page_matches("<html></html>", "300124"))


class TestRouting(unittest.TestCase):
    def test_hong_kong_codes_are_zero_padded(self):
        routed = constituents.route("425.HK", "HK")
        assert routed is not None
        self.assertEqual(routed["yahoo"], ["0425.HK"])
        self.assertEqual(routed["stats"], "/quote/hkg/0425/statistics/")

    def test_shenzhen_and_shanghai_go_to_different_pages(self):
        self.assertIn("/she/", constituents.route("300124.SZ", "CN")["stats"])  # pyright: ignore[reportOptionalSubscript]
        self.assertIn("/sha/", constituents.route("603501.SS", "CN")["stats"])  # pyright: ignore[reportOptionalSubscript]

    def test_a_korean_code_tries_both_boards(self):
        self.assertEqual(constituents.route("005930", "KR")["yahoo"],  # pyright: ignore[reportOptionalSubscript]
                         ["005930.KS", "005930.KQ"])

    def test_a_us_class_share_uses_a_dash_for_prices(self):
        self.assertEqual(constituents.route("BRK.B", "US")["yahoo"], ["BRK-B"])  # pyright: ignore[reportOptionalSubscript]

    def test_an_unknown_notation_is_not_guessed(self):
        self.assertIsNone(constituents.route("ABC_ZZ_EQUITY", ""))
        self.assertIsNone(constituents.route("005930", ""))


def holding(holding_id, weight, *, forward=None, trailing=None, ps=None,
            margin=None, currency="CNY", ret=None, close=None, market="CN"):
    return {
        "holding_id": holding_id, "weight_pct": weight, "market": market,
        "price": {"currency": currency, "as_of": "2026-10-08", "close": close,
                  "return_pct": {"3m": ret}, "drawdown_12m_pct": None},
        "valuation": {"forward_pe": forward, "trailing_pe": trailing, "ps": ps,
                      "profit_margin_pct": margin},
    }


class TestWeightedMultiples(unittest.TestCase):
    def test_the_basket_multiple_is_harmonic_not_arithmetic(self):
        """PER 10배와 40배를 반반 담으면 이익은 0.05+0.0125 — 16배다. 25배가 아니다."""
        figures = lookthrough.valuation([
            holding("A", 50, forward=10), holding("B", 50, forward=40)])
        self.assertEqual(figures["forward_pe"], 16.0)
        self.assertEqual(figures["forward_pe_weight_pct"], 100)

    def test_one_extreme_multiple_does_not_dominate(self):
        figures = lookthrough.valuation([
            holding("A", 90, forward=20), holding("B", 10, forward=200)])
        self.assertLess(figures["forward_pe"], 22)

    def test_loss_makers_are_outside_the_multiple_and_counted_separately(self):
        figures = lookthrough.valuation([
            holding("A", 70, forward=20, ps=2, margin=10),
            holding("LOSS", 30, ps=12, margin=-24)])
        self.assertEqual(figures["forward_pe"], 20.0)
        self.assertEqual(figures["forward_pe_weight_pct"], 70)
        self.assertEqual(figures["loss_making_weight_pct"], 30)

    def test_the_overall_yield_includes_losses_as_negative(self):
        # 0.7 × (1/20) + 0.3 × (-0.24/12) = 0.035 - 0.006 = 2.9%
        figures = lookthrough.valuation([
            holding("A", 70, forward=20), holding("LOSS", 30, ps=12, margin=-24)])
        self.assertEqual(figures["earnings_yield_all_pct"], 2.9)

    def test_a_loss_is_measured_by_margin_over_sales_multiple(self):
        """순이익÷시가총액은 통화가 섞인다. 비율끼리 나눈다."""
        value, basis = lookthrough.earnings_yield(holding("LOSS", 1, ps=12, margin=-24))
        self.assertAlmostEqual(value, -0.02)  # pyright: ignore[reportArgumentType, reportCallIssue]
        self.assertEqual(basis, "TRAILING_MARGIN_OVER_PS")

    def test_a_holding_with_nothing_is_no_data_not_zero(self):
        figures = lookthrough.valuation([
            holding("A", 80, forward=20), holding("BLANK", 20)])
        self.assertEqual(figures["no_data_weight_pct"], 20)
        self.assertEqual(figures["earnings_yield_weight_pct"], 80)

    def test_profitable_without_a_forward_estimate_is_its_own_bucket(self):
        figures = lookthrough.valuation([holding("A", 100, ps=2, margin=10)])
        self.assertEqual(figures["profitable_without_forward_weight_pct"], 100)
        self.assertEqual(figures["loss_making_weight_pct"], 0)
        self.assertIsNone(figures["forward_pe"])


def material(*holdings, etf_return=-30.0, fx=None):
    return {
        "asset_id": "TEST", "collected_at": "2026-10-08T17:00:00+00:00",
        "holdings_as_of": "2026-10-08", "windows_days": {"3m": 91},
        "valuation_source": {"provider": "example", "grade": "SECONDARY"},
        "etf": {"as_of": "2026-10-08", "return_pct": {"3m": etf_return}},
        "fx": fx or {"CNY": {"basis": "CROSS_VIA_USD", "return_pct": {"3m": -10.0}},
                     "HKD": {"basis": "CROSS_VIA_USD", "return_pct": {"3m": -12.0}}},
        "holdings": list(holdings), "unresolved": [],
    }


class TestReturnAttribution(unittest.TestCase):
    def test_each_holding_uses_its_own_currency(self):
        """환율을 한 통화로 대표시키면 환율 몫이 틀린다."""
        data = material(holding("A", 60, ret=-20.0, currency="CNY"),
                        holding("B", 40, ret=-20.0, currency="HKD"))
        row = lookthrough.attribution(data)["3m"]
        self.assertEqual(row["holdings_local_pct"], -20.0)
        self.assertEqual(row["fx_pct"], -10.8)      # 0.6×-10 + 0.4×-12

    def test_local_and_currency_legs_compound(self):
        data = material(holding("A", 100, ret=-20.0, currency="CNY"))
        row = lookthrough.attribution(data)["3m"]
        self.assertEqual(row["holdings_in_quote_pct"], -28.0)   # 0.8 × 0.9 - 1

    def test_the_residual_is_reported_not_forced_to_zero(self):
        data = material(holding("A", 100, ret=-20.0, currency="CNY"), etf_return=-30.0)
        row = lookthrough.attribution(data)["3m"]
        self.assertEqual(row["unexplained_pp"], -2.0)

    def test_a_holding_without_a_return_is_left_out_and_the_coverage_says_so(self):
        data = material(holding("A", 70, ret=-20.0), holding("NEW", 30, ret=None))
        row = lookthrough.attribution(data)["3m"]
        self.assertEqual(row["covered_weight_pct"], 70)
        self.assertEqual(row["holdings_local_pct"], -20.0)

    def test_an_unavailable_currency_leaves_the_holding_out(self):
        data = material(
            holding("A", 100, ret=-20.0, currency="CNY"),
            fx={"CNY": {"basis": "UNAVAILABLE", "return_pct": {"3m": None}}})
        self.assertIsNone(lookthrough.attribution(data)["3m"])


class TestComparisonWithAnEarlierSnapshot(unittest.TestCase):
    """조건부 매수 계획의 "forward 이익이 15% 이상 하향"을 확인하는 산술."""

    THEN = material(holding("A", 60, forward=20, close=100),
                    holding("B", 40, forward=25, close=50))

    def test_unchanged_estimates_show_no_change_even_if_prices_fell(self):
        """주가가 내려 PER 이 낮아진 것은 이익이 변한 것이 아니다."""
        now = material(holding("A", 60, forward=10, close=50),
                       holding("B", 40, forward=12.5, close=25))
        result = lookthrough.compare(now, self.THEN)
        self.assertEqual(result["forward_earnings_change_pct"], 0.0)
        self.assertEqual(result["forward_pe_at_reference_prices"],
                         result["reference_forward_pe"])

    def test_a_cut_in_estimates_raises_the_multiple_at_the_old_prices(self):
        now = material(holding("A", 60, forward=25, close=100),    # EPS 5 → 4
                       holding("B", 40, forward=25, close=50))
        result = lookthrough.compare(now, self.THEN)
        self.assertLess(result["forward_earnings_change_pct"], 0)
        self.assertGreater(result["forward_pe_at_reference_prices"],
                           result["reference_forward_pe"])

    def test_holdings_that_left_or_joined_are_listed(self):
        now = material(holding("A", 60, forward=20, close=100),
                       holding("C", 40, forward=30, close=10))
        result = lookthrough.compare(now, self.THEN)
        self.assertEqual(result["dropped_since_reference"], ["B"])
        self.assertEqual(result["added_since_reference"], ["C"])
        self.assertEqual(result["matched_weight_pct"], 60)


class TestThresholdToPrice(unittest.TestCase):
    def test_only_the_forward_basket_moves(self):
        data = material(holding("A", 70, forward=24), holding("LOSS", 30, ps=12, margin=-20))
        rows = lookthrough.price_at_forward_pe(data, 10_000, [12.0])
        # 흑자 묶음 70% 가 절반이 된다 → ETF -35%
        self.assertEqual(rows[0]["etf_price"], 6500.0)
        self.assertEqual(rows[0]["vs_input_price_pct"], -35.0)

    def test_no_forward_basket_means_no_price(self):
        data = material(holding("LOSS", 100, ps=12, margin=-20))
        self.assertIsNone(lookthrough.price_at_forward_pe(data, 10_000, [20.0])[0]["etf_price"])


class TestJudgmentBoundary(unittest.TestCase):
    """계산기는 측정만 한다. 판정 어휘가 값으로 나오면 실패한다."""

    FORBIDDEN = (
        "싸다", "비싸다", "저평가", "고평가", "매수", "매도", "추천",
        "권장", "줄여", "늘려", "과도", "적정", "위험하다", "BUY", "SELL",
        "OVERWEIGHT", "UNDERWEIGHT", "RECOMMEND", "CHEAP", "EXPENSIVE",
    )

    @classmethod
    def data_only(cls, node):
        if isinstance(node, dict):
            return {key: cls.data_only(value) for key, value in node.items()
                    if not key.startswith("_")}
        if isinstance(node, list):
            return [cls.data_only(item) for item in node]
        return node

    def test_output_contains_no_verdict_vocabulary(self):
        data = material(holding("A", 70, forward=24, ret=-20.0),
                        holding("LOSS", 30, ps=12, margin=-20, ret=-40.0))
        output = {
            "summary": lookthrough.summarize(data),
            "at": lookthrough.price_at_forward_pe(data, 10_000, [20.0]),
            "compare": lookthrough.compare(data, data),
        }
        blob = json.dumps(self.data_only(output), ensure_ascii=False)
        for word in self.FORBIDDEN:
            self.assertNotIn(word, blob)

    def test_the_packet_block_explains_what_it_is(self):
        block = lookthrough.summarize(material(holding("A", 100, forward=20)))
        self.assertIn("판정이 아니다", block["_role"])
        self.assertIn("2차 출처", block["_role"])


class TestArtifactContract(unittest.TestCase):
    def valid(self):
        return {**material(holding("A", 100, forward=20)),
                "schema_version": constituents.SCHEMA_VERSION}

    def test_a_valid_artifact_passes(self):
        self.assertEqual(constituents.validate_artifact(self.valid(), "TEST"), [])

    def test_another_assets_file_is_refused(self):
        errors = constituents.validate_artifact(self.valid(), "OTHER")
        self.assertTrue(any("asset_id" in error for error in errors))

    def test_a_timestamp_without_a_timezone_is_refused(self):
        data = {**self.valid(), "collected_at": "2026-10-08T17:00:00"}
        self.assertTrue(any("시간대" in error
                            for error in constituents.validate_artifact(data, "TEST")))

    def test_an_empty_holdings_list_is_refused(self):
        data = {**self.valid(), "holdings": []}
        self.assertTrue(constituents.validate_artifact(data, "TEST"))


if __name__ == "__main__":
    unittest.main()
