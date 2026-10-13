"""사후 평가의 경계 — 판단일, 체결일, 종료일, 그리고 지수의 종료일.

채점은 뒤늦게 돌아보는 계산이라 오차를 아무도 눈치채지 못한 채 track record 에
남는다. 여기서 고정하는 네 가지는 전부 **어느 날의 값을 쓰는가**가 하루 어긋나서
생긴 것이다.

· 문턱의 분할 기준은 판단일 봉의 것이다. 사후 경로의 첫 봉(판단 다음 거래일)의
  계수를 쓰면, 그날 분할이 있을 때 닿지 않은 손절이 닿았다고 적힌다.
· 기다렸다 산 포지션은 체결된 날부터 배당을 받는다. 판단일의 배당 조정계수를
  진입 원가에 쓰면 체결 전에 지급된 배당이 수익으로 들어간다.
· 완료는 날짜가 아니라 확정 종가로 정한다. 종료일 장중 봉으로 승패와 Brier 를
  굳히지 않는다.
· 초과수익은 종목과 지수가 같은 구간일 때만 뺀다.
· 종료일 뒤의 봉이 있다는 것은 종료일의 관측이 있다는 뜻이 아니다. 종료일의
  봉이 빠진 채로는 완료가 아니다.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from calculators.contracts import ContractError
from cio import memory
from cio.decision import context_id_for, normalize_decision
from cio.memory import _fixed_horizon_returns, _settled_through, evaluate_assessment
from test_cio_core import valid_context, valid_decision


def bar(day, close, *, factor=1.0, adjusted=None, low=None, high=None):
    return {
        "date": day, "close": close,
        "adjusted_close": close if adjusted is None else adjusted,
        "has_adjusted_close": True, "split_adjustment_factor": factor,
        "low": close if low is None else low,
        "high": close if high is None else high,
    }


def evaluate(rows, *, entry=95.0, session=None, known_sessions=None):
    """질문가 100, 손절 80 인 판단을 2026-01-02 에 냈다고 보고 채점한다."""
    context = valid_context()
    context["request"]["as_of"] = "2026-01-02T10:00:00-05:00"
    context["created_at"] = context["request"]["as_of"]
    context["context_id"] = context_id_for(context)
    decision = valid_decision()
    decision["context_id"] = context["context_id"]
    decision["created_at"] = "2026-01-02T10:05:00-05:00"
    decision["price_plan"]["preferred_entry"] = entry
    asset = {"asset_id": "TSLA", "price_symbol": "SYNTHETIC", "market": "US",
             "currency": "USD", "benchmarks": []}
    with patch("cio.memory.get_asset", return_value=asset), \
            patch("cio.memory.find_context", return_value=(context, None)), \
            patch("cio.memory._price_session", return_value=session):
        return evaluate_assessment(
            normalize_decision(decision), rows=rows, known_sessions=known_sessions)


class TestThresholdsUseTheDecisionDaySplitBasis(unittest.TestCase):
    """판단 다음 거래일에 2:1 분할. 손절 80 의 비교값은 40, 진입 95 는 47.5 다."""

    ROWS = [
        bar("2026-01-02", 50, factor=0.5),
        bar("2026-01-05", 50, low=49, high=51),
        bar("2026-01-30", 55, low=54, high=56),
        bar("2026-02-03", 55, low=54, high=56),
    ]

    def test_a_stop_that_was_never_touched_is_not_recorded(self):
        result = evaluate(self.ROWS)
        self.assertEqual(result["price"]["adjustment_factor_at_start"], 0.5)
        self.assertIsNone(result["triggers"]["stop"])

    def test_an_entry_that_was_never_reached_is_not_a_fill(self):
        waited = evaluate(self.ROWS)["counterfactual"]["waited_for_entry"]
        self.assertFalse(waited["filled"])
        self.assertIsNone(waited["return_to_end_pct"])

    def test_a_real_touch_after_the_split_is_still_seen(self):
        rows = [*self.ROWS[:1], bar("2026-01-05", 50, low=39, high=51),
                *self.ROWS[2:]]
        stop = evaluate(rows)["triggers"]["stop"]
        self.assertEqual(stop["date"], "2026-01-05")
        self.assertEqual(stop["split_adjusted_level"], 40.0)


class TestDividendsBelongToWhoeverHeldTheShares(unittest.TestCase):
    def test_a_dividend_paid_before_the_fill_is_not_the_waiters_return(self):
        """판단가 100, 배당락 뒤 90 에 체결, 이후 90 유지 — 수익은 0% 다."""
        rows = [
            bar("2026-01-02", 100, adjusted=90),
            bar("2026-01-05", 90),
            bar("2026-01-30", 90),
            bar("2026-02-03", 90),
        ]
        result = evaluate(rows, entry=90)
        waited = result["counterfactual"]["waited_for_entry"]
        self.assertTrue(waited["filled"])
        self.assertAlmostEqual(waited["return_to_end_pct"], 0.0, places=6)
        # 질문가에 바로 산 쪽은 배당을 받았다. 가격은 -10% 지만 총수익은 0% 다.
        self.assertAlmostEqual(result["price"]["return_to_end_pct"], 0.0, places=6)

    def test_a_dividend_paid_after_the_fill_is_the_waiters_return(self):
        rows = [
            bar("2026-01-02", 100, adjusted=90),
            bar("2026-01-05", 95, adjusted=85.5),     # 체결. 아직 배당 전이다
            bar("2026-01-30", 85.5),                  # 배당락 뒤
            bar("2026-02-03", 85.5),
        ]
        waited = evaluate(rows, entry=95)["counterfactual"]["waited_for_entry"]
        self.assertAlmostEqual(waited["return_to_end_pct"], 0.0, places=6)


class TestEvaluationUsesOnePriceBasis(unittest.TestCase):
    """판단일 봉에만 조정종가가 없다. 원가격 100, 배당락 뒤 90, 조정종가 90.

    예전에는 봉마다 있는 쪽을 골랐다. 원가는 원가격 100(배당 미반영), 기간 말은
    조정종가 90 — 한 수익률 안에서 두 기준이 섞였고 표시만 `CLOSE_FALLBACK` 이었다.
    """

    def rows(self, *, decision_day_adjusted):
        first = bar("2026-01-02", 100, adjusted=90)
        first["has_adjusted_close"] = decision_day_adjusted
        if not decision_day_adjusted:
            first["adjusted_close"] = 100     # `_price_rows` 가 비어 있을 때 넣는 값
        return [first, bar("2026-01-05", 90), bar("2026-01-30", 90),
                bar("2026-02-02", 90)]

    def test_with_every_adjusted_close_the_dividend_is_part_of_the_return(self):
        price = evaluate(self.rows(decision_day_adjusted=True))["price"]
        self.assertEqual(price["return_basis"], "ADJUSTED_CLOSE_ASSET_CURRENCY")
        self.assertAlmostEqual(price["return_to_end_pct"], 0.0, places=6)
        self.assertNotIn("_return_basis_note", price)

    def test_a_blank_on_the_decision_day_makes_the_whole_window_a_price_return(self):
        price = evaluate(self.rows(decision_day_adjusted=False))["price"]
        self.assertEqual(price["return_basis"], "CLOSE_FALLBACK_ASSET_CURRENCY")
        self.assertAlmostEqual(price["return_to_end_pct"], -10.0, places=6)
        self.assertEqual(price["adjustment_factor_at_start"], 1.0)
        self.assertIn("가격 수익률", price["_return_basis_note"])

    def test_a_blank_before_the_decision_day_does_not_change_the_basis(self):
        older = bar("2025-12-31", 100)
        older["has_adjusted_close"] = False
        price = evaluate([older, *self.rows(decision_day_adjusted=True)])["price"]
        self.assertEqual(price["return_basis"], "ADJUSTED_CLOSE_ASSET_CURRENCY")
        self.assertAlmostEqual(price["return_to_end_pct"], 0.0, places=6)


class TestCompletionNeedsASettledClose(unittest.TestCase):
    ROWS = [bar("2026-01-02", 100), bar("2026-01-05", 101), bar("2026-02-02", 120)]

    def test_an_intraday_bar_on_the_target_date_does_not_complete_the_window(self):
        result = evaluate(
            self.ROWS, session={"last_bar": "2026-02-02", "session": "UNSETTLED"})
        self.assertFalse(result["window"]["complete"])
        self.assertEqual(result["window"]["settled_through"], "2026-01-05")
        self.assertIsNone(result["calibration"]["brier_score"])
        self.assertIsNone(result["thresholds"]["win_observed"])
        self.assertFalse(result["fixed_horizons"]["months"]["m1"]["complete"])

    def test_an_unknown_session_state_does_not_complete_it_either(self):
        result = evaluate(
            self.ROWS, session={"last_bar": "2026-02-02", "session": "UNKNOWN"})
        self.assertFalse(result["window"]["complete"])

    def test_a_settled_close_on_the_target_date_completes_it(self):
        result = evaluate(
            self.ROWS, session={"last_bar": "2026-02-02", "session": "SETTLED"})
        self.assertTrue(result["window"]["complete"])
        self.assertIsNotNone(result["calibration"]["brier_score"])
        self.assertTrue(result["fixed_horizons"]["months"]["m1"]["complete"])

    def test_an_intraday_bar_after_the_target_does_not_reopen_a_closed_window(self):
        rows = [*self.ROWS, bar("2026-02-03", 121)]
        result = evaluate(
            rows, session={"last_bar": "2026-02-03", "session": "UNSETTLED"})
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["price"]["end"], 120)

    def test_settled_through(self):
        rows = [{"date": "2026-01-05"}, {"date": "2026-01-06"}]
        self.assertEqual(_settled_through(rows, None), "2026-01-06")
        self.assertEqual(_settled_through(
            rows, {"last_bar": "2026-01-06", "session": "SETTLED"}), "2026-01-06")
        self.assertEqual(_settled_through(
            rows, {"last_bar": "2026-01-06", "session": "UNSETTLED"}), "2026-01-05")
        self.assertIsNone(_settled_through(
            rows[:1], {"last_bar": "2026-01-05", "session": "UNSETTLED"}))
        self.assertIsNone(_settled_through([], None))


class TestCompletionNeedsTheEndSessionItself(unittest.TestCase):
    """평가 종료일은 2/2. 그날의 봉이 없고 2/3 봉만 있다.

    예전에는 확정 종가가 종료일을 지났는지만 봤다. 그래서 1/30 의 가격을 기간 말
    가격으로 굳혀 승패와 Brier 를 확정했고, 지수는 2/2 가격을 써서 서로 다른
    구간의 수익률을 뺐다. 뒤 날짜의 봉은 종료일 관측을 보장하지 않는다.
    """

    ROWS = [bar("2026-01-02", 100), bar("2026-01-05", 101),
            bar("2026-01-30", 105), bar("2026-02-03", 130)]
    SETTLED = {"last_bar": "2026-02-03", "session": "SETTLED"}

    def test_a_known_session_missing_on_the_target_date_keeps_the_window_open(self):
        result = evaluate(self.ROWS, session=self.SETTLED,
                          known_sessions={"2026-02-02"})
        window = result["window"]
        self.assertFalse(window["complete"])
        self.assertEqual(window["end_observation_date"], "2026-01-30")
        self.assertEqual(window["end_observation_gaps"], ["2026-02-02"])
        self.assertIn("2026-02-02", window["_end_gap_note"])
        self.assertIsNone(result["calibration"]["brier_score"])
        self.assertIsNone(result["calibration"]["return_error_pct_points"])
        self.assertIsNone(result["thresholds"]["win_observed"])
        month = result["fixed_horizons"]["months"]["m1"]
        self.assertFalse(month["complete"])
        self.assertEqual(month["missing_sessions_before_target"], ["2026-02-02"])

    def test_a_target_date_with_no_session_still_ends_on_the_last_bar_before_it(self):
        """종료일이 휴장일이면 그 전 마지막 봉이 기간 말이다. 그것은 구멍이 아니다."""
        result = evaluate(self.ROWS, session=self.SETTLED, known_sessions=set())
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["window"]["end_observation_gaps"], [])
        self.assertNotIn("_end_gap_note", result["window"])
        self.assertEqual(result["price"]["end"], 105)
        self.assertIsNotNone(result["calibration"]["brier_score"])

    def test_a_gap_earlier_in_the_window_does_not_block_the_end(self):
        """기간 말 봉보다 앞선 구멍은 기간 말 가격을 바꾸지 않는다."""
        result = evaluate(self.ROWS, session=self.SETTLED,
                          known_sessions={"2026-01-15", "2026-02-03"})
        self.assertTrue(result["window"]["complete"])

    def test_a_session_the_series_has_not_reached_yet_is_not_a_hole(self):
        """평가가 진행 중이고 지수 파일만 하루 먼저 수집됐다. 종목의 마지막 봉
        뒤의 날은 아직 받지 않은 것이지 사라진 것이 아니다."""
        rows = self.ROWS[:3]                      # 1/30 까지. 종료일은 2/2 다.
        result = evaluate(rows, session={"last_bar": "2026-01-30", "session": "SETTLED"},
                          known_sessions={"2026-02-02"})
        self.assertFalse(result["window"]["complete"])
        self.assertEqual(result["window"]["end_observation_gaps"], [])
        self.assertNotIn("_end_gap_note", result["window"])
        self.assertNotIn("missing_sessions_before_target",
                         result["fixed_horizons"]["months"]["m1"])

    def test_once_the_end_session_is_present_the_window_closes(self):
        rows = [*self.ROWS[:3], bar("2026-02-02", 120), self.ROWS[3]]
        result = evaluate(rows, session=self.SETTLED,
                          known_sessions={"2026-02-02"})
        self.assertTrue(result["window"]["complete"])
        self.assertEqual(result["price"]["end"], 120)
        self.assertEqual(result["window"]["end_observation_date"], "2026-02-02")


class TestKnownSessions(unittest.TestCase):
    """달력의 근거 — 자기 파일의 기록과 같은 거래소 지수, 그리고 거래소의 확인."""

    HEADER = ("# schema_version price-series/2.1.0\n# symbol {symbol} / currency KRW\n"
              "# retrieved_at 2026-02-04T00:00:00+00:00 / source Yahoo Finance chart API\n"
              "# last_bar {last} / session SETTLED\n{extra}"
              "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "prices" / "_backfill").mkdir(parents=True)
        patcher = patch.object(memory, "KNOWLEDGE_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)

    def series(self, symbol, days, *, missing=()):
        extra = (f"# missing_sessions {','.join(missing)} / 봉이 저장됐다가 사라졌다\n"
                 if missing else "")
        body = "".join(f"{day},100,100,1,100,100,10\n" for day in days)
        (self.root / "prices" / f"{memory.safe_symbol(symbol)}.csv").write_text(
            self.HEADER.format(symbol=symbol, last=days[-1], extra=extra) + body,
            encoding="utf-8")

    def marker(self, symbol, unfilled, reasons=None):
        name = memory.safe_symbol(symbol)
        (self.root / "prices" / "_backfill" / f"{name}.json").write_text(json.dumps({
            "schema_version": "price-backfill/1.0.0", "symbol": symbol,
            "target": f"{name}.csv", "filled": [], "unfilled": unfilled,
            **({"unfilled_reasons": reasons} if reasons else {})}), encoding="utf-8")

    def test_the_files_own_record_of_a_vanished_bar_is_a_known_session(self):
        self.series("214450.KQ", ["2026-01-30", "2026-02-03"], missing=["2026-02-02"])
        self.assertEqual(memory._known_sessions("214450.KQ", "KR"), {"2026-02-02"})

    def test_an_index_on_the_same_calendar_reveals_a_hole_the_file_cannot_see(self):
        self.series("214450.KQ", ["2026-01-30", "2026-02-03"])
        self.series("^KS11", ["2026-01-30", "2026-02-02", "2026-02-03"])
        self.assertIn("2026-02-02", memory._known_sessions("214450.KQ", "KR"))

    def test_an_index_is_checked_against_its_sibling_but_not_itself(self):
        self.series("^KQ11", ["2026-01-30", "2026-02-03"])
        self.series("^KS11", ["2026-01-30", "2026-02-02", "2026-02-03"])
        self.assertIn("2026-02-02", memory._known_sessions("^KQ11"))

    def test_a_foreign_index_has_no_calendar_to_be_checked_against(self):
        """휴장일이 다른 달력과 대조하면 없는 구멍이 생긴다."""
        self.series("^HSI", ["2026-01-30", "2026-02-03"])
        self.series("^KS11", ["2026-01-30", "2026-02-02", "2026-02-03"])
        self.assertEqual(memory._known_sessions("^HSI"), set())

    def test_a_day_the_exchange_confirmed_as_absent_is_not_a_hole(self):
        """거래정지·휴장으로 거래소 원문에도 없는 날은 그 전 봉이 마지막 관측이다."""
        self.series("214450.KQ", ["2026-01-30", "2026-02-03"])
        self.series("^KS11", ["2026-01-30", "2026-02-02", "2026-02-03"])
        self.marker("214450.KQ", ["2026-02-02"])
        self.assertNotIn("2026-02-02", memory._known_sessions("214450.KQ", "KR"))

    def test_a_day_the_exchange_has_but_could_not_be_placed_is_still_a_hole(self):
        self.series("214450.KQ", ["2026-01-30", "2026-02-03"])
        self.series("^KS11", ["2026-01-30", "2026-02-02", "2026-02-03"])
        self.marker("214450.KQ", ["2026-02-02"],
                    {"2026-02-02": "앞뒤 봉의 분할계수가 다르다"})
        self.assertIn("2026-02-02", memory._known_sessions("214450.KQ", "KR"))


class TestAWindowWithNoObservation(unittest.TestCase):
    def test_bars_only_after_the_horizon_are_not_the_period_end(self):
        """평가 기간 안에 봉이 하나도 없고 그 뒤에만 있다.

        예전에는 종료일 뒤의 첫 봉을 기간 말 가격으로 썼고, 고가·저가를 낼 행이
        없어 ValueError 로 그 자산의 채점 전체가 죽었다. 이 건만 건너뛰도록
        계약 오류로 알린다.
        """
        with self.assertRaises(ContractError) as raised:
            evaluate([bar("2026-01-02", 100), bar("2026-07-01", 150)])
        self.assertIn("가격 관측이 없다", raised.exception.errors[0])


class TestExcessReturnNeedsTheSameWindow(unittest.TestCase):
    SECURITY = [bar("2026-01-02", 100), bar("2026-01-05", 102), bar("2026-02-02", 120)]

    def horizons(self, benchmark, security=None, *, known_sessions=None,
                 benchmark_known=()):
        with patch.object(memory, "_price_rows", return_value=benchmark), \
                patch.object(memory, "_price_currency", return_value="USD"), \
                patch.object(memory, "_price_session", return_value=None), \
                patch.object(memory, "_known_sessions",
                             return_value=set(benchmark_known)):
            return _fixed_horizon_returns(
                rows=security or self.SECURITY, start=dt.date(2026, 1, 2),
                adjusted_cost=100, benchmarks=["INDEX"], asset_currency="USD",
                known_sessions=known_sessions)

    def test_a_security_without_its_end_session_is_not_compared_with_a_finished_index(self):
        """종목은 1/30 가격, 지수는 2/2 가격 — 예전에는 둘 다 완료라며 뺐다."""
        fixed = self.horizons(
            [bar("2026-01-02", 100), bar("2026-01-30", 102), bar("2026-02-02", 105),
             bar("2026-02-03", 106)],
            security=[bar("2026-01-02", 100), bar("2026-01-30", 110),
                      bar("2026-02-03", 125)],
            known_sessions={"2026-02-02"})
        own = fixed["months"]["m1"]
        row = fixed["vs_benchmark"]["INDEX"]["m1"]
        self.assertFalse(own["complete"])
        self.assertEqual(own["observed_date"], "2026-01-30")
        self.assertEqual(own["missing_sessions_before_target"], ["2026-02-02"])
        self.assertTrue(row["benchmark_complete"])
        self.assertEqual(row["benchmark_observed_date"], "2026-02-02")
        self.assertIsNone(row["excess_pct"])

    def test_an_index_without_its_end_session_is_not_finished_either(self):
        fixed = self.horizons(
            [bar("2026-01-02", 100), bar("2026-01-30", 102), bar("2026-02-03", 106)],
            security=[bar("2026-01-02", 100), bar("2026-01-30", 110),
                      bar("2026-02-02", 120), bar("2026-02-03", 125)],
            benchmark_known={"2026-02-02"})
        row = fixed["vs_benchmark"]["INDEX"]["m1"]
        self.assertTrue(fixed["months"]["m1"]["complete"])
        self.assertFalse(row["benchmark_complete"])
        self.assertEqual(row["benchmark_missing_sessions_before_target"], ["2026-02-02"])
        self.assertIsNone(row["excess_pct"])

    def test_a_benchmark_that_stopped_early_is_not_subtracted(self):
        """종목은 2/2 까지 +20%, 지수 파일은 1/5 에서 멈춰 +1%. 뺄 수 없다."""
        fixed = self.horizons([bar("2026-01-02", 100), bar("2026-01-05", 101)])
        row = fixed["vs_benchmark"]["INDEX"]["m1"]
        self.assertTrue(fixed["months"]["m1"]["complete"])
        self.assertEqual(row["benchmark_return_pct"], 1.0)
        self.assertFalse(row["benchmark_complete"])
        self.assertEqual(row["benchmark_observed_date"], "2026-01-05")
        self.assertIsNone(row["excess_pct"])

    def test_both_complete_is_a_comparison(self):
        fixed = self.horizons([bar("2026-01-02", 100), bar("2026-01-05", 101),
                               bar("2026-02-02", 105)])
        self.assertEqual(fixed["vs_benchmark"]["INDEX"]["m1"]["excess_pct"], 15.0)

    def test_both_in_progress_through_the_same_day_is_a_comparison(self):
        fixed = self.horizons([bar("2026-01-02", 100), bar("2026-02-02", 105)])
        row = fixed["vs_benchmark"]["INDEX"]["m3"]
        self.assertFalse(fixed["months"]["m3"]["complete"])
        self.assertEqual(row["excess_pct"], 15.0)

    def test_in_progress_through_different_days_is_not(self):
        """종목 파일만 오래됐을 때 — 실제 저장 평가 11건이 이 모양이었다."""
        fixed = self.horizons(
            [bar("2026-01-02", 100), bar("2026-01-05", 101), bar("2026-03-02", 110)],
            security=self.SECURITY[:2])
        row = fixed["vs_benchmark"]["INDEX"]["m3"]
        self.assertEqual(row["benchmark_observed_date"], "2026-03-02")
        self.assertIsNone(row["excess_pct"])


if __name__ == "__main__":
    unittest.main()
