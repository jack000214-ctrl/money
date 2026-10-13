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

import contextlib
import io
import json
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

    def test_calendar_is_one_for_both_korean_markets(self):
        """유가증권·코스닥은 휴장일이 같다. 한쪽에만 남은 날짜가 다른 쪽의 구멍을 드러낸다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "005930_KS.csv", ["2026-08-06", "2026-08-07"])
            self.write(directory, "000660_KS.csv", ["2026-08-07", "2026-08-10"])
            self.write(directory, "214450_KQ.csv", ["2026-08-11"])
            with patch.object(krx, "PRICE_DIR", directory):
                days = krx.trading_calendar()
        self.assertEqual(
            days, {"2026-08-06", "2026-08-07", "2026-08-10", "2026-08-11"})

    def test_an_index_file_reveals_a_day_every_stock_file_lost(self):
        """2026-10-13: 코스닥 종목과 코스닥 지수가 10-12 를 함께 잃었고 코스피 지수만 갖고 있었다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "214450_KQ.csv", ["2026-10-08", "2026-10-13"])
            self.write(directory, "KQ11.csv", ["2026-10-08", "2026-10-13"])
            self.write(directory, "KS11.csv", ["2026-10-08", "2026-10-12", "2026-10-13"])
            with patch.object(krx, "PRICE_DIR", directory):
                days = krx.trading_calendar()
        self.assertIn("2026-10-12", days)

    def test_a_recorded_missing_session_is_part_of_the_calendar(self):
        """모든 파일이 같은 날을 잃어도, 봉이 있었다는 기록이 그날을 달력에 남긴다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "214450_KQ.csv").write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol 214450.KQ / currency KRW\n"
                "# retrieved_at 2026-10-13T00:37:14+00:00 / source Yahoo Finance chart API\n"
                "# last_bar 2026-10-13 / session UNSETTLED\n"
                "# missing_sessions 2026-10-12 / 봉이 저장됐다가 응답에서 사라졌다\n"
                "date,close\n2026-10-08,302500\n2026-10-13,278000\n",
                encoding="utf-8",
            )
            with patch.object(krx, "PRICE_DIR", directory):
                days = krx.trading_calendar()
        self.assertEqual(days, {"2026-10-08", "2026-10-12", "2026-10-13"})

    def test_backfilled_krx_files_do_not_define_the_calendar(self):
        """자기가 메운 파일을 달력의 근거로 쓰면 순환 논증이 된다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self.write(directory, "005930_KS.csv", ["2026-08-07"])
            self.write(directory, "005930_KS_krx.csv", ["2026-08-08"])
            with patch.object(krx, "PRICE_DIR", directory):
                days = krx.trading_calendar()
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


PRICE_HEADER = (
    "# schema_version price-series/2.1.0\n"
    "# symbol {symbol} / currency KRW\n"
    "# retrieved_at 2026-10-13T00:37:14+00:00 / source Yahoo Finance chart API\n"
    "# last_bar 2026-10-13 / session UNSETTLED\n"
    "# last_bar_timestamp 1791849600 / source Yahoo timestamp\n"
    "{extra}"
    "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
)


