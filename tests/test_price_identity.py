"""가격 파일은 요청한 종목의, 실제로 관측된, 지금의 시세여야 한다.

이 계층이 없을 때의 구멍 넷.

· **다른 종목의 응답.** 요청 TSLA 에 MSFT 응답이 와도 요청한 이름으로 저장했다.
  헤더에는 TSLA 가 찍히고 그 뒤로는 아무도 구분할 수 없다.
· **깨진 마지막 봉.** 마지막 종가가 숫자가 아니어도 계약을 통과했고, packet 은
  그 행을 버린 전날 값을 현재가로 쓰면서 세션 상태는 버린 날짜의 것을 붙였다.
· **지금 받았다는 것과 지금의 시세라는 것의 혼동.** 신선도는 회수시각으로 잰다.
  1년 전에 끝난 시계열을 오늘 받아도 FRESH 였다.
· **미래 날짜의 봉**도 같은 이유로 FRESH 이고 확정 종가였다.

그리고 보완이 필수를 막던 구멍 하나 — 벤치마크 백필(KRX)이 `required` 라 키가
없는 설치에서는 가격·재무가 멀쩡해도 모든 한국 자산의 판단이 막혔다.
"""

import contextlib
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from cio import refresh
from cio.refresh import RefreshTask, inspect_task, tasks_for_asset
from collectors import krx, price

NOW = dt.datetime(2026, 10, 13, 3, 0, tzinfo=dt.timezone.utc)
DAYS = ["2026-10-07", "2026-10-08", "2026-10-09"]


def stamp(day):
    return int(dt.datetime.fromisoformat(day + "T12:00:00+00:00").timestamp())


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def chart(days, closes, *, symbol="TSLA"):
    return {"chart": {"error": None, "result": [{
        "meta": {"symbol": symbol, "currency": "USD", "dataGranularity": "1d"},
        "timestamp": [stamp(day) for day in days],
        "indicators": {
            "quote": [{"close": closes, "high": [101] * len(days),
                       "low": [99] * len(days), "volume": [1000] * len(days)}],
            "adjclose": [{"adjclose": closes}],
        },
    }]}}


def fetch(payload, symbol="TSLA"):
    with patch("collectors.price.urllib.request.urlopen",
               return_value=FakeResponse(payload)), \
            patch("collectors.price.datetime") as clock, \
            contextlib.redirect_stderr(io.StringIO()) as err:
        clock.now.return_value = NOW
        clock.fromtimestamp.side_effect = dt.datetime.fromtimestamp
        try:
            return price.fetch(symbol, "5y"), err.getvalue()
        except SystemExit:
            return None, err.getvalue()


class TestTheResponseMustBeTheRequestedSymbol(unittest.TestCase):
    def test_another_symbols_quotes_are_not_saved_under_this_name(self):
        result, message = fetch(chart(DAYS, [100, 100, 100], symbol="MSFT"))
        self.assertIsNone(result)
        self.assertIn("MSFT", message)

    def test_the_requested_symbol_is_accepted_whatever_its_case(self):
        result, _ = fetch(chart(DAYS, [100, 101, 102], symbol="tsla"))
        assert result is not None
        self.assertEqual(len(result[1]), 3)

    def test_a_response_that_does_not_name_a_symbol_is_not_blocked(self):
        """대조할 수 없는 것을 틀렸다고 하지 않는다. 막는 것은 확인된 불일치다."""
        payload = chart(DAYS, [100, 101, 102])
        del payload["chart"]["result"][0]["meta"]["symbol"]
        self.assertIsNotNone(fetch(payload)[0])

    def test_the_usd_fx_alias_is_the_same_pair(self):
        """KRW=X 요청에 USDKRW=X 가 오는 것은 같은 쌍이다 (2026-10-15 실측).

        막았더니 달러/원 시계열이 이틀 동안 멈췄고 packet 의 환율이 낡았다.
        """
        result, _ = fetch(chart(DAYS, [1340, 1341, 1346], symbol="USDKRW=X"),
                          symbol="KRW=X")
        assert result is not None
        self.assertEqual(len(result[1]), 3)
        result, _ = fetch(chart(DAYS, [1340, 1341, 1346], symbol="KRW=X"),
                          symbol="USDKRW=X")
        assert result is not None
        self.assertEqual(len(result[1]), 3)

    def test_another_currency_or_a_cross_pair_is_still_refused(self):
        for responded in ("USDJPY=X", "JPY=X", "CNYKRW=X"):
            with self.subTest(responded=responded):
                result, message = fetch(chart(DAYS, [1, 1, 1], symbol=responded),
                                        symbol="KRW=X")
                self.assertIsNone(result)
                self.assertIn(responded, message)


