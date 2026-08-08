"""표에 찍히는 값의 계약.

화면은 사람만 보는 것이 아니다 — 이 저장소의 CLI 를 부르는 것은 모델이고,
실제로 한 판단이 `kr_inventory` 의 DIO, `kr_flow` 의 순매수, `normalized_earnings`
의 배수를 전부 렌더링 출력에서 가져다 썼다. 표에 찍히는 숫자는 장식이 아니라
**입력**이다.

여기서 고정하는 것은 미관이 아니라 셋이다.
  1. 없는 값이 **크래시**를 만들지 않는다
  2. 없는 값이 **0 으로 둔갑**하지 않는다 (이쪽이 더 나쁘다 — 조용하다)
  3. 값이 칸을 넘어 옆 칸과 붙지 않는다
"""

import unittest

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.display import ABSENT, cell, display_width, pad


class TestAbsentValues(unittest.TestCase):
    def test_none_does_not_raise_where_a_format_spec_would(self):
        """`f"{None:>7.1f}"` 는 TypeError 다. 실제로 컨센서스 표 전체가 이걸로 죽었다."""
        self.assertEqual(cell(None, 7, ".1f").strip(), ABSENT)

    def test_absent_is_visually_distinct_from_zero(self):
        """'적자라 PER 이 없다'와 'PER 0배'는 다른 사건이다."""
        self.assertNotEqual(cell(None, 7, ".1f"), cell(0, 7, ".1f"))
        self.assertIn("0.0", cell(0, 7, ".1f"))
        self.assertNotIn("0", cell(None, 7, ".1f"))

    def test_absent_still_occupies_the_column(self):
        """빈칸이 좁으면 그 행만 열이 밀린다."""
        self.assertEqual(len(cell(None, 9, ",.0f")), 9)

    def test_zero_is_a_value_not_an_absence(self):
        self.assertEqual(cell(0.0, 6, ",.0f").strip(), "0")


class TestFormatting(unittest.TestCase):
    def test_spec_and_unit_are_applied(self):
        self.assertEqual(cell(12.345, 8, ".1f", "%").strip(), "12.3%")

    def test_signed_spec_keeps_the_plus(self):
        self.assertEqual(cell(4.0, 8, "+.1f", "%").strip(), "+4.0%")

    def test_scale_converts_before_formatting(self):
        """호출부에서 `value / 1e8` 을 먼저 하면 그 자리에서 None 이 터진다."""
        self.assertEqual(cell(3_276_570_000_000, 12, ",.0f", scale=1e8).strip(),
                         "32,766")

    def test_scale_is_skipped_for_absent_values(self):
        self.assertEqual(cell(None, 12, ",.0f", scale=1e8).strip(), ABSENT)

    def test_negative_values_survive_scaling(self):
        self.assertEqual(cell(-7_730_300_000_000, 12, ",.0f", scale=1e8).strip(),
                         "-77,303")


class TestWidth(unittest.TestCase):
    def test_hangul_counts_as_two_terminal_cells(self):
        """한글을 한 칸으로 세면 헤더가 데이터 행과 어긋난다."""
        self.assertEqual(display_width("매출"), 4)
        self.assertEqual(display_width("YoY"), 3)
        self.assertEqual(display_width("영업익"), 6)

    def test_pad_aligns_by_display_width_not_character_count(self):
        self.assertEqual(display_width(pad("매출", 11)), 11)
        self.assertEqual(display_width(pad("YoY", 11)), 11)

    def test_left_alignment_is_available_for_labels(self):
        padded = pad("연도", 11, align="<")
        self.assertTrue(padded.startswith("연도"))
        self.assertEqual(display_width(padded), 11)

    def test_a_value_wider_than_its_column_is_not_truncated(self):
        """잘라내면 자릿수가 사라져 값 자체가 틀린다. 넘치더라도 온전히 낸다."""
        wide = cell(5_017_411, 6, ",.0f")
        self.assertIn("5,017,411", wide)


class TestTableAlignment(unittest.TestCase):
    def test_a_trillion_won_row_does_not_collide_with_the_next_column(self):
        """조 단위 회사에서 실제로 옆 칸과 붙었다. 어느 숫자가 어느 항목인지 사라진다."""
        row = (cell(3_456_048, 11, ",.0f")
               + cell(255.76, 8, ".1f", "%")
               + cell(2_663_757, 11, ",.0f"))
        self.assertEqual(display_width(row), 30)
        self.assertIn(" 3,456,048", row)
        self.assertIn(" 2,663,757", row)

    def test_mixed_present_and_absent_cells_keep_the_grid(self):
        widths = (11, 8, 11, 10, 7, 7)
        values = (327_657, None, -77_303, -12_517, None, -15.61)
        row = "".join(cell(v, w, ",.0f") for v, w in zip(values, widths))
        self.assertEqual(display_width(row), sum(widths))


if __name__ == "__main__":
    unittest.main()
