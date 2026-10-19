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
from test_cio_core import (
    small_price_context as valid_context,
    small_price_decision as valid_decision,
)
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


class TestSessionDateBoundaries(unittest.TestCase):
    @staticmethod
    def timestamp(iso):
        return int(dt.datetime.fromisoformat(iso).timestamp())

    def metadata(self, quote, start, end, **overrides):
        meta = {
            "exchangeTimezoneName": "America/New_York",
            "gmtoffset": -14400,
            "regularMarketTime": self.timestamp(quote),
            "currentTradingPeriod": {"regular": {
                "start": self.timestamp(start), "end": self.timestamp(end),
            }},
        }
        meta.update(overrides)
        return meta

    def test_us_premarket_keeps_previous_close_settled(self):
        # Sep 22 21:43 KST is still premarket in New York. The quote is the
        # Sep 21 close while currentTradingPeriod already points at Sep 22.
        meta = self.metadata("2026-09-21T16:00:00-04:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        self.assertEqual(price.session_state(meta, "2026-09-21"), "SETTLED")

    def test_weekend_and_dst_boundary_use_exchange_session_dates(self):
        # Friday closed in EDT; the next declared regular session is Monday EST.
        meta = self.metadata("2026-10-30T16:00:00-04:00",
                             "2026-11-02T09:30:00-05:00",
                             "2026-11-02T16:00:00-05:00", gmtoffset=-18000)
        self.assertEqual(price.session_state(meta, "2026-10-30"), "SETTLED")

    def test_korean_calendar_date_does_not_close_a_live_us_bar(self):
        # A Sep 22 US midday quote is Sep 23 in Korea, but its session is open.
        meta = self.metadata("2026-09-23T01:00:00+09:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        self.assertEqual(price.session_state(meta, "2026-09-22"), "UNSETTLED")
        meta["regularMarketTime"] = self.timestamp("2026-09-23T05:00:00+09:00")
        self.assertEqual(price.session_state(meta, "2026-09-22"), "SETTLED")

    def test_same_session_early_close_uses_declared_boundary(self):
        meta = self.metadata("2026-11-27T13:00:00-05:00",
                             "2026-11-27T09:30:00-05:00",
                             "2026-11-27T13:00:00-05:00", gmtoffset=-18000)
        self.assertEqual(price.session_state(meta, "2026-11-27"), "SETTLED")

    def test_future_or_inconsistent_bar_is_unknown(self):
        meta = self.metadata("2026-09-21T16:00:00-04:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        self.assertEqual(price.session_state(meta, "2026-09-23"), "UNKNOWN")
        self.assertEqual(price.session_state(meta, "not-a-date"), "UNKNOWN")
        self.assertEqual(price.session_state(meta, "2026-09-22"), "UNKNOWN")

    def test_missing_start_does_not_compare_yesterday_with_todays_end(self):
        meta = self.metadata("2026-09-21T16:00:00-04:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        del meta["currentTradingPeriod"]["regular"]["start"]
        self.assertEqual(price.session_state(meta, "2026-09-21"), "UNKNOWN")

    def test_missing_zone_uses_the_declared_period_offset(self):
        meta = self.metadata("2026-09-21T16:00:00-04:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        del meta["exchangeTimezoneName"]
        meta["currentTradingPeriod"]["regular"]["gmtoffset"] = -14400
        del meta["gmtoffset"]
        self.assertEqual(price.session_state(meta, "2026-09-21"), "SETTLED")

    def test_fx_utc_date_label_does_not_close_the_london_midnight_session(self):
        bar_timestamp = self.timestamp("2026-09-22T00:00:00+01:00")
        payload = {"chart": {"result": [{
            "meta": self.metadata("2026-09-22T00:30:00+01:00",
                                  "2026-09-22T00:00:00+01:00",
                                  "2026-09-22T23:59:00+01:00",
                                  exchangeTimezoneName="Europe/London", gmtoffset=3600),
            "timestamp": [bar_timestamp],
            "indicators": {"quote": [{"close": [1400], "high": [1401], "low": [1399], "volume": [0]}]},
        }]}}
        with patch("collectors.price.urllib.request.urlopen", return_value=FakeResponse(payload)):
            meta, rows = price.fetch("KRW=X", "5d")
        self.assertEqual(rows[-1]["date"], "2026-09-21")
        self.assertEqual(price.session_state(meta, rows[-1]["date"]), "UNSETTLED")
        with tempfile.TemporaryDirectory() as tmp, patch("collectors.price.PRICE_DIR", Path(tmp)):
            path = price.save("KRW=X", meta, rows)
            self.assertIn(f"# last_bar_timestamp {bar_timestamp}", path.read_text())
            self.assertIn("# last_bar 2026-09-21 / session UNSETTLED", path.read_text())

    def test_refetch_corrects_saved_last_bar_header(self):
        meta = self.metadata("2026-09-21T16:00:00-04:00",
                             "2026-09-22T09:30:00-04:00",
                             "2026-09-22T16:00:00-04:00")
        row = {"date": "2026-09-21", "close": 3.97, "adjusted_close": 3.97,
               "split_adjustment_factor": 1, "high": 4.015, "low": 3.83,
               "volume": 37686400}
        with tempfile.TemporaryDirectory() as tmp, patch("collectors.price.PRICE_DIR", Path(tmp)):
            path = price.save("RXRX", meta, [row])
            self.assertIn("# last_bar 2026-09-21 / session SETTLED", path.read_text())


class TestPriceSessionHeaderIntegrity(unittest.TestCase):
    def inspect(self, last_bar, state="SETTLED", last_row="2026-07-29"):
        task = RefreshTask(name="price:TEST", capability="PRICE", artifact="prices/TEST.csv",
                           max_age_hours=12, command=None)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            path = root / "prices" / "TEST.csv"
            path.parent.mkdir()
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-29T20:00:00+00:00 / source Yahoo Finance chart API\n"
                f"# last_bar {last_bar} / session {state}\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
                + (f"{last_row},100,100,1,101,99,1\n" if last_row else ""),
                encoding="utf-8",
            )
            with patch("cio.refresh.ROOT", root):
                return inspect_task(task, now=dt.datetime(2026, 7, 29, 21, tzinfo=dt.timezone.utc))

    def test_each_state_must_belong_to_the_actual_last_row(self):
        for state in ("SETTLED", "UNSETTLED", "UNKNOWN"):
            for header_date in ("2026-07-28", "2026-07-30"):
                with self.subTest(state=state, header_date=header_date):
                    checked = self.inspect(header_date, state)
                    self.assertEqual(checked.state, "STALE")
                    self.assertIn("last_bar 날짜", checked.reason)  # pyright: ignore[reportArgumentType]
            with self.subTest(state=state, header_date="matching"):
                self.assertEqual(self.inspect("2026-07-29", state).state, "FRESH")

    def test_no_rows_cannot_validate_a_session_header(self):
        checked = self.inspect("2026-07-29", last_row=None)  # pyright: ignore[reportArgumentType]
        self.assertEqual(checked.state, "STALE")
        self.assertIn("데이터 행", checked.reason)  # pyright: ignore[reportArgumentType]

    def test_matching_but_impossible_dates_are_invalid(self):
        checked = self.inspect("2026-07-99", last_row="2026-07-99")
        self.assertEqual(checked.state, "STALE")
        self.assertIn("날짜", checked.reason)  # pyright: ignore[reportArgumentType]


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
        self.assertIn("다시 사라졌다", checked.reason)  # pyright: ignore[reportArgumentType]


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
        assert summary is not None
        self.assertEqual(summary["returns_pct"]["1m"], 10.0)
        # 이 표본은 종가가 전부 100 이라 고가=저가다. 범위가 없으면 그 안의
        # 위치도 없다 — 여기는 packet 이라 50 을 넣으면 모델이 "구간 중간"을
        # 관측으로 읽는다.
        self.assertIsNone(summary["position_in_12m_range_pct"])
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
        assert summary is not None
        self.assertEqual(summary["last_bar"]["session"], "UNSETTLED")
        self.assertFalse(summary["last_bar"]["is_settled_close"])
        self.assertIn("확정 종가가 아니다", summary["last_bar"]["_warning"])

    def _last_bar(self, session_line):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            path = knowledge / "prices" / "TEST.csv"
            path.parent.mkdir(parents=True)
            path.write_text(
                "# schema_version price-series/2.1.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-29T00:59:00+00:00 / "
                "source Yahoo Finance chart API\n"
                + session_line
                + "date,close,adjusted_close,split_adjustment_factor,"
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
        assert summary is not None
        return summary["last_bar"]

    def test_an_undecidable_session_is_not_reported_as_unsettled(self):
        """수집기는 세 갈래를 남기는데 packet 이 둘로 접고 있었다.

        `session_state` 는 거래소 세션 경계를 못 읽으면 UNKNOWN 을 낸다. 그걸
        `bool(session) and ... == "SETTLED"` 로 접으면 **모른다가 '확정 종가가
        아니다'** 로 바뀌고, 경고는 UNSETTLED 일 때만 붙으므로 모델은 이유 없는
        음성 하나만 본다. 관측하지 않은 것과 관측된 음성은 같은 값일 수 없다.
        """
        bar = self._last_bar("# last_bar 2026-07-29 / session UNKNOWN\n")
        self.assertEqual(bar["session"], "UNKNOWN")
        self.assertIsNone(bar["is_settled_close"])
        self.assertIn("판별하지 못했다", bar["_warning"])

    def test_a_missing_session_header_is_also_undecidable(self):
        bar = self._last_bar("")
        self.assertEqual(bar["session"], "UNKNOWN")
        self.assertIsNone(bar["is_settled_close"])
        self.assertIsNotNone(bar["_warning"])

    def test_a_settled_bar_carries_no_warning(self):
        bar = self._last_bar("# last_bar 2026-07-29 / session SETTLED\n")
        self.assertIs(bar["is_settled_close"], True)
        self.assertIsNone(bar["_warning"])


class TestScoringKeepsTheThirdSessionState(unittest.TestCase):
    """채점 기록도 세 갈래를 둘로 접지 않는다.

    평가는 저장되고 나중에 읽힌다. 그때 `latest_is_settled_close: false` 만
    남아 있으면 읽는 쪽은 그것을 관측으로 받아들이는데, 실제로는 판별을 못 한
    것일 수 있다. packet 과 같은 규칙이어야 두 기록이 같은 뜻을 갖는다.
    """

    ROWS = [
        {
            "date": "2026-01-02", "close": 100.0, "adjusted_close": 100.0,
            "has_adjusted_close": True, "split_adjustment_factor": 1.0,
            "high": 101.0, "low": 99.0,
        },
        {
            "date": "2026-02-02", "close": 110.0, "adjusted_close": 110.0,
            "has_adjusted_close": True, "split_adjustment_factor": 1.0,
            "high": 111.0, "low": 109.0,
        },
    ]

    def window(self, session):
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
            patch("cio.memory._price_session", return_value=session),
        ):
            result = evaluate_assessment(
                normalize_decision(valid_decision()), rows=self.ROWS
            )
        return result["window"]

    def test_undecidable_session_is_null_not_false(self):
        window = self.window({"last_bar": "2026-02-02", "session": "UNKNOWN"})
        self.assertIsNone(window["latest_is_settled_close"])
        self.assertIn("판별하지 못했다", window["_settled_note"])

    def test_intraday_bar_is_false_and_says_why(self):
        window = self.window({"last_bar": "2026-02-02", "session": "UNSETTLED"})
        self.assertIs(window["latest_is_settled_close"], False)
        self.assertIn("장중 수집분", window["_settled_note"])

    def test_settled_bar_is_true_with_no_note(self):
        window = self.window({"last_bar": "2026-02-02", "session": "SETTLED"})
        self.assertIs(window["latest_is_settled_close"], True)
        self.assertIsNone(window["_settled_note"])

    def test_an_older_bar_is_closed_whatever_the_header_says(self):
        """쓰고 있는 행이 헤더의 마지막 봉이 아니면 이미 닫힌 봉이다."""
        for state in ("UNKNOWN", "UNSETTLED", "SETTLED"):
            window = self.window({"last_bar": "2026-03-01", "session": state})
            self.assertIs(window["latest_is_settled_close"], True, state)
            self.assertIsNone(window["_settled_note"], state)

    def test_no_price_header_at_all_is_unknown(self):
        window = self.window(None)
        self.assertIsNone(window["latest_is_settled_close"])
        self.assertIsNone(window["_settled_note"])

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
            self.assertIn("세션 상태", checked.reason)  # pyright: ignore[reportArgumentType]
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
            self.assertGreater(checked.age_hours, 12)  # pyright: ignore[reportArgumentType, reportCallIssue]
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
        """저장된 봉은 **이미 분할이 반영된** 값이다. 한 번 더 조정하면 안 된다.

        판단일 2026-01-01 / 질문가 100 / 손절 80 / 분할매도 시작 140 인데
        2026-01-15 에 2:1 분할이 있었다. Yahoo 는 소급 조정해서 주므로 분할 전
        구간의 종가·고가·저가는 이미 절반으로 저장돼 있고, 그 행의
        `split_adjustment_factor` 만 0.5 로 남아 "이 값 = 그날 호가 × 0.5" 를
        말한다. 실제 파일이 그렇다 — MSTR 은 10:1 분할 전날 종가가 124.68 이고
        분할 다음 날 135.99 로 끊김 없이 이어진다.

        그러니 보정할 것은 시계열이 아니라 **판단 시점 호가로 적힌 문턱**이다.
        시계열에까지 계수를 곱하면 분할 전 구간이 통째로 절반이 되어, 닿은 적
        없는 손절가가 닿았다고 기록되고 수익률은 -45% 로 뒤집힌다.
        """
        rows = [
            # 분할 전. 그날 실제 호가는 close 101 / high 102 / low 99 였고
            # 파일에는 2:1 이 반영된 절반 값이 들어 있다.
            {
                "date": "2026-01-02",
                "close": 50.5,
                "adjusted_close": 50.5,
                "has_adjusted_close": True,
                "split_adjustment_factor": 0.5,
                "high": 51.0,
                "low": 49.5,
            },
            {
                "date": "2026-01-15",
                "close": 55.0,
                "adjusted_close": 55.0,
                "has_adjusted_close": True,
                "split_adjustment_factor": 1.0,
                "high": 57.0,
                "low": 54.0,
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
        assessment = normalize_decision(valid_decision())
        with (
            patch(
                "cio.memory.get_asset",
                return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
            ),
            patch("cio.memory.find_context", return_value=(valid_context(), None)),
        ):
            result = evaluate_assessment(assessment, rows=rows)
        # 손절 80 은 분할 후 기준 40, 분할매도 시작 140 은 70 이다. 경로의
        # 저가 최저는 49.5, 고가 최고는 57 이라 어느 쪽도 닿지 않았다.
        self.assertIsNone(result["triggers"]["stop"])
        self.assertIsNone(result["triggers"]["scale_out_start"])
        # 질문가 100 은 분할 후 기준 50 이고 지평 종가는 55 다 → +10%.
        self.assertAlmostEqual(
            result["price"]["return_to_end_pct"], 10.0, places=6
        )
        self.assertAlmostEqual(
            result["price"]["adjustment_factor_at_start"], 0.5, places=10
        )
        # 최대손실은 49.5/50 - 1 = -1% 다. 시계열을 한 번 더 조정하면 -50% 가 된다.
        self.assertAlmostEqual(result["price"]["max_loss_pct"], -1.0, places=6)
        self.assertAlmostEqual(result["price"]["max_gain_pct"], 14.0, places=6)
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


def chart_payload(timestamps, granularity=None):
    """일봉 형태의 최소 Yahoo 응답. meta 의 granularity 는 선택이다."""
    count = len(timestamps)
    meta = {"currency": "KRW"}
    if granularity is not None:
        meta["dataGranularity"] = granularity
    return {
        "chart": {
            "result": [
                {
                    "timestamp": list(timestamps),
                    "meta": meta,
                    "indicators": {
                        "quote": [
                            {
                                "close": [100.0] * count,
                                "high": [101.0] * count,
                                "low": [99.0] * count,
                                "volume": [10] * count,
                            }
                        ],
                        "adjclose": [{"adjclose": [100.0] * count}],
                    },
                }
            ],
            "error": None,
        }
    }


class TestDailyGranularityContract(unittest.TestCase):
    """일봉을 요청했는데 월봉이 와도 그대로 저장되던 구멍.

    Yahoo 는 interval=1d 로 요청해도 range 가 크면 조용히 봉을 굵게 만든다
    (range=max → dataGranularity=1mo). 실제로 2,447행짜리 일봉 파일이
    320행짜리 월봉으로 덮였고, 헤더에는 그 사실이 남지 않았다. 그 뒤의
    변동성·이동평균·급변 판정은 전부 월봉 위에서 조용히 계산된다.
    """

    def fetch(self, payload):
        with patch(
            "collectors.price.urllib.request.urlopen",
            return_value=FakeResponse(payload),
        ):
            return price.fetch("TEST", "max")

    def test_declared_monthly_granularity_is_refused_before_saving(self):
        payload = chart_payload(
            [86400 * 30 * step for step in range(1, 13)], granularity="1mo"
        )
        with self.assertRaises(SystemExit):
            self.fetch(payload)

    def test_monthly_row_spacing_is_refused_when_meta_is_silent(self):
        # meta 가 granularity 를 말해 주지 않아도 행 간격이 말해 준다.
        payload = chart_payload([86400 * 30 * step for step in range(1, 13)])
        with self.assertRaises(SystemExit):
            self.fetch(payload)

    def test_weekly_row_spacing_is_refused_too(self):
        payload = chart_payload([86400 * 7 * step for step in range(1, 13)])
        with self.assertRaises(SystemExit):
            self.fetch(payload)

    def test_daily_rows_with_weekend_gaps_still_pass(self):
        # 주말·공휴일을 건너뛰어도 일봉이다. 중앙 간격으로 세는 이유다.
        days = [1, 2, 3, 4, 5, 8, 9, 10, 11, 12, 15, 16]
        payload = chart_payload([86400 * day for day in days], granularity="1d")
        meta, rows = self.fetch(payload)
        self.assertEqual(len(rows), len(days))
        self.assertEqual(price.granularity(meta, rows), "1d")

    def test_granularity_is_unknown_rather_than_guessed_from_one_bar(self):
        self.assertEqual(price.granularity({}, [{"date": "2026-07-28"}]), "UNKNOWN")

    def test_refresh_flags_a_price_file_that_is_not_daily(self):
        # 이미 디스크에 있는 파일도 검사한다. 쓰기를 막는 것만으로는 사고
        # 이전에 저장된 파일이 그대로 모델에 실려 나간다.
        task = RefreshTask(
            name="price:TEST",
            capability="PRICE",
            artifact="prices/TEST.csv",
            max_age_hours=12,
            command=None,
        )
        header = (
            "# schema_version price-series/2.1.0\n"
            "# symbol TEST / currency KRW\n"
            "# retrieved_at 2026-07-28T00:00:00+00:00 / "
            "source Yahoo Finance chart API\n"
            "# last_bar 2026-07-28 / session SETTLED\n"
            "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
        )
        monthly = "".join(
            f"2026-{month:02d}-01,100,100,1,101,99,1\n" for month in range(1, 8)
        ) + "2026-07-28,100,100,1,101,99,1\n"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = dt.datetime(2026, 7, 28, 1, tzinfo=dt.timezone.utc)
            path = root / "prices" / "TEST.csv"
            path.parent.mkdir(parents=True)
            path.write_text(header + monthly, encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                checked = inspect_task(task, now=now)
            self.assertEqual(checked.state, "STALE")
            self.assertIn("일봉", checked.reason)  # pyright: ignore[reportArgumentType]

            daily = "".join(
                f"2026-07-{day:02d},100,100,1,101,99,1\n"
                for day in (13, 14, 15, 16, 17, 20, 21, 28)
            )
            path.write_text(header + daily, encoding="utf-8")
            task.state = "UNKNOWN"
            task.reason = None
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(task, now=now).state, "FRESH")


if __name__ == "__main__":
    unittest.main()