class TestBrokenAndFutureBars(unittest.TestCase):
    def test_a_close_that_is_not_a_number_stops_the_fetch(self):
        result, message = fetch(chart(DAYS, [100, 102, "not-a-number"]))
        self.assertIsNone(result)
        self.assertIn("2026-10-09", message)

    def test_a_missing_close_is_still_just_a_skipped_day(self):
        """휴장일에도 날짜만 실려 온다. 값이 없는 것과 깨진 것은 다르다."""
        result, _ = fetch(chart(DAYS, [100, None, 102]))
        assert result is not None
        self.assertEqual([row["date"] for row in result[1]],
                         ["2026-10-07", "2026-10-09"])

    def test_bars_dated_after_today_are_not_observations(self):
        result, message = fetch(
            chart(["2027-01-04", "2027-01-05", "2027-01-06"], [100, 105, 110]))
        self.assertIsNone(result)
        self.assertIn("미래", message)

    def test_non_numeric_high_and_low_become_absent_not_text(self):
        payload = chart(DAYS, [100, 101, 102])
        payload["chart"]["result"][0]["indicators"]["quote"][0]["high"] = [
            101, "bad", 103]
        result, _ = fetch(payload)
        assert result is not None
        self.assertIsNone(result[1][1]["high"])


class TestAMissingAdjustedCloseIsNotFilledIn(unittest.TestCase):
    """조정종가가 없는 봉에 원가격을 채우면 그 값의 뜻이 사라진다.

    원가격 100→90, 배당 10 인 자산의 총수익률은 0% 다. 응답에서 조정종가만
    빠졌을 때 수집기가 원가격을 대신 넣었고, 파일의 모든 행에 조정종가가 있는
    것처럼 보여 packet 이 −10% 를 `returns_basis: ADJUSTED_CLOSE` 로 실었다.
    """

    def without_adjusted(self, payload, indexes=None):
        result = payload["chart"]["result"][0]
        if indexes is None:
            del result["indicators"]["adjclose"]
        else:
            values = list(result["indicators"]["adjclose"][0]["adjclose"])
            for index in indexes:
                values[index] = None
            result["indicators"]["adjclose"] = [{"adjclose": values}]
        return payload

    def test_a_response_without_the_adjusted_block_leaves_every_bar_blank(self):
        (meta, rows), _ = fetch(self.without_adjusted(chart(DAYS, [100, 95, 90])))  # pyright: ignore[reportGeneralTypeIssues]
        self.assertEqual([row["close"] for row in rows], [100, 95, 90])
        self.assertEqual([row["adjusted_close"] for row in rows], [None] * 3)

    def test_only_the_bar_that_lacks_it_is_blank(self):
        (meta, rows), _ = fetch(  # pyright: ignore[reportGeneralTypeIssues]
            self.without_adjusted(chart(DAYS, [100, 95, 90]), indexes=[1]))
        self.assertEqual([row["adjusted_close"] for row in rows], [100.0, None, 90.0])

    def test_the_blank_survives_the_file_and_is_not_read_back_as_a_price(self):
        (meta, rows), _ = fetch(  # pyright: ignore[reportGeneralTypeIssues]
            self.without_adjusted(chart(DAYS, [100, 95, 90]), indexes=[0]))
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(price, "PRICE_DIR", Path(tmp)):
            path = price.save("TSLA", meta, rows)
            stored = price._existing_rows(path)
        self.assertEqual(stored[0]["adjusted_close"], "")
        self.assertEqual(float(stored[1]["adjusted_close"]), 95.0)

    def test_a_stored_bar_without_an_adjusted_close_is_still_carried(self):
        """그 봉의 종가와 고가·저가는 관측이다. 조정종가가 없다고 이력을 버리지 않는다."""
        header = ("# schema_version price-series/2.1.0\n# symbol TSLA / currency USD\n"
                  "# retrieved_at 2026-10-12T00:00:00+00:00 / source Yahoo Finance chart API\n"
                  "# last_bar 2026-10-09 / session SETTLED\n"
                  "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "TSLA.csv"
            path.write_text(header + "2026-10-01,98,,1,99,97,10\n"
                            "2026-10-07,100,100,1,101,99,10\n"
                            "2026-10-08,95,95,1,101,99,10\n"
                            "2026-10-09,90,90,1,101,99,10\n", encoding="utf-8")
            fresh = [{"date": day, "close": close, "adjusted_close": close,
                      "split_adjustment_factor": 1, "high": 101, "low": 99,
                      "volume": 10}
                     for day, close in zip([*DAYS, "2026-10-12"], [100, 95, 90, 91])]
            merged, _notes, _missing = price.merge_with_existing(path, fresh)
        self.assertEqual(merged[0]["date"], "2026-10-01")
        self.assertEqual(merged[0]["adjusted_close"], "")


class TestReturnsUseOnePriceBasis(unittest.TestCase):
    """수익률은 한 기준의 가격으로만 낸다 — 조정된 값과 안 된 값을 섞지 않는다."""

    HEADER = ("# schema_version price-series/2.1.0\n# symbol TEST / currency USD\n"
              "# retrieved_at 2026-10-13T00:00:00+00:00 / source Yahoo Finance chart API\n"
              "# last_bar {last} / session SETTLED\n"
              "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n")

    def summary(self, bars):
        from cio import context
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "prices").mkdir()
            body = "".join(
                f"{day},{close},{'' if adjusted is None else adjusted},1,"
                f"{close},{close},1\n" for day, close, adjusted in bars)
            (root / "prices" / "TEST.csv").write_text(
                self.HEADER.format(last=bars[-1][0]) + body, encoding="utf-8")
            with patch.object(context, "KNOWLEDGE_DIR", root), \
                    patch.object(context, "ROOT", root):
                return context._price_summary("TEST")[0]

    def bars(self, count, *, blank=()):
        """원가격은 100 에서 90 으로, 조정종가는 내내 90 — 배당 10, 총수익률 0%."""
        start = dt.date(2025, 1, 1)
        rows = []
        for index in range(count):
            day = (start + dt.timedelta(days=index)).isoformat()
            close = 100 if index < count - 1 else 90
            rows.append((day, close, None if index in blank else 90))
        return rows

    def test_a_fully_adjusted_window_is_a_total_return(self):
        summary = self.summary(self.bars(30))
        assert summary is not None
        self.assertEqual(summary["returns_basis"], "ADJUSTED_CLOSE")
        self.assertEqual(summary["returns_pct"]["1m"], 0.0)
        self.assertNotIn("_returns_basis_warning", summary)  # pyright: ignore[reportArgumentType]

    def test_one_blank_bar_in_the_window_moves_every_return_to_raw_prices(self):
        """기준 봉에만 조정종가가 없다. 그 봉만 원가격으로 읽으면 100→90(조정)
        이라는, 어느 기준도 아닌 −10% 가 조정종가 기준으로 실린다."""
        summary = self.summary(self.bars(30, blank={8}))
        assert summary is not None
        self.assertEqual(summary["returns_basis"], "RAW_CLOSE_FALLBACK")
        self.assertEqual(summary["returns_pct"]["1m"], -10.0)
        self.assertIn("가격 수익률", summary["_returns_basis_warning"])
        # 마지막 세션 수익률도 같은 기준이다 (원가격 100 → 90).
        self.assertEqual(
            summary["settled_session_returns"]["windows"]["1"]["return_pct"], -10.0)

    def test_a_blank_bar_older_than_the_longest_window_changes_nothing(self):
        summary = self.summary(self.bars(300, blank={0, 1, 2}))
        assert summary is not None
        self.assertEqual(summary["returns_basis"], "ADJUSTED_CLOSE")
        self.assertEqual(summary["returns_pct"]["12m"], 0.0)


