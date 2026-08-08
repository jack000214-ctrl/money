"""수급·대차·KRX 원문 수집기의 파싱 계약.

이 세 모듈은 판단의 `positioning`("외국인 순매도 350만주", "대차잔고 -8.1%")에
그대로 인용되는데 테스트가 한 줄도 없었다. 수급은 이익 동인이 아니라는 이유로
계약에서 빠지기 쉬운 자리이고, 실제로 그 실패가 한 번 있었다.

여기서 지키는 성질은 셋이다.
  1. **빈 칸은 0 이 아니라 결측**이다 — 순매수 0 과 '모른다'는 다른 사건이다
  2. **필터가 안 걸리면 저장하지 않는다** — 다른 종목의 대차잔고를 이 종목
     것으로 읽는 것이 가장 나쁜 실패다
  3. **종목코드는 정확히 일치해야 한다** — 임의 행으로 폴백하지 않는다
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import krx
from collectors.kr_flow import cells, num, parse_market_page, parse_stock_page
from collectors.kr_lending import num as lending_num
from collectors.kr_lending import to_row, verify_rows


def market_html(*rows):
    body = "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>"
        for row in rows
    )
    return f"<table>{body}</table>"


class TestNaverNumberParsing(unittest.TestCase):
    """네이버는 부호를 +/− 로 적고 천단위 콤마를 쓴다."""

    def test_signs_and_separators(self):
        cases = {
            "1,234": 1234.0,
            "+1,234": 1234.0,
            "−1,234": -1234.0,   # U+2212 minus sign
            "-1,234": -1234.0,
            "0": 0.0,
        }
        for text, expected in cases.items():
            with self.subTest(text):
                self.assertEqual(num(text), expected)

    def test_blank_is_missing_not_zero(self):
        """0 으로 접으면 '순매수 0'과 '모른다'가 같은 값이 된다."""
        for blank in ("", "   ", "-", "--"):
            with self.subTest(repr(blank)):
                self.assertIsNone(num(blank))

    def test_garbage_is_missing_rather_than_an_exception(self):
        self.assertIsNone(num("N/A"))

    def test_cells_strip_tags_and_nbsp(self):
        """네이버는 빈 칸을 실제 U+00A0 으로 채운다. 그냥 두면 결측이 아니게 된다."""
        row = "<td><span>1,000</span></td><td>\xa0</td>"
        self.assertEqual(cells(row), ["1,000", ""])
        self.assertIsNone(num(cells(row)[1]))


class TestMarketFlowPage(unittest.TestCase):
    def test_only_dated_rows_of_the_right_width_are_taken(self):
        """페이지네이션 행은 칸 수가 달라 여기서 걸러져야 한다."""
        html = market_html(
            ["26.08.07"] + ["1,000"] * 10,
            ["맨 위", "다음", "페이지"],
            ["합계"] + ["9,999"] * 10,
        )
        rows = parse_market_page(html)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["date"], "2026-08-07")

    def test_two_digit_year_expands_to_four(self):
        rows = parse_market_page(market_html(["26.01.02"] + ["0"] * 10))
        self.assertEqual(rows[0]["date"], "2026-01-02")

    def test_investor_columns_are_read_by_position(self):
        """헤더가 2행으로 쪼개져 있어 이름이 아니라 위치로 읽는다."""
        rows = parse_market_page(market_html(
            ["26.08.07", "+100", "-200", "+300"] + ["0"] * 7
        ))
        self.assertEqual(rows[0]["individual"], 100.0)
        self.assertEqual(rows[0]["foreign"], -200.0)
        self.assertEqual(rows[0]["institution_total"], 300.0)


class TestStockFlowPage(unittest.TestCase):
    def row(self, **overrides):
        base = ["2026.08.07", "1,422,000", "-73,000", "-4.88%", "5,335,610",
                "+32,908", "-56,357", "370,000,000", "50.83%"]
        for index, value in overrides.items():
            base[int(index)] = value
        return base

    def test_a_full_row_maps_to_named_fields(self):
        rows = parse_stock_page(market_html(self.row()))
        self.assertEqual(rows[0], {
            "date": "2026-08-07",
            "close": 1422000.0,
            "change_pct": -4.88,
            "volume": 5335610.0,
            "institution_net": 32908.0,
            "foreign_net": -56357.0,
            "foreign_shares": 370000000.0,
            "foreign_pct": 50.83,
        })

    def test_short_rows_are_dropped_rather_than_padded(self):
        """칸이 모자란 행을 받아들이면 열이 한 칸씩 밀려 저장된다."""
        self.assertEqual(parse_stock_page(market_html(["2026.08.07", "1,000"])), [])

    def test_header_rows_without_a_date_are_ignored(self):
        self.assertEqual(parse_stock_page(market_html(["날짜", "종가"] * 5)), [])


class TestLendingRows(unittest.TestCase):
    def item(self, **overrides):
        base = {
            "basDt": "20260806", "stckItmsCd": "000660",
            "stckItmsNm": "SK하이닉스", "mrktClsfNm": "코스피",
            "cclStckCnt": "235,830", "rdptStckCnt": "117,916",
            "balnStckCnt": "13,247,603", "balnStckAmt": "19805200000000",
        }
        base.update(overrides)
        return base

    def test_compact_date_becomes_iso(self):
        self.assertEqual(to_row(self.item())["date"], "2026-08-06")

    def test_a_date_that_is_not_eight_digits_is_left_alone(self):
        """모르는 모양을 ISO 인 척 자르면 없는 날짜가 만들어진다."""
        self.assertEqual(to_row(self.item(basDt="2026-08-06"))["date"], "2026-08-06")

    def test_share_counts_stay_integers(self):
        """float 로 두면 `.0` 이 CSV 에 그대로 쓰인다."""
        row = to_row(self.item())
        self.assertEqual(row["balance_shares"], 13247603)
        self.assertIsInstance(row["balance_shares"], int)

    def test_non_integer_values_keep_their_fraction(self):
        self.assertEqual(lending_num("1,234.5"), 1234.5)

    def test_missing_values_are_none(self):
        for blank in (None, "", "-"):
            with self.subTest(repr(blank)):
                self.assertIsNone(lending_num(blank))

    def test_rows_of_only_the_requested_code_pass_verification(self):
        rows = [{"code": "000660"}, {"code": "000660"}]
        self.assertEqual(verify_rows(rows, "000660", "stckItmsCd"), rows)

    def test_an_unfiltered_response_stops_instead_of_being_saved(self):
        """API 가 필터를 무시하고 전 종목을 주면, 저장하는 순간 다른 종목의
        대차잔고를 이 종목 것으로 읽게 된다."""
        rows = [{"code": "000660"}, {"code": "005930"}]
        with self.assertRaises(SystemExit):
            verify_rows(rows, "000660", "stckItmsCd")

    def test_blank_codes_do_not_trip_the_filter_check(self):
        rows = [{"code": "000660"}, {"code": ""}]
        self.assertEqual(verify_rows(rows, "000660", "stckItmsCd"), rows)


class TestKrxRowSelection(unittest.TestCase):
    def test_exact_code_match_only(self):
        rows = [{krx.F_CODE: "005930"}, {krx.F_CODE: "000660"}]
        self.assertEqual(krx.pick(rows, "000660"), {krx.F_CODE: "000660"})

    def test_no_match_returns_none_rather_than_the_first_row(self):
        """임의 행으로 폴백하면 다른 종목의 종가를 이 종목 것으로 쓴다."""
        self.assertIsNone(krx.pick([{krx.F_CODE: "005930"}], "000660"))

    def test_empty_response_is_none(self):
        self.assertIsNone(krx.pick([], "000660"))

    def test_a_changed_schema_stops_instead_of_guessing(self):
        with self.assertRaises(SystemExit):
            krx.pick([{"다른필드": "000660"}], "000660")

    def test_number_parsing_treats_blanks_as_missing(self):
        self.assertEqual(krx.num("1,234"), 1234.0)
        for blank in (None, "", "-"):
            with self.subTest(repr(blank)):
                self.assertIsNone(krx.num(blank))


class TestKrxTradingCalendar(unittest.TestCase):
    """지수 파일 혼자서는 자기 구멍을 볼 수 없다."""

    def write(self, directory, name, dates):
        body = "".join(f"{d},100\n" for d in dates)
        (directory / name).write_text(
            "# schema_version price-series/2.1.0\ndate,close\n" + body,
            encoding="utf-8",
        )

    def test_calendar_is_the_union_of_same_market_stock_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "005930_KS.csv", ["2026-08-06", "2026-08-07"])
            self.write(directory, "000660_KS.csv", ["2026-08-07", "2026-08-10"])
            self.write(directory, "214450_KQ.csv", ["2026-08-11"])
            with patch.object(krx, "PRICE_DIR", directory):
                kospi = krx.trading_calendar("KOSPI")
                kosdaq = krx.trading_calendar("KOSDAQ")
        self.assertEqual(kospi, {"2026-08-06", "2026-08-07", "2026-08-10"})
        self.assertEqual(kosdaq, {"2026-08-11"})

    def test_backfilled_krx_files_do_not_define_the_calendar(self):
        """자기가 메운 파일을 달력의 근거로 쓰면 순환 논증이 된다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "005930_KS.csv", ["2026-08-07"])
            self.write(directory, "005930_KS_krx.csv", ["2026-08-08"])
            with patch.object(krx, "PRICE_DIR", directory):
                days = krx.trading_calendar("KOSPI")
        self.assertEqual(days, {"2026-08-07"})

    def test_comment_header_is_separated_from_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.csv"
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol X / currency KRW\n"
                "date,close\n2026-08-07,100\n",
                encoding="utf-8",
            )
            header, rows = krx.price_rows(path)
        self.assertEqual(len(header), 2)
        self.assertEqual(rows, [{"date": "2026-08-07", "close": "100"}])


if __name__ == "__main__":
    unittest.main()
