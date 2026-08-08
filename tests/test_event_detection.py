"""급변일 선별의 산술 계약.

`events.moves` 는 모델이 "무슨 일이 있었는지 조사할 날"로 읽는 목록이다. 이
목록에서 빠진 날은 **모델에게 존재하지 않는다** — 화면에도 packet 에도 남지
않으므로 빠졌다는 사실조차 관측되지 않는다.

이 산술은 원래 packet 을 만드는 루프 안에서만 계산돼 어디에도 저장되지
않았다. 대조할 대상이 없으니 틀려도 조용했다. 여기서 고정하는 것은 넷이다.

  1. 임계값은 **그 날 이전** 구간으로만 만든다 (미래를 쓰지 않는다)
  2. 벤치마크가 있으면 초과변동으로, 없으면 절대변동으로 판정한다
  3. 거래량 배수의 분모가 0 이면 배수를 지어내지 않는다
  4. 직전 종가가 0 이면 변화율을 만들지 않는다
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors.kr_events import detect_moves, stdev


def bars(*rows):
    """(날짜, 종가, 거래량)"""
    return [(f"2026-01-{day:02d}", float(close), int(volume))
            for day, close, volume in rows]


def run(window, *, judged=None, bench=None, sigma=2.0, fixed=None,
        avg_volume=1000.0, vol_window=60):
    dates = [d for d, _, _ in window]
    return detect_moves(
        window,
        judged_series=judged if judged is not None else {},
        dates=dates,
        index_of={d: i for i, d in enumerate(dates)},
        aligned=(lambda date, prev: (bench or {}).get(date)),
        avg_volume=avg_volume,
        sigma=sigma,
        fixed=fixed,
        vol_window=vol_window,
    )


class TestChangeAndDirection(unittest.TestCase):
    def test_change_is_measured_against_the_previous_trading_day(self):
        moves = run(bars((5, 100, 1000), (6, 110, 1000)), fixed=1.0)
        self.assertEqual(moves[0]["change_pct"], 10.0)
        self.assertEqual(moves[0]["direction"], "up")

    def test_a_fall_is_marked_down(self):
        moves = run(bars((5, 100, 1000), (6, 85, 1000)), fixed=1.0)
        self.assertEqual(moves[0]["change_pct"], -15.0)
        self.assertEqual(moves[0]["direction"], "down")

    def test_a_zero_previous_close_produces_no_move_instead_of_infinity(self):
        """상장 첫날이나 데이터 구멍에서 0 으로 나누면 안 된다."""
        self.assertEqual(run(bars((5, 0, 1000), (6, 100, 1000)), fixed=1.0), [])

    def test_the_first_bar_is_never_a_move(self):
        """직전 거래일이 없으면 변화율이 성립하지 않는다."""
        moves = run(bars((5, 100, 1000)), fixed=1.0)
        self.assertEqual(moves, [])


class TestThreshold(unittest.TestCase):
    def test_a_fixed_threshold_filters_smaller_moves(self):
        window = bars((5, 100, 1000), (6, 103, 1000), (7, 120, 1000))
        moves = run(window, fixed=10.0)
        self.assertEqual([m["date"] for m in moves], ["2026-01-07"])

    def test_the_adaptive_threshold_uses_only_days_before_the_move(self):
        """미래를 쓰면 나중에 조용해진 구간의 급변이 사라진다.

        조용한 앞 구간 뒤에 요동치는 뒤 구간을 두고, 앞 구간의 급변이 뒤
        구간의 변동성으로 지워지지 않는지 본다.
        """
        window = bars(
            (1, 100, 1000), (2, 101, 1000), (3, 100, 1000), (4, 101, 1000),
            (5, 110, 1000),                     # 조용한 구간 뒤의 +9%
            (6, 150, 1000), (7, 100, 1000),     # 이후의 큰 요동
        )
        judged = {d: v for d, v in zip(
            [f"2026-01-{i:02d}" for i in range(2, 8)],
            [1.0, -1.0, 1.0, 9.0, 36.0, -33.0],
        )}
        moves = run(window, judged=judged, sigma=2.0, vol_window=60)
        self.assertIn("2026-01-05", [m["date"] for m in moves])

    def test_the_threshold_never_falls_below_one_percent(self):
        """변동성이 0 인 구간에서 문턱이 0 이 되면 모든 날이 급변이 된다."""
        window = bars((5, 100, 1000), (6, 100.1, 1000))
        judged = {"2026-01-05": 0.0, "2026-01-06": 0.0}
        self.assertEqual(run(window, judged=judged, sigma=2.0), [])

    def test_the_threshold_used_is_reported_with_the_move(self):
        """어떤 문턱으로 골랐는지가 남아야 다음 판단이 이 목록을 읽을 수 있다."""
        moves = run(bars((5, 100, 1000), (6, 130, 1000)), fixed=7.5)
        self.assertEqual(moves[0]["threshold_pct"], 7.5)


class TestBenchmarkRelative(unittest.TestCase):
    def test_excess_is_the_move_minus_the_benchmark(self):
        moves = run(
            bars((5, 100, 1000), (6, 110, 1000)),
            bench={"2026-01-06": 4.0},
            fixed=1.0,
        )
        self.assertEqual(moves[0]["benchmark_pct"], 4.0)
        self.assertEqual(moves[0]["excess_pct"], 6.0)

    def test_a_market_wide_move_is_not_flagged_as_a_stock_event(self):
        """지수가 같이 빠진 날을 종목 사건으로 올리면 조사 순서가 망가진다."""
        moves = run(
            bars((5, 100, 1000), (6, 90, 1000)),
            bench={"2026-01-06": -10.0},
            fixed=5.0,
        )
        self.assertEqual(moves, [])

    def test_without_a_benchmark_the_absolute_move_is_judged(self):
        moves = run(bars((5, 100, 1000), (6, 90, 1000)), fixed=5.0)
        self.assertEqual(moves[0]["change_pct"], -10.0)
        self.assertIsNone(moves[0]["excess_pct"])
        self.assertIsNone(moves[0]["benchmark_pct"])

    def test_a_calendar_gap_falls_back_to_the_absolute_move(self):
        """벤치마크에 그 날이 없으면 상대변동을 지어내지 않고 절대변동으로 본다."""
        moves = run(
            bars((5, 100, 1000), (6, 90, 1000)),
            bench={"2026-01-99": 0.0},
            fixed=5.0,
        )
        self.assertIsNone(moves[0]["excess_pct"])
        self.assertEqual(moves[0]["change_pct"], -10.0)


class TestVolumeMultiple(unittest.TestCase):
    def test_multiple_is_volume_over_the_window_average(self):
        moves = run(bars((5, 100, 1000), (6, 130, 3440)),
                    fixed=1.0, avg_volume=2000.0)
        self.assertEqual(moves[0]["volume_vs_avg"], 1.72)

    def test_zero_average_volume_gives_none_not_a_division_error(self):
        """거래 정지 구간 뒤에서 평균이 0 이 될 수 있다."""
        moves = run(bars((5, 100, 0), (6, 130, 0)), fixed=1.0, avg_volume=0.0)
        self.assertIsNone(moves[0]["volume_vs_avg"])

    def test_raw_volume_is_carried_alongside_the_multiple(self):
        """배수만 남기면 '평소가 얼마였나'를 되짚을 수 없다."""
        moves = run(bars((5, 100, 1000), (6, 130, 5000)), fixed=1.0)
        self.assertEqual(moves[0]["volume"], 5000)


class TestStdev(unittest.TestCase):
    def test_fewer_than_two_points_is_zero_not_an_error(self):
        self.assertEqual(stdev([]), 0.0)
        self.assertEqual(stdev([5.0]), 0.0)

    def test_sample_standard_deviation(self):
        self.assertAlmostEqual(stdev([2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]),
                               2.13809, places=4)


if __name__ == "__main__":
    unittest.main()