class PriceContract(unittest.TestCase):
    def inspect(self, rows, *, retrieved="2026-10-13T02:00:00+00:00", last_bar=None):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "knowledge" / "prices" / "TSLA.csv"
            path.parent.mkdir(parents=True)
            body = "".join(f"{day},{close},{close},1.0,101,99,1000\n"
                           for day, close in rows)
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TSLA / currency USD\n"
                f"# retrieved_at {retrieved} / source Yahoo Finance chart API\n"
                f"# last_bar {last_bar or rows[-1][0]} / session SETTLED\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
                + body, encoding="utf-8")
            task = RefreshTask(name="price:TSLA", capability="PRICE",
                               artifact="knowledge/prices/TSLA.csv",
                               max_age_hours=12, command=None)
            with patch.object(refresh, "ROOT", root):
                return inspect_task(task, now=NOW)

    @staticmethod
    def series(last_day, count=8, close=100):
        day = dt.date.fromisoformat(last_day)
        days = []
        while len(days) < count:
            if day.weekday() < 5:
                days.append(day.isoformat())
            day -= dt.timedelta(days=1)
        return [(value, close) for value in reversed(days)]


class TestRetrievedNowIsNotObservedNow(PriceContract):
    def test_a_current_series_is_fresh(self):
        self.assertEqual(self.inspect(self.series("2026-10-12")).state, "FRESH")

    def test_a_series_that_ended_long_ago_is_not_fresh_however_recently_fetched(self):
        task = self.inspect(self.series("2025-09-29"))
        self.assertEqual(task.state, "STALE")
        self.assertIn("현재 시세로 쓸 수 없다", task.contract_error)  # pyright: ignore[reportArgumentType]

    def test_a_long_holiday_is_not_mistaken_for_a_dead_series(self):
        """추석 연휴와 주말이 겹쳐도 마지막 봉은 열흘 안쪽이다."""
        self.assertEqual(self.inspect(self.series("2026-10-02")).state, "FRESH")

    def test_a_series_ending_in_the_future_is_not_an_observation(self):
        task = self.inspect(self.series("2027-01-06"))
        self.assertEqual(task.state, "STALE")
        self.assertIn("관측이 아니다", task.contract_error)  # pyright: ignore[reportArgumentType]

    def test_a_last_row_whose_close_is_not_a_number_fails_the_contract(self):
        rows = self.series("2026-10-12")
        rows[-1] = (rows[-1][0], "not-a-number")  # pyright: ignore[reportArgumentType, reportCallIssue]
        task = self.inspect(rows)
        self.assertEqual(task.state, "STALE")
        self.assertIn("숫자가 아니다", task.contract_error)  # pyright: ignore[reportArgumentType]


