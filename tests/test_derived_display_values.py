"""화면에서만 만들어지던 파생값들의 산술 계약.

이 값들은 원래 `print` 직전에 계산돼 어디에도 저장되지 않았다. 저장되지 않으니
대조할 대상이 없었고, 틀려도 조용했다. 그런데 이 저장소의 CLI 를 부르는 것은
모델이고, 모델은 이 출력을 근거로 읽는다 — 실제로 한 판단이 여기서 나온
"고점 대비 -51.3%", "요구 FCF 성장률 12.05%" 를 그대로 인용했다.

여기서 고정하는 공통 성질은 하나다. **모르면 지어내지 않는다.**
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.valuation_band import percentile_of, target_prices
from collectors.price import band


def series(*pairs):
    return [{"date": d, "close": str(c)} for d, c in pairs]


class TestPriceBand(unittest.TestCase):
    def test_drawdown_is_measured_from_the_window_high(self):
        rows = series(("2026-06-22", 2919000), ("2026-08-07", 1422000))
        self.assertEqual(band(rows, "000660.KS")["drawdown_pct"], -51.28)

    def test_position_is_where_in_the_range_the_close_sits(self):
        rows = series(("2026-01-01", 100), ("2026-01-02", 200),
                      ("2026-01-03", 150))
        self.assertEqual(band(rows, "X")["position_pct"], 50.0)

    def test_a_flat_window_does_not_claim_a_position(self):
        """고가와 저가가 같으면 위치라는 것이 아예 없다.

        0 이나 100 으로 두면 거래 정지 구간이 '52주 최저'로 읽힌다. 그래서
        예전에는 50 을 넣었는데, 그것도 '구간 중간'이라는 없는 정보다. 세
        값 다 지어낸 것이고 답은 넷째다 — 범위가 없으면 위치도 없다.
        """
        rows = series(("2026-01-01", 100), ("2026-01-02", 100))
        self.assertIsNone(band(rows, "X")["position_pct"])

    def test_cost_return_appears_only_when_a_cost_is_given(self):
        rows = series(("2026-08-07", 1422000))
        self.assertNotIn("return_from_cost_pct", band(rows, "X"))
        self.assertEqual(
            band(rows, "X", cost=2000000)["return_from_cost_pct"], -28.9
        )

    def test_the_window_edges_are_reported_with_the_numbers(self):
        """어느 구간의 낙폭인지 없으면 -51% 가 무엇 대비인지 알 수 없다."""
        rows = series(("2026-06-22", 2919000), ("2026-08-07", 1422000))
        result = band(rows, "000660.KS")
        self.assertEqual(result["from"], "2026-06-22")
        self.assertEqual(result["to"], "2026-08-07")
        self.assertEqual(result["high"], 2919000.0)


class TestPercentileOf(unittest.TestCase):
    def test_share_of_history_below_the_current_value(self):
        self.assertEqual(percentile_of([1.0, 2.0, 3.0, 4.0], 3.0), 50.0)

    def test_the_lowest_ever_is_zero_percent(self):
        self.assertEqual(percentile_of([2.0, 3.0, 4.0], 1.0), 0.0)

    def test_an_empty_history_has_no_percentile_at_all(self):
        """0 으로 나누지 않는 것과 50 을 지어내는 것은 다른 일이다.

        분포가 없는 상태에 50 을 돌려주면 화면에 `현재 백분위 50.0%` 가 찍히고
        막대의 점도 한가운데 놓인다. 이 저장소의 출력을 읽는 것은 모델이므로
        그것이 그대로 근거가 된다 — 없는 분포가 '중간'이라는 관측이 된다.
        """
        self.assertIsNone(percentile_of([], 5.0))


class TestTargetPrices(unittest.TestCase):
    def quantiles(self):
        # 1~99 백분위. 값은 백분위와 같게 두어 색인 오차가 바로 보이게 한다.
        return [float(i) for i in range(1, 100)]

    def test_a_percentile_maps_to_its_quantile_not_its_neighbour(self):
        """색인이 한 칸 밀리면 25백분위가 24백분위 값으로 찍힌다."""
        rows = target_prices(self.quantiles(), [25.0], eps=100.0,
                             current_price=2000.0)
        self.assertEqual(rows[0]["per"], 25.0)
        self.assertEqual(rows[0]["price"], 2500.0)
        self.assertEqual(rows[0]["vs_current_pct"], 25.0)

    def test_out_of_range_percentiles_are_refused_not_silently_medianed(self):
        """예전에는 0·100 을 말없이 중앙값으로 바꿨다.

        사용자는 0백분위를 물었는데 화면에는 50백분위 값이 '0백분위'라는
        라벨을 달고 앉았다. 라벨과 값이 다른 표는 표가 아니다.
        """
        rows = target_prices(self.quantiles(), [0.0, 100.0], eps=100.0,
                             current_price=2000.0)
        for row in rows:
            with self.subTest(row["label"]):
                self.assertIsNone(row["per"])
                self.assertIsNone(row["price"])
                self.assertIn("1~99", row["note"])

    def test_a_negative_eps_does_not_produce_a_negative_target_price(self):
        """적자 회사에 PER 배수를 곱하면 산술은 되지만 뜻이 없다."""
        rows = target_prices(self.quantiles(), [50.0], eps=-100.0,
                             current_price=2000.0)
        self.assertIsNone(rows[0]["price"])
        self.assertIsNone(rows[0]["vs_current_pct"])
        self.assertIn("EPS", rows[0]["note"])
        # PER 자체는 관측이므로 남긴다.
        self.assertEqual(rows[0]["per"], 50.0)

    def test_a_zero_eps_is_refused_too(self):
        rows = target_prices(self.quantiles(), [50.0], eps=0.0,
                             current_price=2000.0)
        self.assertIsNone(rows[0]["price"])

    def test_a_zero_current_price_leaves_the_comparison_empty(self):
        rows = target_prices(self.quantiles(), [50.0], eps=100.0,
                             current_price=0.0)
        self.assertEqual(rows[0]["price"], 5000.0)
        self.assertIsNone(rows[0]["vs_current_pct"])

    def test_the_label_always_says_what_was_asked(self):
        rows = target_prices(self.quantiles(), [10.0, 0.0], eps=100.0,
                             current_price=2000.0)
        self.assertEqual([r["label"] for r in rows], ["10백분위", "0백분위"])


if __name__ == "__main__":
    unittest.main()
