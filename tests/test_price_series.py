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
from collectors import price
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
        self.assertTrue(text.startswith("# schema_version price-series/2.0.0"))
        self.assertIn("adjusted_close,split_adjustment_factor", text)


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
                "# schema_version price-series/2.0.0\n"
                "# symbol TEST / currency USD\n"
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
                "# schema_version price-series/2.0.0\n"
                "# symbol TEST / currency USD\n"
                "# retrieved_at 2026-07-28T00:00:00+00:00 / "
                "source Yahoo Finance chart API\n"
                "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
                "2026-07-28,100,100,1,101,99,1\n",
                encoding="utf-8",
            )
            task.state = "UNKNOWN"
            task.reason = None
            with patch("cio.refresh.ROOT", root):
                self.assertEqual(inspect_task(task, now=now).state, "FRESH")
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
