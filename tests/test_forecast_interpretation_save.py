"""forecast_engine.save_interpretation: 동시 해석 저장 시 서로의 결과를 덮어쓰지 않는다."""

from __future__ import annotations

import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import forecast_engine


class TestSaveInterpretation(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.patcher = patch.object(forecast_engine, "FORECASTS_DIR", self.temp_dir)
        self.patcher.start()
        report = {
            "id": "FC-TEST01",
            "algorithm": "MA_CROSS",
            "algorithm_label": "x",
            "created_at": "2026-10-16T00:00:00+00:00",
            "asset_count": 3,
            "results": [{"ticker": t, "name": t} for t in ("A", "B", "C")],
        }
        (self.temp_dir / "FC-TEST01.json").write_text(
            json.dumps(report, ensure_ascii=False), encoding="utf-8"
        )

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _load(self) -> dict:
        return json.loads((self.temp_dir / "FC-TEST01.json").read_text(encoding="utf-8"))

    def test_sequential_saves_accumulate(self):
        self.assertTrue(forecast_engine.save_interpretation("FC-TEST01", "A", {"success": True, "headline": "a"}))
        self.assertTrue(forecast_engine.save_interpretation("FC-TEST01", "B", {"success": True, "headline": "b"}))
        got = {r["ticker"]: r.get("ai_interpretation") for r in self._load()["results"]}
        self.assertEqual(got["A"]["headline"], "a")
        self.assertEqual(got["B"]["headline"], "b")
        self.assertIsNone(got["C"])

    def test_concurrent_saves_do_not_lose_updates(self):
        threads = [
            threading.Thread(
                target=forecast_engine.save_interpretation,
                args=("FC-TEST01", t, {"success": True, "headline": t}),
            )
            for t in ("A", "B", "C")
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        for r in self._load()["results"]:
            self.assertEqual(r["ai_interpretation"]["headline"], r["ticker"])

    def test_missing_report_or_ticker_returns_false(self):
        self.assertFalse(forecast_engine.save_interpretation("FC-NOPE", "A", {"success": True}))
        self.assertFalse(forecast_engine.save_interpretation("FC-TEST01", "ZZZ", {"success": True}))


if __name__ == "__main__":
    unittest.main()
