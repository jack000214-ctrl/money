"""Peer 비교가 비연속 분기를 TTM으로 합치지 않는지 검증한다."""

import unittest

from peer_compare import align_period, periods, ttm


def point(fy, fp, start, end, value, form="10-Q", filed="2026-01-01"):
    return {
        "fy": fy, "fp": fp, "start": start, "end": end, "val": value,
        "form": form, "filed": filed,
    }


class TestPeerTtm(unittest.TestCase):
    def test_continuous_fiscal_year_is_ttm(self):
        points = [
            point(2025, "Q1", "2025-01-01", "2025-03-31", 10),
            point(2025, "Q2", "2025-01-01", "2025-06-30", 25),
            point(2025, "Q3", "2025-01-01", "2025-09-30", 45),
            point(2025, "FY", "2025-01-01", "2025-12-31", 70, "10-K"),
        ]
        value, _, basis = ttm(points)
        self.assertEqual(value, 70)
        self.assertEqual(basis, "TTM")

    def test_missing_quarter_falls_back_to_fy_not_fake_ttm(self):
        points = [
            point(2025, "Q1", "2025-01-01", "2025-03-31", 10),
            point(2025, "Q2", "2025-01-01", "2025-06-30", 25),
            point(2025, "FY", "2025-01-01", "2025-12-31", 70, "10-K"),
        ]
        value, _, basis = ttm(points)
        self.assertEqual(value, 70)
        self.assertEqual(basis, "FY")

    def test_latest_amendment_wins_for_same_period(self):
        points = [
            point(2025, "Q1", "2025-01-01", "2025-03-31", 10, filed="2025-05-01"),
            point(2025, "Q1", "2025-01-01", "2025-03-31", 12, filed="2025-05-20"),
        ]
        self.assertEqual(periods(points, 80, 100)[0]["val"], 12)

    def test_metrics_from_different_basis_are_not_mixed(self):
        self.assertIsNone(
            align_period(20, "2025-12-31", "FY", "2025-12-31", "TTM")
        )
        self.assertIsNone(
            align_period(20, "2025-09-30", "TTM", "2025-12-31", "TTM")
        )
        self.assertEqual(
            align_period(20, "2025-12-31", "TTM", "2025-12-31", "TTM"),
            20,
        )


if __name__ == "__main__":
    unittest.main()
