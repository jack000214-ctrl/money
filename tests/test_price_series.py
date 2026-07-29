"""조정종가·분할 보정 가격 계약 회귀 테스트."""

import csv
import datetime as dt
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cio.context import _price_summary
from cio.memory import evaluate_assessment
from cio.refresh import RefreshTask, inspect_task
from collectors import kr_events, price
from test_cio_core import valid_context, valid_decision
from cio.decision import normalize_decision


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class TestPriceCollectorV2(unittest.TestCase):
    def test_yahoo_rows_align_and_split_factor_is_preserved(self):
        payload = {
            "chart": {
                "result": [
                    {
                        "timestamp": [86400, 259200],
                        "meta": {"currency": "USD"},
                        "indicators": {
                            "quote": [
                                {
                                    "close": [100.0, 55.0],
                                    "high": [101.0, 56.0],
                                    "low": [99.0, 54.0],
                                    "volume": [10, 20],
                                }
                            ],
                            "adjclose": [{"adjclose": [50.0, 55.0]}],
                        },
                        "events": {
                            "splits": {
                                "172800": {
                                    "date": 172800,
                                    "numerator": 2.0,
                                    "denominator": 1.0,
                                }
                            }
                        },
                    }
                ],
                "error": None,
            }
        }
        with patch(
            "collectors.price.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ):
            _meta, rows = price.fetch("TEST", "5y")
        self.assertEqual(rows[0]["split_adjustment_factor"], 0.5)
        self.assertEqual(rows[1]["split_adjustment_factor"], 1.0)
        self.assertEqual(rows[1]["adjusted_close"], 55.0)

    def test_saved_contract_has_adjusted_and_split_columns(self):
        rows = [
            {
                "date": "2026-07-28",
                "close": 100,
                "adjusted_close": 99,
                "split_adjustment_factor": 1,
                "high": 101,
                "low": 98,
                "volume": 10,
            }
        ]
        with tempfile.TemporaryDirectory() as tmp, patch(
            "collectors.price.PRICE_DIR", Path(tmp)
        ):
            path = price.save("TEST", {"currency": "USD"}, rows)
            text = path.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# schema_version price-series/2.1.0"))
        self.assertIn("adjusted_close,split_adjustment_factor", text)
        self.assertIn("# last_bar 2026-07-28 / session UNKNOWN\n", text)

    def test_session_state_marks_bar_open_during_the_session(self):
        # 거래소가 선언한 세션 경계를 쓴다. 장 시간을 하드코딩하지 않는다.
        offset = 32400
        meta = {
            "gmtoffset": offset,
            "regularMarketTime": 1785286641,
            "currentTradingPeriod": {
                "regular": {"start": 1785283200, "end": 1785304800}
            },
        }
        self.assertEqual(price.session_state(meta, "2026-07-29"), "UNSETTLED")
        closed = {**meta, "regularMarketTime": 1785304800}
        self.assertEqual(price.session_state(closed, "2026-07-29"), "SETTLED")
        # 마지막 봉이 이전 세션이면 이미 닫힌 봉이다.
        self.assertEqual(price.session_state(meta, "2026-07-28"), "SETTLED")
        self.assertEqual(price.session_state({}, "2026-07-29"), "UNKNOWN")


class TestBenchmarkCalendarAlignment(unittest.TestCase):
    """벤치마크에 거래일 구멍이 나면 상대변동을 내지 않는다.

    2026-07-28 코스피 -10.84% 폭락일이 Yahoo 시계열에서 통째로 빠졌을 때,
    07-27 과 07-29 를 직접 빼서 '지수 -9.52%' 라는 없는 하루가 만들어졌고
    삼성전자 07-29 가 상대 +13.39% 아웃퍼폼으로 보고됐다. 실제로는 07-28 에
    시장 -10.84% / 종목 -13.39% (상대 -2.55%) 였다. 부호까지 뒤집힌다.
    """

    def test_daily_returns_carry_the_previous_trading_day(self):
        series = [("2026-07-27", 100.0, 1), ("2026-07-29", 90.0, 1)]
        self.assertEqual(
            kr_events.daily_returns(series)["2026-07-29"][1], "2026-07-27"
        )

    def test_relative_move_is_refused_when_the_benchmark_skipped_a_day(self):
        asset = [
            ("2026-07-27", 254000.0, 1),
            ("2026-07-28", 220000.0, 1),
            ("2026-07-29", 228500.0, 1),
        ]
        gapped = [("2026-07-27", 6755.75, 1), ("2026-07-29", 6112.42, 1)]
        asset_returns = kr_events.daily_returns(asset)
        bench_returns = kr_events.daily_returns(gapped)
        # 구멍이 난 채로 빼면 +13.4% 아웃퍼폼이라는 거짓 신호가 나온다.
        naive = (
            asset_returns["2026-07-29"][0] - bench_returns["2026-07-29"][0]
        )
        self.assertGreater(naive, 13.0)
        # 정합성을 확인하면 계산 자체를 거부한다.
        self.assertIsNone(
            kr_events.aligned_benchmark(
                bench_returns, "2026-07-29", asset_returns["2026-07-29"][1]
            )
        )

    def test_relative_move_is_computed_once_the_gap_is_backfilled(self):
        asset = [
            ("2026-07-27", 254000.0, 1),
            ("2026-07-28", 220000.0, 1),
            ("2026-07-29", 228500.0, 1),
        ]
        repaired = [
            ("2026-07-27", 6755.75, 1),
            ("2026-07-28", 6023.66, 1),
            ("2026-07-29", 6112.42, 1),
        ]
        asset_returns = kr_events.daily_returns(asset)
        bench_returns = kr_events.daily_returns(repaired)
        crash = kr_events.aligned_benchmark(
            bench_returns, "2026-07-28", asset_returns["2026-07-28"][1]
        )
        assert crash is not None
        # KRX 원문 등락률 -10.84% 와 일치해야 한다.
        self.assertAlmostEqual(crash, -10.84, places=2)
        excess = asset_returns["2026-07-28"][0] - crash
        self.assertAlmostEqual(excess, -2.55, places=2)


class TestBackfillRecordVerifiesItself(unittest.TestCase):
    """백필 기록은 스스로를 증명해야 한다.

    가격 파일이 어떤 경로로든 다시 쓰여 메웠던 거래일이 사라지면, 기록만 남아
    '메웠다'고 거짓말한다. 그러면 백필은 영원히 다시 돌지 않는다.
    """

    def _task(self) -> RefreshTask:
        return RefreshTask(
            name="benchmark_backfill:^KS11",
            capability="BENCHMARK_BACKFILL",
            artifact="knowledge/prices/_backfill/KS11.json",
            max_age_hours=12,
            command=None,
        )

    def _write(self, root: Path, *, include_filled_row: bool) -> None:
        prices = root / "knowledge" / "prices"
        (prices / "_backfill").mkdir(parents=True, exist_ok=True)
        rows = "2026-07-27,6755.75,6755.75,1,6806,6557,275700\n"
        if include_filled_row:
            rows += "2026-07-28,6023.66,6023.66,1,6413,5992,\n"
        (prices / "KS11.csv").write_text(
            "# schema_version price-series/2.1.0\n"
            "# symbol ^KS11 / currency KRW\n"
            "# retrieved_at 2026-07-29T00:00:00+00:00 / "
            "source Yahoo Finance chart API\n"
            "# last_bar 2026-07-28 / session SETTLED\n"
            "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
            + rows,
            encoding="utf-8",
        )
        (prices / "_backfill" / "KS11.json").write_text(
            json.dumps(
                {
                    "schema_version": "price-backfill/1.0.0",
                    "symbol": "^KS11",
                    "target": "KS11.csv",
                    "collected_at": "2026-07-29T01:00:00+00:00",
                    "filled": ["2026-07-28"],
                    "unfilled": [],
                }
            ),
            encoding="utf-8",
        )

    def test_record_is_fresh_while_the_filled_day_is_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, include_filled_row=True)
            now = dt.datetime(2026, 7, 29, 2, tzinfo=dt.timezone.utc)
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(self._task(), now=now).state, "FRESH")

    def test_record_goes_stale_when_the_filled_day_disappears_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._write(root, include_filled_row=False)
            now = dt.datetime(2026, 7, 29, 2, tzinfo=dt.timezone.utc)
            with patch("cio.refresh.ROOT", root):
                checked = inspect_task(self._task(), now=now)
        self.assertEqual(checked.state, "STALE")
        self.assertIn("다시 사라졌다", checked.reason)


