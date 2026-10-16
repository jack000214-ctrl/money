"""forecast_engine의 리포트 보관 정책 (7일 이내 & 알고리즘별 1일 1건) 테스트."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from scripts import forecast_engine


class TestForecastCleanup(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.patcher = patch.object(forecast_engine, "FORECASTS_DIR", self.temp_dir)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_report(self, report_id: str, algorithm: str, created_at_iso: str) -> Path:
        data = {
            "id": report_id,
            "algorithm": algorithm,
            "algorithm_label": forecast_engine.ALGORITHMS.get(algorithm, algorithm),
            "created_at": created_at_iso,
            "asset_count": 5,
            "results": [],
        }
        path = self.temp_dir / f"{report_id}.json"
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def test_cleanup_deletes_older_than_7_days(self):
        """7일 초과된 리포트는 자동 삭제되어야 한다."""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=8)).isoformat()
        recent_time = (now - timedelta(days=2)).isoformat()

        self._create_report("FC-OLD01", "MA_CROSS", old_time)
        self._create_report("FC-REC01", "MA_CROSS", recent_time)

        deleted = forecast_engine.cleanup_old_forecasts(retention_days=7)
        self.assertEqual(deleted, 1)
        self.assertFalse((self.temp_dir / "FC-OLD01.json").exists())
        self.assertTrue((self.temp_dir / "FC-REC01.json").exists())

    def test_cleanup_keeps_only_latest_per_day_per_algorithm(self):
        """동일 일자(KST) & 동일 알고리즘은 가장 최신 1개만 남기고 이전 것을 삭제한다."""
        # 2026-10-16 KST (UTC 2026-10-16 01:00:00 vs 02:00:00)
        dt1 = datetime(2026, 10, 16, 1, 0, 0, tzinfo=timezone.utc)
        dt2 = datetime(2026, 10, 16, 2, 0, 0, tzinfo=timezone.utc)
        dt3 = datetime(2026, 10, 16, 3, 0, 0, tzinfo=timezone.utc)

        # MA_CROSS 3건 (같은 날)
        self._create_report("FC-MA-01", "MA_CROSS", dt1.isoformat())
        self._create_report("FC-MA-02", "MA_CROSS", dt2.isoformat())
        self._create_report("FC-MA-03", "MA_CROSS", dt3.isoformat())

        # COMBINED 2건 (같은 날)
        self._create_report("FC-CB-01", "COMBINED", dt1.isoformat())
        self._create_report("FC-CB-02", "COMBINED", dt3.isoformat())

        deleted = forecast_engine.cleanup_old_forecasts(retention_days=7)
        # MA_CROSS에서 2개 삭제, COMBINED에서 1개 삭제 -> 총 3개 삭제
        self.assertEqual(deleted, 3)

        self.assertFalse((self.temp_dir / "FC-MA-01.json").exists())
        self.assertFalse((self.temp_dir / "FC-MA-02.json").exists())
        self.assertTrue((self.temp_dir / "FC-MA-03.json").exists())

        self.assertFalse((self.temp_dir / "FC-CB-01.json").exists())
        self.assertTrue((self.temp_dir / "FC-CB-02.json").exists())

    def test_different_days_and_different_algorithms_are_kept(self):
        """다른 날짜이거나 다른 알고리즘이면 7일 이내일 때 각각 1개씩 정상 보관된다."""
        now = datetime.now(timezone.utc)
        today = now.isoformat()
        yesterday = (now - timedelta(days=1)).isoformat()
        two_days_ago = (now - timedelta(days=2)).isoformat()

        self._create_report("FC-MA-TODAY", "MA_CROSS", today)
        self._create_report("FC-MA-YESTERDAY", "MA_CROSS", yesterday)
        self._create_report("FC-RSI-TODAY", "RSI", today)
        self._create_report("FC-BOLL-2DAYS", "BOLLINGER", two_days_ago)

        deleted = forecast_engine.cleanup_old_forecasts(retention_days=7)
        self.assertEqual(deleted, 0)

        # 4개 모두 살아있어야 함
        summaries = forecast_engine.list_forecasts()
        self.assertEqual(len(summaries), 4)


if __name__ == "__main__":
    unittest.main()