class TestStockSeriesIsFilledFromTheExchange(unittest.TestCase):
    """종목 시계열의 구멍도 지수와 같은 방법으로 메운다.

    2026-10-13 수집에서 파마리서치의 10-12 봉이 사라졌다. 그날 저장돼 있던 값은
    장중의 300,000원이었고 거래소 종가는 297,000원이다 — 사라진 봉을 그대로
    되살렸다면 틀린 값이 종가로 굳었을 것이다. 원문으로 메운다.
    """

    def _files(self, directory, *, extra="", stock_rows=None):
        (directory / "214450_KQ.csv").write_text(
            PRICE_HEADER.format(symbol="214450.KQ", extra=extra)
            + "".join(stock_rows or [
                "2026-10-08,302500.0,302500.0,1,309000.0,295500.0,125898\n",
                "2026-10-13,278000.0,278000.0,1,289500.0,269500.0,30296\n",
            ]),
            encoding="utf-8",
        )
        (directory / "KS11.csv").write_text(
            PRICE_HEADER.format(symbol="^KS11", extra="")
            + "2026-10-08,6625.93,6625.93,1,6810.83,6625.93,302500\n"
            + "2026-10-12,6384.24,6384.24,1,6589.69,6368.49,340600\n"
            + "2026-10-13,6098.41,6098.41,1,6198.46,6088.6,52778\n",
            encoding="utf-8",
        )

    def _run(self, directory, bars, **kwargs):
        calls = []

        def fetch_bar(date):
            calls.append(date)
            return bars.get(date)

        with patch.object(krx, "PRICE_DIR", directory), \
                patch.object(krx, "ROOT", directory), \
                patch.object(krx.time, "sleep"), \
                contextlib.redirect_stdout(io.StringIO()):
            krx.backfill_series(
                directory / "214450_KQ.csv", symbol="214450.KQ",
                endpoint="sto/ksq_bydd_trd", fetch_bar=fetch_bar,
                limit=kwargs.get("limit", 20))
        return calls

    def test_a_failed_write_leaves_the_stored_history_untouched(self):
        """메운 결과를 제자리에서 열어 썼다. 쓰다 죽으면 잘린 파일만 남는다.

        이 파일에는 야후가 더는 주지 않는 과거 봉이 있어 다시 받을 수 없다.
        """
        class Failing:
            def __init__(self, *args, **kwargs):
                pass

            def writeheader(self):
                pass

            def writerows(self, rows):
                raise ValueError("행이 FIELDS 와 어긋난다")

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory, extra="# missing_sessions 2026-10-12 / 사라졌다\n")
            before = (directory / "214450_KQ.csv").read_bytes()
            with patch.object(krx.csv, "DictWriter", Failing), \
                    self.assertRaises(ValueError):
                self._run(directory, {"2026-10-12": {
                    "close": 297000.0, "high": 306500.0, "low": 296500.0,
                    "volume": 69710.0}})
            self.assertEqual((directory / "214450_KQ.csv").read_bytes(), before)
            self.assertEqual(sorted(p.name for p in directory.iterdir() if p.is_file()),
                             ["214450_KQ.csv", "KS11.csv"])

    def test_the_lost_session_is_filled_with_the_exchange_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory, extra="# missing_sessions 2026-10-12 / 사라졌다\n")
            calls = self._run(directory, {"2026-10-12": {
                "close": 297000.0, "high": 306500.0, "low": 296500.0,
                "volume": 69710.0}})
            header, rows = krx.price_rows(directory / "214450_KQ.csv")
            marker = json.loads(
                (directory / "_backfill" / "214450_KQ.json").read_text(encoding="utf-8"))

        self.assertEqual(calls, ["2026-10-12"])
        filled = next(r for r in rows if r["date"] == "2026-10-12")
        self.assertEqual(float(filled["close"]), 297000.0)
        self.assertEqual(float(filled["adjusted_close"]), 297000.0)
        self.assertEqual(float(filled["volume"]), 69710.0)
        self.assertEqual([r["date"] for r in rows],
                         ["2026-10-08", "2026-10-12", "2026-10-13"])
        self.assertEqual(marker["filled"], ["2026-10-12"])
        self.assertEqual(marker["target"], "214450_KQ.csv")
        self.assertTrue(header[3].startswith("# last_bar "),
                        "머리말 네 줄의 자리가 바뀌었다")
        self.assertTrue(any(line.startswith("# backfilled 2026-10-12 ") for line in header))
        self.assertFalse(any(line.startswith("# missing_sessions") for line in header),
                         "메웠는데 사라진 세션 기록이 남았다")

    def test_a_day_the_exchange_does_not_have_is_left_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory)
            self._run(directory, {})
            _, rows = krx.price_rows(directory / "214450_KQ.csv")
            marker = json.loads(
                (directory / "_backfill" / "214450_KQ.json").read_text(encoding="utf-8"))

        self.assertEqual([r["date"] for r in rows], ["2026-10-08", "2026-10-13"])
        self.assertEqual(marker["filled"], [])
        self.assertEqual(marker["unfilled"], ["2026-10-12"])

    def test_exchange_quotes_are_moved_onto_the_stored_basis(self):
        """앞뒤 봉이 같은 분할계수·배당 조정을 말하면 원문에 그 기준을 입힌다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory, stock_rows=[
                "2026-10-08,1000.0,950.0,0.5,1010.0,990.0,200\n",
                "2026-10-13,1100.0,1045.0,0.5,1110.0,1090.0,300\n",
            ])
            self._run(directory, {"2026-10-12": {
                "close": 2100.0, "high": 2120.0, "low": 2080.0, "volume": 400.0}})
            _, rows = krx.price_rows(directory / "214450_KQ.csv")

        filled = next(r for r in rows if r["date"] == "2026-10-12")
        self.assertEqual(float(filled["close"]), 1050.0)
        self.assertAlmostEqual(float(filled["adjusted_close"]), 997.5)
        self.assertEqual(float(filled["split_adjustment_factor"]), 0.5)
        self.assertEqual(float(filled["high"]), 1060.0)
        self.assertEqual(float(filled["volume"]), 800.0)

    def test_two_holes_each_use_their_own_neighbours(self):
        """먼저 메운 봉이 다음 구멍의 '직전 봉'이 되면 안 된다.

        실제로 그랬다. 2025-09-19 를 먼저 메운 뒤 그 봉(배당 조정 전 기준)을
        2026-10-12 의 직전 봉으로 읽어, 멀쩡한 구멍을 기준 불일치로 거절했다.
        """
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory, stock_rows=[
                "2025-09-18,1000.0,980.0,1,1010.0,990.0,200\n",
                "2025-09-22,1020.0,999.6,1,1030.0,1010.0,200\n",
                "2026-10-08,302500.0,302500.0,1,309000.0,295500.0,125898\n",
                "2026-10-13,278000.0,278000.0,1,289500.0,269500.0,30296\n",
            ])
            ks = directory / "KS11.csv"
            ks.write_text(
                ks.read_text(encoding="utf-8").replace(
                    "2026-10-08,", "2025-09-19,3445.24,3445.24,1,3467.89,3434.61,1\n"
                    "2026-10-08,", 1),
                encoding="utf-8")
            self._run(directory, {
                "2025-09-19": {"close": 1010.0, "high": 1015.0, "low": 1005.0,
                               "volume": 100.0},
                "2026-10-12": {"close": 297000.0, "high": 306500.0, "low": 296500.0,
                               "volume": 69710.0},
            })
            _, rows = krx.price_rows(directory / "214450_KQ.csv")
            marker = json.loads(
                (directory / "_backfill" / "214450_KQ.json").read_text(encoding="utf-8"))

        self.assertEqual(marker["filled"], ["2025-09-19", "2026-10-12"])
        by_date = {r["date"]: r for r in rows}
        self.assertAlmostEqual(float(by_date["2025-09-19"]["adjusted_close"]), 989.8)
        self.assertEqual(float(by_date["2026-10-12"]["adjusted_close"]), 297000.0)

    def test_a_basis_change_around_the_hole_is_not_guessed(self):
        """배당락·분할 경계가 빈 날의 앞인지 뒤인지 모르면 메우지 않는다."""
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            self._files(directory, stock_rows=[
                "2026-10-08,1000.0,950.0,1,1010.0,990.0,200\n",
                "2026-10-13,1100.0,1100.0,1,1110.0,1090.0,300\n",
            ])
            self._run(directory, {"2026-10-12": {
                "close": 1050.0, "high": 1060.0, "low": 1040.0, "volume": 400.0}})
            _, rows = krx.price_rows(directory / "214450_KQ.csv")
            marker = json.loads(
                (directory / "_backfill" / "214450_KQ.json").read_text(encoding="utf-8"))

        self.assertNotIn("2026-10-12", [r["date"] for r in rows])
        self.assertEqual(marker["unfilled"], ["2026-10-12"])
        self.assertIn("2026-10-12", marker["unfilled_reasons"])


if __name__ == "__main__":
    unittest.main()
