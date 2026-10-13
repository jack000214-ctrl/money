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
        self.assertEqual(len(result[1]), 3)

    def test_a_response_that_does_not_name_a_symbol_is_not_blocked(self):
        """대조할 수 없는 것을 틀렸다고 하지 않는다. 막는 것은 확인된 불일치다."""
        payload = chart(DAYS, [100, 101, 102])
        del payload["chart"]["result"][0]["meta"]["symbol"]
        self.assertIsNotNone(fetch(payload)[0])


class TestBrokenAndFutureBars(unittest.TestCase):
    def test_a_close_that_is_not_a_number_stops_the_fetch(self):
        result, message = fetch(chart(DAYS, [100, 102, "not-a-number"]))
        self.assertIsNone(result)
        self.assertIn("2026-10-09", message)

    def test_a_missing_close_is_still_just_a_skipped_day(self):
        """휴장일에도 날짜만 실려 온다. 값이 없는 것과 깨진 것은 다르다."""
        result, _ = fetch(chart(DAYS, [100, None, 102]))
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
        self.assertIsNone(result[1][1]["high"])


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
        self.assertIn("현재 시세로 쓸 수 없다", task.contract_error)

    def test_a_long_holiday_is_not_mistaken_for_a_dead_series(self):
        """추석 연휴와 주말이 겹쳐도 마지막 봉은 열흘 안쪽이다."""
        self.assertEqual(self.inspect(self.series("2026-10-02")).state, "FRESH")

    def test_a_series_ending_in_the_future_is_not_an_observation(self):
        task = self.inspect(self.series("2027-01-06"))
        self.assertEqual(task.state, "STALE")
        self.assertIn("관측이 아니다", task.contract_error)

    def test_a_last_row_whose_close_is_not_a_number_fails_the_contract(self):
        rows = self.series("2026-10-12")
        rows[-1] = (rows[-1][0], "not-a-number")
        task = self.inspect(rows)
        self.assertEqual(task.state, "STALE")
        self.assertIn("숫자가 아니다", task.contract_error)


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
