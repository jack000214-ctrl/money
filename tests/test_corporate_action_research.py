import unittest

from cio.context import _research_requirements


class CorporateActionResearch(unittest.TestCase):
    def test_rights_issue_exposes_unvalidated_basis(self):
        asset = {"asset_type": "STOCK", "market": "KR", "analysis_lens": "QUALITY_GROWTH"}
        filings = {"latest_material_events": [{"description": "[기재정정]주요사항보고서(유무상증자결정)"}]}
        requirements = _research_requirements(asset, filings)
        gap = next(r for r in requirements if r["topic"] == "corporate_action_price_and_share_basis")
        self.assertIn("과거 EPS 분모", gap["known_local_gap"])
        self.assertIn("청약", gap["known_local_gap"])

    def test_ordinary_report_does_not_raise_corporate_action(self):
        asset = {"asset_type": "STOCK", "market": "KR", "analysis_lens": "QUALITY_GROWTH"}
        requirements = _research_requirements(asset, {"latest_material_events": [{"description": "잠정실적"}]})
        self.assertFalse(any(r["topic"] == "corporate_action_price_and_share_basis" for r in requirements))


if __name__ == "__main__":
    unittest.main()
