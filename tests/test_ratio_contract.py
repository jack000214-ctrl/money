"""변화율·비율의 공통 계약.

같은 산술이 `dart`(한국 분기), `edgar`(미국 분기), `peer_us`(동종 비교)에 따로
쓰여 있었고, 세 곳 모두 기저를 `if base:` 로만 걸렀다. 그 검사는 **음수를
통과시킨다** — 적자에서 흑자로 돌아선 해의 "전년 대비 -250%" 가 그렇게 나온다.
성장률처럼 생겼지만 성장률이 아니고, 그럴듯해서 화면에서 걸러지지 않는다.

한 곳으로 모은 이유는 유지비가 아니라 **한 곳에서만 틀리게 하기 위해서**다.
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import change_pct, ratio_pct


class TestChangePct(unittest.TestCase):
    def test_growth_from_a_positive_base(self):
        self.assertAlmostEqual(change_pct(79_318_746, 22_231_952), 256.78, places=2)

    def test_a_fall_is_negative(self):
        self.assertEqual(change_pct(50, 100), -50.0)

    def test_no_change_is_zero_not_none(self):
        self.assertEqual(change_pct(100, 100), 0.0)

    def test_a_zero_base_yields_nothing(self):
        """0 으로 나눌 수 없고, 채우면 무한대가 성장률로 찍힌다."""
        self.assertIsNone(change_pct(100, 0))

    def test_a_negative_base_yields_nothing(self):
        """적자에서 흑자로 돌아선 해의 대비는 부호가 뒤집힌다.

        -100 → +100 을 그대로 계산하면 -200% 가 나온다. 이익이 늘었는데
        마이너스 성장률이 찍히고, 아무도 그 부호를 의심하지 않는다.
        """
        self.assertIsNone(change_pct(100, -100))

    def test_missing_inputs_yield_nothing(self):
        self.assertIsNone(change_pct(None, 100))
        self.assertIsNone(change_pct(100, None))

    def test_non_numeric_inputs_do_not_raise(self):
        self.assertIsNone(change_pct("100", 50))
        self.assertIsNone(change_pct(100, "50"))

    def test_a_negative_current_over_a_positive_base_is_a_real_observation(self):
        """흑자에서 적자로 간 것은 관측이다. 이쪽은 비워선 안 된다."""
        self.assertEqual(change_pct(-50, 100), -150.0)


class TestRatioPct(unittest.TestCase):
    def test_margin_from_revenue(self):
        self.assertAlmostEqual(
            ratio_pct(60_542_608, 79_318_746), 76.33, places=2
        )

    def test_an_operating_loss_gives_a_negative_margin(self):
        """영업손실은 실제로 일어나는 관측이다. 분자는 음수여도 된다."""
        self.assertEqual(ratio_pct(-200, 1000), -20.0)

    def test_a_zero_denominator_yields_nothing(self):
        self.assertIsNone(ratio_pct(100, 0))

    def test_a_negative_denominator_yields_nothing(self):
        """매출이 음수인 회사는 없다. 나왔다면 데이터 오류이지 관측이 아니다."""
        self.assertIsNone(ratio_pct(100, -1000))

    def test_missing_inputs_yield_nothing(self):
        self.assertIsNone(ratio_pct(None, 1000))
        self.assertIsNone(ratio_pct(100, None))

    def test_zero_numerator_is_a_real_zero(self):
        self.assertEqual(ratio_pct(0, 1000), 0.0)


class TestSharedByEveryCaller(unittest.TestCase):
    """세 모듈이 같은 함수를 쓰는지 — 한 곳만 고치고 나머지를 두면 의미가 없다."""

    def test_dart_edgar_and_peer_us_import_the_shared_helpers(self):
        from calculators import peer_us
        from collectors import dart, edgar

        for module in (dart, edgar, peer_us):
            with self.subTest(module.__name__):
                self.assertIs(module.change_pct, change_pct)
        for module in (dart, edgar, peer_us):
            with self.subTest(module.__name__ + ".ratio"):
                self.assertIs(module.ratio_pct, ratio_pct)


if __name__ == "__main__":
    unittest.main()
