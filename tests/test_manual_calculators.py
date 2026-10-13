"""손으로 부르는 조사 보조 계산기들의 계약.

`peer_kr` 와 `sector_strength` 는 refresh 배선에 없다 — packet 을 만들지 않고
사용자가 직접 불러 읽는 도구다. 그래서 오래 고아처럼 남아 있었고 테스트도
없었다. 배선이 없다는 것과 **틀려도 된다**는 것은 다르다. 여기 나오는 숫자도
모델이 조사 결과로 인용할 수 있다.

지키는 성질은 하나로 모인다 — **모자란 데이터를 지어내지 않는다.**
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators import sector_strength
from calculators.peer_kr import fmt, num, pick, same_basis_peers


class TestPeersShareTheTargetsFiscalYear(unittest.TestCase):
    """재무는 요청 연도가 없으면 한 해 전으로 내려간다. 그 사실을 '기준' 열에
    적으면서도 상대 위치는 연도를 섞어 냈고, 화면은 "전 종목 같은 기준으로
    통일했다"고 말했다. 다른 해의 이익으로 낸 배수는 같은 통에 넣지 않는다."""

    def test_a_peer_from_another_fiscal_year_is_set_aside(self):
        target = {"code": "T", "basis": "2025", "per": 15.0}
        same = {"code": "A", "basis": "2025", "per": 10.0}
        other = {"code": "B", "basis": "2024", "per": 30.0}
        kept, set_aside = same_basis_peers(target, [same, other])
        self.assertEqual(kept, [same])
        self.assertEqual(set_aside, [other])

    def test_a_peer_with_no_financials_has_no_year_to_match(self):
        target = {"code": "T", "basis": "2025"}
        unknown = {"code": "C", "basis": None}
        self.assertEqual(same_basis_peers(target, [unknown]), ([], [unknown]))

    def test_an_unknown_target_year_compares_with_nothing(self):
        peer = {"code": "A", "basis": "2025"}
        self.assertEqual(same_basis_peers({"code": "T", "basis": None}, [peer]),
                         ([], [peer]))


class TestPeerNumberParsing(unittest.TestCase):
    def test_commas_and_whitespace_are_stripped(self):
        self.assertEqual(num(" 1,234,567 "), 1234567.0)

    def test_unparseable_values_are_missing_not_zero(self):
        for bad in (None, "", "-", "해당없음", object()):
            with self.subTest(repr(bad)):
                self.assertIsNone(num(bad))

    def test_negative_amounts_survive(self):
        self.assertEqual(num("-77,303"), -77303.0)


class TestPeerAccountPick(unittest.TestCase):
    def rows(self, *entries):
        return [
            {"sj_div": div, "account_nm": name, "thstrm_amount": amount}
            for div, name, amount in entries
        ]

    def test_first_matching_name_wins_in_the_order_given(self):
        """이름 목록의 순서가 우선순위다 — 회사마다 계정명이 다르다."""
        rows = self.rows(
            ("IS", "영업이익", "100"),
            ("IS", "영업이익(손실)", "200"),
        )
        self.assertEqual(pick(rows, ["영업이익(손실)", "영업이익"]), 200.0)

    def test_spaces_inside_account_names_are_ignored(self):
        """DART 계정명에는 공백이 불규칙하게 들어간다."""
        rows = self.rows(("IS", "영 업 이 익", "100"))
        self.assertEqual(pick(rows, ["영업이익"]), 100.0)

    def test_a_row_from_the_wrong_statement_is_not_used(self):
        """재무상태표의 같은 이름 계정을 손익계산서 값으로 읽으면 안 된다."""
        rows = self.rows(("BS", "영업이익", "100"))
        self.assertIsNone(pick(rows, ["영업이익"]))

    def test_comprehensive_income_statement_is_accepted(self):
        rows = self.rows(("CIS", "영업이익", "100"))
        self.assertEqual(pick(rows, ["영업이익"]), 100.0)

    def test_no_match_is_none(self):
        self.assertIsNone(pick(self.rows(("IS", "매출액", "1")), ["영업이익"]))


class TestPeerFormatting(unittest.TestCase):
    def test_missing_values_render_as_a_dash_not_zero(self):
        """0 으로 채우면 '적자라 없다'가 '0원'으로 읽힌다."""
        self.assertEqual(fmt(None, ",.0f", 8), "       —")

    def test_present_values_use_the_given_spec(self):
        self.assertEqual(fmt(1234.0, ",.0f", 8), "1,234")


class TestSectorWindowReturn(unittest.TestCase):
    def series(self, closes):
        return [(f"2026-01-{i:02d}", float(v)) for i, v in enumerate(closes, 1)]

    def test_return_is_measured_over_exactly_n_trading_days(self):
        series = self.series([100, 101, 102, 103, 110])
        self.assertAlmostEqual(sector_strength.window_return(series, 4), 10.0)

    def test_short_history_returns_none_instead_of_a_partial_window(self):
        """모자란 구간을 있는 만큼으로 계산하면 1개월과 12개월이 같은 값이 된다."""
        series = self.series([100, 110])
        self.assertIsNone(sector_strength.window_return(series, 21))

    def test_exactly_enough_history_is_enough(self):
        series = self.series([100, 101, 102])
        self.assertIsNotNone(sector_strength.window_return(series, 2))

    def test_a_fall_is_negative(self):
        series = self.series([100, 100, 50])
        self.assertAlmostEqual(sector_strength.window_return(series, 2), -50.0)


class TestSectorCloses(unittest.TestCase):
    def test_comment_headers_are_skipped_and_blanks_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "XLK.csv").write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol XLK / currency USD\n"
                "date,close\n"
                "2026-08-06,100.5\n"
                "2026-08-07,\n"
                "2026-08-10,102.0\n",
                encoding="utf-8",
            )
            with patch.object(sector_strength, "PRICE_DIR", directory):
                rows = sector_strength.closes("XLK")
        self.assertEqual(rows, [("2026-08-06", 100.5), ("2026-08-10", 102.0)])

    def test_a_missing_price_file_stops_with_the_command_to_run(self):
        absent = helpers.ROOT / "knowledge" / "prices" / "없는하위폴더"
        with patch.object(sector_strength, "PRICE_DIR", absent):
            with self.assertRaises(SystemExit):
                sector_strength.closes("XLK")


class TestSectorRoster(unittest.TestCase):
    def test_every_sector_has_a_price_symbol_and_a_korean_label(self):
        for symbol, label in sector_strength.SECTORS:
            with self.subTest(symbol):
                self.assertTrue(symbol.isupper())
                self.assertTrue(label.strip())

    def test_the_benchmark_is_not_one_of_the_sectors(self):
        """SPY 를 섹터 목록에 넣으면 자기 대비 초과수익이 0으로 섞여 든다."""
        self.assertNotIn(
            sector_strength.BENCHMARK[0],
            [symbol for symbol, _ in sector_strength.SECTORS],
        )


if __name__ == "__main__":
    unittest.main()