KR_STOCK = {
    "asset_id": "214450", "market": "KR", "asset_type": "STOCK",
    "exchange": "KOSDAQ", "currency": "KRW", "price_symbol": "214450.KQ",
    "benchmarks": ["^KQ11", "^KS11"], "filing_source": "DART",
    "optional_collectors": [], "peers": [],
}


class TestBackfillIsARepairNotARequirement(unittest.TestCase):
    def plan(self, root):
        with patch.object(refresh, "ROOT", root):
            return {task.name: task for task in tasks_for_asset(KR_STOCK)}

    def test_the_index_backfill_cannot_block_a_judgment(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = self.plan(Path(tmp))
        self.assertFalse(plan["benchmark_backfill:^KQ11"].required)
        self.assertFalse(plan["benchmark_backfill:^KS11"].required)
        # 가격 자체는 여전히 필수다.
        self.assertTrue(plan["price:^KQ11"].required)

    def test_events_do_not_wait_for_a_marker_that_was_never_written(self):
        marker = "knowledge/prices/_backfill/KQ11.json"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            before = self.plan(root)["events:214450"].dependencies
            (root / marker).parent.mkdir(parents=True)
            (root / marker).write_text("{}", encoding="utf-8")
            after = self.plan(root)["events:214450"].dependencies
        self.assertNotIn(marker, before)
        self.assertIn(marker, after)

    def run_backfill(self, prices, *, with_gap):
        index = prices / krx.BENCHMARKS["KOSPI"]["yahoo"]
        stock = prices / "005930_KS.csv"
        head = ("# schema_version price-series/2.1.0\n# symbol X / currency KRW\n"
                "# retrieved_at 2026-10-13T00:00:00+00:00 / source Yahoo Finance chart API\n"
                "# last_bar 2026-10-12 / session SETTLED\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n")
        index.write_text(head + "2026-10-08,100,100,1.0,101,99,1\n"
                         "2026-10-12,101,101,1.0,102,100,1\n", encoding="utf-8")
        if with_gap:
            # 종목 파일에는 10-09 가 있다 — 지수 쪽 구멍이 그래서 보인다.
            stock.write_text(head + "2026-10-08,50,50,1.0,51,49,1\n"
                             "2026-10-09,51,51,1.0,52,50,1\n"
                             "2026-10-12,52,52,1.0,53,51,1\n", encoding="utf-8")
        err = io.StringIO()
        with patch.object(krx, "PRICE_DIR", prices), \
                patch.object(krx, "ROOT", prices.parent.parent), \
                patch.dict(krx.os.environ, {}, clear=True), \
                patch("sys.argv", ["collectors.krx", "backfill", "KOSPI"]), \
                contextlib.redirect_stderr(err), \
                contextlib.redirect_stdout(io.StringIO()):
            try:
                krx.main()
                return 0, err.getvalue()
            except SystemExit as exc:
                return exc.code, err.getvalue()

    def test_no_gap_is_recorded_without_asking_for_a_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            prices = Path(tmp) / "knowledge" / "prices"
            prices.mkdir(parents=True)
            code, _ = self.run_backfill(prices, with_gap=False)
            marker = json.loads(
                (prices / "_backfill" / "KS11.json").read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual((marker["filled"], marker["unfilled"]), ([], []))

    def test_a_real_gap_still_needs_the_key_and_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            prices = Path(tmp) / "knowledge" / "prices"
            prices.mkdir(parents=True)
            code, message = self.run_backfill(prices, with_gap=True)
            written = (prices / "_backfill" / "KS11.json").exists()
        self.assertEqual(code, 1)
        self.assertIn("KRX_API_KEY", message)
        self.assertFalse(written, "메우지 못했는데 '확인했다'는 표식을 남기면 안 된다")


if __name__ == "__main__":
    unittest.main()