class TestPriceConsumers(unittest.TestCase):
    def test_context_returns_use_adjusted_close_but_range_uses_raw(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            path = knowledge / "prices" / "TEST.csv"
            path.parent.mkdir(parents=True)
            stream = io.StringIO()
            writer = csv.DictWriter(
                stream,
                fieldnames=[
                    "date",
                    "close",
                    "adjusted_close",
                    "split_adjustment_factor",
                    "high",
                    "low",
                    "volume",
                ],
            )
            writer.writeheader()
            for day in range(1, 23):
                writer.writerow(
                    {
                        "date": f"2026-07-{day:02d}",
                        "close": 100,
                        "adjusted_close": 50 if day == 1 else 55,
                        "split_adjustment_factor": 1,
                        "high": 101,
                        "low": 99,
                        "volume": 1,
                    }
                )
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-22T00:00:00+00:00 / "
                "source Yahoo Finance chart API\n"
                "# last_bar 2026-07-22 / session SETTLED\n"
                + stream.getvalue(),
                encoding="utf-8",
            )
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _price_summary("TEST")
        self.assertEqual(summary["returns_pct"]["1m"], 10.0)
        self.assertEqual(summary["position_in_12m_range_pct"], 50.0)
        self.assertEqual(summary["returns_basis"], "ADJUSTED_CLOSE")
        self.assertTrue(summary["last_bar"]["is_settled_close"])
        self.assertIsNone(summary["last_bar"]["_warning"])

    def test_context_flags_an_unsettled_last_bar_to_the_model(self):
        """장중 수집분은 패킷 자체가 '확정 종가 아님'을 말해야 한다."""
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            path = knowledge / "prices" / "TEST.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-29T00:59:00+00:00 / "
                "source Yahoo Finance chart API\n"
                "# last_bar 2026-07-29 / session UNSETTLED\n"
                "date,close,adjusted_close,split_adjustment_factor,"
                "high,low,volume\n"
                "2026-07-28,100,100,1,101,99,40000000\n"
                "2026-07-29,104,104,1,105,103,9000000\n",
                encoding="utf-8",
            )
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _price_summary("TEST")
        self.assertEqual(summary["last_bar"]["session"], "UNSETTLED")
        self.assertFalse(summary["last_bar"]["is_settled_close"])
        self.assertIn("확정 종가가 아니다", summary["last_bar"]["_warning"])

    def test_refresh_rejects_v1_and_accepts_complete_v2(self):
        task = RefreshTask(
            name="price:TEST",
            capability="PRICE",
            artifact="prices/TEST.csv",
            max_age_hours=12,
            command=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = dt.datetime(2026, 7, 28, 1, tzinfo=dt.timezone.utc)
            path = root / "prices" / "TEST.csv"
            path.parent.mkdir(parents=True)
            path.write_text("date,close\n2026-07-28,100\n", encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(task, now=now).state, "STALE")
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-28T00:00:00+00:00 / "
                "source Yahoo Finance chart API\n"
                "# last_bar 2026-07-28 / session SETTLED\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
                "2026-07-28,100,100,1,101,99,1\n",
                encoding="utf-8",
            )
            task.state = "UNKNOWN"
            task.reason = None
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(task, now=now).state, "FRESH")
            # 세션 상태 줄이 없으면 계약 위반이다 — 확정 여부를 알 수 없다.
            task.state = "UNKNOWN"
            task.reason = None
            without_session = "".join(
                line
                for line in path.read_text(encoding="utf-8").splitlines(True)
                if not line.startswith("# last_bar")
            )
            path.write_text(without_session, encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                checked = inspect_task(task, now=now)
            self.assertEqual(checked.state, "STALE")
            self.assertIn("세션 상태", checked.reason)
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-28T00:00:00+00:00 / "
                "source Yahoo Finance chart API\n"
                "# last_bar 2026-07-28 / session SETTLED\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
                "2026-07-28,100,100,1,101,99,1\n",
                encoding="utf-8",
            )
            path.write_text(
                path.read_text(encoding="utf-8").replace(
                    "2026-07-28T00:00:00+00:00",
                    "2020-07-28T00:00:00+00:00",
                ),
                encoding="utf-8",
            )
            task.state = "UNKNOWN"
            task.reason = None
            with patch("cio.refresh.ROOT", root):
                checked = inspect_task(task, now=now)
            self.assertEqual(checked.state, "STALE")
            self.assertGreater(checked.age_hours, 12)
            path.write_text(
                path.read_text(encoding="utf-8")
                .replace(
                    "2020-07-28T00:00:00+00:00",
                    "2026-07-28T00:00:00+00:00",
                )
                .replace("# symbol TEST", "# symbol OTHER"),
                encoding="utf-8",
            )
            task.state = "UNKNOWN"
            task.reason = None
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(task, now=now).state, "STALE")

    def test_split_does_not_false_trigger_stop_or_scale_out(self):
        assessment = normalize_decision(valid_decision())
        rows = [
            {
                "date": "2026-01-02",
                "close": 100.0,
                "adjusted_close": 50.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 0.5,
                "high": 101.0,
                "low": 99.0,
            },
            {
                "date": "2026-01-15",
                "close": 55.0,
                "adjusted_close": 55.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 65.0,
                "low": 45.0,
            },
            {
                "date": "2026-02-02",
                "close": 56.0,
                "adjusted_close": 56.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 60.0,
                "low": 54.0,
            },
        ]
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
        ):
            result = evaluate_assessment(assessment, rows=rows)
        self.assertIsNone(result["triggers"]["stop"])
        self.assertIsNone(result["triggers"]["scale_out_start"])
        self.assertGreater(result["price"]["return_to_end_pct"], 0)
        self.assertEqual(
            result["price"]["price_path_basis"], "SPLIT_ADJUSTED_RAW_OHLC"
        )

    def test_decision_day_ohlc_cannot_trigger_with_daily_data(self):
        assessment = normalize_decision(valid_decision())
        rows = [
            {
                "date": "2026-01-01",
                "close": 100.0,
                "adjusted_close": 100.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 150.0,
                "low": 70.0,
            },
            {
                "date": "2026-01-02",
                "close": 101.0,
                "adjusted_close": 101.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 110.0,
                "low": 90.0,
            },
            {
                "date": "2026-02-02",
                "close": 102.0,
                "adjusted_close": 102.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 110.0,
                "low": 90.0,
            },
        ]
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
        ):
            result = evaluate_assessment(assessment, rows=rows)
        self.assertEqual(
            result["window"]["first_observation_date"], "2026-01-02"
        )
        self.assertIsNone(result["triggers"]["stop"])
        self.assertIsNone(result["triggers"]["scale_out_start"])


if __name__ == "__main__":
    unittest.main()
