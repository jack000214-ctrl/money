"""분기 환율표가 packet 에 실리는 자리의 계약.

산술은 `test_fx_periods.py` 가 지킨다. 여기서 지키는 것은 배선이다.
  1. 표는 **이 회사의 결산 분기**에 맞춰진다 — 실적표가 끝난 다음 분기는 미발표다
  2. 수출 비중은 품목군을 내수·수출로 나눠 공시하는 회사에서만 나온다
  3. 재료가 없으면 근사하지 않고 블록을 싣지 않는다
"""

import datetime as dt
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401
from cio.context import _export_share, _fx_quarters


def write_fx(root: Path, rows) -> None:
    path = root / "prices" / "KRWX.csv"
    path.parent.mkdir(exist_ok=True)
    path.write_text(
        "# schema_version price-series/2.1.0\n# symbol KRW=X / currency KRW\n"
        "# retrieved_at 2026-10-13T01:17:54+00:00 / source fixture\n"
        "# last_bar 2026-10-13 / session UNSETTLED\n"
        "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
        + "".join(f"{day},{close},{close},1,{close},{close},0\n" for day, close in rows),
        encoding="utf-8",
    )


def weekdays(start: str, end: str, close: float):
    day, last = dt.date.fromisoformat(start), dt.date.fromisoformat(end)
    out = []
    while day <= last:
        if day.weekday() < 5:
            out.append((day.isoformat(), close))
        day += dt.timedelta(days=1)
    return out


FX = (
    weekdays("2026-01-01", "2026-03-31", 1460.0)
    + weekdays("2026-04-01", "2026-06-30", 1500.0)
    + weekdays("2026-07-01", "2026-09-30", 1420.0)
    + weekdays("2026-10-01", "2026-10-13", 1336.0)
)
FUNDAMENTALS = {
    "earnings_bridge": {
        "covered_through": "2026-06-30",
        "quarter_ends_after_covered": ["2026-09-30"],
        "rows": [{"end": "2026-03-31"}, {"end": "2026-06-30"}],
    }
}
PRODUCT_SALES = {
    "items": ["의료기기(내수)", "의료기기(수출)", "화장품(내수)", "화장품(수출)"],
    "quarters": [
        {"period": "2025Q2", "values": [600, 240, 110, 190], "sum": 1140,
         "reconciliation": "RECONCILED"},
        {"period": "2026Q2", "values": [640, 320, 160, 430], "sum": 1550,
         "reconciliation": "RECONCILED"},
    ],
}


class TestFxBlock(unittest.TestCase):
    def _block(self, fundamentals=FUNDAMENTALS, product_sales=PRODUCT_SALES, rows=FX):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            if rows is not None:
                write_fx(root, rows)
            with patch("cio.context.KNOWLEDGE_DIR", root), patch("cio.context.ROOT", root):
                return _fx_quarters(fundamentals, product_sales)

    def test_the_quarter_after_the_earnings_table_is_marked_unreported(self):
        block, source = self._block()
        assert block is not None
        flags = {row["end"]: row["reported"] for row in block["quarters"]}
        self.assertEqual(flags, {
            "2026-03-31": True, "2026-06-30": True, "2026-09-30": False})
        assert block["latest_vs_last_reported_quarter"] is not None
        self.assertEqual(block["latest_vs_last_reported_quarter"]["end"], "2026-06-30")
        self.assertEqual(block["as_of"], "2026-10-13")
        self.assertEqual(source["source_id"], "PRICE:KRW=X")

    def test_the_block_says_what_it_does_not_contain(self):
        block, _ = self._block()
        assert block is not None
        self.assertIn("환위험", block["_role"])
        self.assertIn("reported:false", block["_role"])

    def test_without_an_earnings_table_there_is_no_block(self):
        block, _ = self._block(fundamentals={})
        self.assertIsNone(block)
        block, _ = self._block(fundamentals=None)
        self.assertIsNone(block)

    def test_without_the_fx_series_there_is_no_block(self):
        block, source = self._block(rows=None)
        self.assertIsNone(block)
        self.assertEqual(source["source_id"], "PRICE:KRW=X")

    def test_export_share_travels_with_the_table(self):
        block, _ = self._block()
        assert block is not None
        assert block["export_share"] is not None
        self.assertEqual(block["export_share"]["period"], "2026Q2")
        self.assertEqual(block["export_share"]["pct"], round(750 / 1550 * 100, 1))
        self.assertEqual(block["export_share"]["year_ago_pct"], round(430 / 1140 * 100, 1))


class TestExportShare(unittest.TestCase):
    def test_a_company_that_does_not_split_export_lines_gets_none(self):
        unsplit = {"items": ["의료기기", "화장품"], "quarters": [
            {"period": "2026Q2", "values": [900, 600], "sum": 1500,
             "reconciliation": "RECONCILED"}]}
        self.assertIsNone(_export_share(unsplit))

    def test_all_export_lines_is_not_a_share(self):
        """전부 '수출'이면 나눠 공시한 것이 아니다. 100% 를 지어내지 않는다."""
        only = {"items": ["A(수출)", "B(수출)"], "quarters": [
            {"period": "2026Q2", "values": [900, 600], "sum": 1500,
             "reconciliation": "RECONCILED"}]}
        self.assertIsNone(_export_share(only))

    def test_an_unreconciled_quarter_gives_no_share(self):
        broken = {**PRODUCT_SALES, "quarters": [
            {**PRODUCT_SALES["quarters"][-1], "reconciliation": "MISMATCH"}]}
        self.assertIsNone(_export_share(broken))

    def test_a_missing_year_ago_quarter_leaves_that_field_empty(self):
        latest_only = {**PRODUCT_SALES, "quarters": PRODUCT_SALES["quarters"][-1:]}
        share = _export_share(latest_only)
        assert share is not None
        self.assertEqual(share["pct"], round(750 / 1550 * 100, 1))
        self.assertIsNone(share["year_ago_pct"])

    def test_an_absent_or_unavailable_summary_gives_none(self):
        self.assertIsNone(_export_share(None))
        self.assertIsNone(_export_share({"available": False}))


if __name__ == "__main__":
    unittest.main()
