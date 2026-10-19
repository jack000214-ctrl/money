"""시계열에서 빠진 거래일이 packet 에 조용히 실리지 않는지 지키는 테스트.

2026-10-13 packet 은 파마리서치의 10-12 봉이 없는 채로 만들어졌다. "최근 1개
확정 세션 수익률"은 10-07→10-08 을 가리켰고(실제 최근 세션은 10-12),
`events.benchmark_calendar_gaps` 는 `[]` 였다 — 종목과 벤치마크가 같은 날을 함께
잃어서 둘끼리의 대조로는 보이지 않았다. 그날을 가진 것은 코스피 지수뿐이었다.

여기서 지키는 성질은 둘이다.
  1. 같은 거래소 달력의 다른 시계열에 있는 최근 거래일이 이 시계열에 없으면 말한다
  2. 그 구멍에 걸친 세션 수익률은 내지 않는다 — 세션 수가 틀린 값이 된다
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from cio.context import _apply_session_gaps, _price_summary, _session_gaps

HEADER = (
    "# schema_version price-series/2.1.0\n# symbol {symbol} / currency KRW\n"
    "# retrieved_at 2026-10-13T00:37:14+00:00 / source fixture\n"
    "# last_bar 2026-10-13 / session UNSETTLED\n"
    "{extra}"
    "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
)
SESSIONS = ["2026-10-02", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-12", "2026-10-13"]


def write(root: Path, name: str, symbol: str, dates, *, extra: str = "") -> None:
    path = root / "prices" / name
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        HEADER.format(symbol=symbol, extra=extra)
        + "".join(f"{day},{100 + index},{100 + index},1,101,99,10\n"
                  for index, day in enumerate(dates)),
        encoding="utf-8",
    )


class TestSessionGaps(unittest.TestCase):
    def _gaps(self, *, asset_dates, kq_dates, ks_dates, extra=""):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "214450_KQ.csv", "214450.KQ", asset_dates, extra=extra)
            write(root, "KQ11.csv", "^KQ11", kq_dates)
            write(root, "KS11.csv", "^KS11", ks_dates)
            with patch("cio.context.KNOWLEDGE_DIR", root), patch("cio.context.ROOT", root):
                gaps = _session_gaps("214450.KQ", "KR")
                summary = _price_summary("214450.KQ")[0]
        return gaps, summary

    def test_a_day_only_another_index_kept_is_reported(self):
        without = [day for day in SESSIONS if day != "2026-10-12"]
        gaps, _ = self._gaps(asset_dates=without, kq_dates=without, ks_dates=SESSIONS)
        assert gaps is not None
        self.assertEqual(gaps["dates"], ["2026-10-12"])
        self.assertEqual(gaps["checked_against"], ["^KS11", "^KQ11"])

    def test_a_complete_series_reports_an_empty_list_not_silence(self):
        gaps, _ = self._gaps(asset_dates=SESSIONS, kq_dates=SESSIONS, ks_dates=SESSIONS)
        assert gaps is not None
        self.assertEqual(gaps["dates"], [])
        self.assertNotIn("_warning", gaps)

    def test_a_recorded_missing_session_is_reported_when_every_series_lost_it(self):
        without = [day for day in SESSIONS if day != "2026-10-12"]
        gaps, _ = self._gaps(
            asset_dates=without, kq_dates=without, ks_dates=without,
            extra="# missing_sessions 2026-10-12 / 봉이 저장됐다가 사라졌다\n")
        assert gaps is not None
        self.assertEqual(gaps["dates"], ["2026-10-12"])

    def test_a_reference_running_ahead_is_not_a_gap(self):
        """지수가 종목보다 하루 더 수집됐다고 종목에 구멍이 난 것은 아니다."""
        gaps, _ = self._gaps(asset_dates=SESSIONS[:-1], kq_dates=SESSIONS, ks_dates=SESSIONS)
        assert gaps is not None
        self.assertEqual(gaps["dates"], [])

    def test_session_returns_across_a_gap_are_withheld(self):
        without = [day for day in SESSIONS if day != "2026-10-12"]
        gaps, summary = self._gaps(asset_dates=without, kq_dates=without, ks_dates=SESSIONS)
        assert gaps is not None
        assert summary is not None
        self.assertIn("1", summary["settled_session_returns"]["windows"])
        _apply_session_gaps(summary, gaps)
        returns = summary["settled_session_returns"]
        self.assertEqual(returns["windows"], {}, "구멍을 건너뛴 창이 그대로 실렸다")
        self.assertIn("2026-10-12", returns["_withheld"])
        self.assertEqual(summary["session_gaps"]["dates"], ["2026-10-12"])
        self.assertIn("_warning", summary["session_gaps"])

    def test_an_old_gap_does_not_withhold_recent_windows(self):
        dates = ["2026-09-28", "2026-09-29", "2026-09-30", "2026-10-01", *SESSIONS]
        without = [day for day in dates if day != "2026-09-29"]
        gaps, summary = self._gaps(asset_dates=without, kq_dates=dates, ks_dates=dates)
        assert gaps is not None
        assert summary is not None
        self.assertEqual(gaps["dates"], ["2026-09-29"])
        _apply_session_gaps(summary, gaps)
        self.assertEqual(
            sorted(summary["settled_session_returns"]["windows"]), ["1", "2", "3"])

    def test_a_market_without_a_shared_calendar_is_not_checked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write(root, "214450_KQ.csv", "214450.KQ", SESSIONS)
            with patch("cio.context.KNOWLEDGE_DIR", root), patch("cio.context.ROOT", root):
                self.assertIsNone(_session_gaps("214450.KQ", "JP"))


if __name__ == "__main__":
    unittest.main()
