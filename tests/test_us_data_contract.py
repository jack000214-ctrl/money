"""미국 종목은 무엇을 받을 수 있고 무엇을 못 받는지 말해야 한다.

`config/assets.json` 에서 US STOCK 의 `optional_collectors` 가 전부 비어
있었다. 한국 종목은 수급·대차·재고·부문·정상화이익을 받는데 미국은 하나도
없고, **그 사실이 packet 어디에도 없었다.** 특히 `NORMALIZED_EARNINGS` 가
없는 것이 치명적이다 — 직전 세션에서 두 판단 모두 결론을 뒤집은 입력이
그것이었다(028050 은 BUY_NOW 가 WAIT 이 됐고, 005930 은 "FCF 가 사이클
최고점"이라는 판정 전체가 거기서 나왔다).

재무 계약도 마찬가지다. IBM 표본은 영업이익이 통째로 비어 있는데 계약을
통과했다 — 사실상 OCF·capex·FCF·순부채 항등식만 봤기 때문이다. **0 이 아니라
결측인데 계약이 아무 말도 하지 않으면 그 공백은 없는 것이 된다.**

없는 것을 만들어 내라는 뜻이 아니다. 없다고 말하라는 뜻이다.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculators.contracts import fundamentals_id_for, validate_fundamentals
from cio.assets import OPTIONAL_COLLECTORS_BY_ROUTE, get_asset
from cio.context import _data_coverage
from test_fundamentals import valid_fundamentals


def _with_income(income):
    data = valid_fundamentals()
    data["income_statement"] = income
    data["fundamentals_id"] = fundamentals_id_for(data)
    return data


def _line(**overrides):
    row = {
        "tag": "Revenues",
        "ttm": 69_095e6,
        "quarters": [{"fy": 2026, "quarter": 1, "end": "2026-03-31",
                      "val": 17e9, "derived": False}],
    }
    row.update(overrides)
    return row


class TestSilentlyEmptyIncomeLines(unittest.TestCase):
    def test_a_line_with_no_data_and_no_reason_is_rejected(self):
        """IBM 의 operating_income 이 그랬다. 이유 없는 빈칸은 계약 위반이다."""
        data = _with_income(
            {"revenue": _line(),
             "operating_income": {"tag": None, "ttm": None, "quarters": []}}
        )
        errors = validate_fundamentals(data)
        self.assertTrue(
            any("operating_income" in error for error in errors), errors
        )

    def test_a_declared_absence_passes(self):
        """왜 없는지 적으면 그것은 결측이 아니라 관측된 부재다."""
        data = _with_income(
            {
                "revenue": _line(),
                "operating_income": {
                    "tag": None,
                    "ttm": None,
                    "quarters": [],
                    "absent_reason": (
                        "us-gaap:OperatingIncomeLoss 를 공시하지 않는다"
                    ),
                },
            }
        )
        self.assertEqual(validate_fundamentals(data), [])

    def test_a_line_with_data_passes(self):
        self.assertEqual(validate_fundamentals(_with_income({"revenue": _line()})), [])

    def test_a_snapshot_without_an_income_statement_is_unchanged(self):
        """손익계산서를 아예 싣지 않는 판본까지 소급해 깨지 않는다."""
        self.assertEqual(validate_fundamentals(valid_fundamentals()), [])


class TestUsStocksCanRegisterCollectors(unittest.TestCase):
    def test_normalized_earnings_is_allowed_for_us_stocks(self):
        self.assertIn(
            "NORMALIZED_EARNINGS", OPTIONAL_COLLECTORS_BY_ROUTE[("US", "STOCK")]
        )

    def test_the_registered_us_stocks_actually_use_it(self):
        for asset_id in ("TSLA", "IBM"):
            with self.subTest(asset_id=asset_id):
                self.assertIn(
                    "NORMALIZED_EARNINGS",
                    get_asset(asset_id)["optional_collectors"],
                )


class TestCoverageIsStatedNotImplied(unittest.TestCase):
    """가용성과 지연을 명시적으로 관리한다. 조용히 비어 있으면 안 된다."""

    STATES = {
        "AVAILABLE", "REGISTERED_NOT_COLLECTED", "NOT_REGISTERED",
        "NO_COLLECTOR_FOR_MARKET", "NOT_OBSERVABLE",
    }

    def _coverage(self, asset_id, **kwargs):
        return _data_coverage(get_asset(asset_id), **kwargs)

    def test_every_row_has_a_known_state(self):
        coverage = self._coverage("IBM", structured_optional={}, fundamentals={})
        rows = {key: row for key, row in coverage.items()
                if not key.startswith("_")}
        self.assertTrue(rows)
        for key, row in rows.items():
            with self.subTest(key=key):
                self.assertIn(row["state"], self.STATES)

    def test_us_forward_estimates_say_there_is_no_collector(self):
        coverage = self._coverage("IBM", structured_optional={}, fundamentals={})
        self.assertEqual(
            coverage["forward_estimates"]["state"], "NO_COLLECTOR_FOR_MARKET"
        )

    def test_kr_forward_estimates_are_available_when_present(self):
        coverage = self._coverage(
            "214450",
            structured_optional={"consensus": {"as_of": "2026-08-06"}},
            fundamentals={},
        )
        self.assertEqual(coverage["forward_estimates"]["state"], "AVAILABLE")
        self.assertEqual(coverage["forward_estimates"]["as_of"], "2026-08-06")

    def test_kr_forward_estimates_registered_but_missing(self):
        coverage = self._coverage("214450", structured_optional={}, fundamentals={})
        self.assertEqual(
            coverage["forward_estimates"]["state"], "REGISTERED_NOT_COLLECTED"
        )

    def test_segments_registered_for_one_asset_and_not_another(self):
        self.assertEqual(
            self._coverage("005930", structured_optional={}, fundamentals={})[
                "segment_revenue"
            ]["state"],
            "REGISTERED_NOT_COLLECTED",
        )
        self.assertEqual(
            self._coverage("214450", structured_optional={}, fundamentals={})[
                "segment_revenue"
            ]["state"],
            "NOT_REGISTERED",
        )

    def test_dilution_reports_the_share_count_basis(self):
        coverage = self._coverage(
            "005930",
            structured_optional={},
            fundamentals={
                "values": {"shares_diluted": 6_675_338_718.0},
                "eps_basis": "BASIC_NO_DILUTED_DISCLOSED",
            },
        )
        row = coverage["dilution"]
        self.assertEqual(row["state"], "AVAILABLE")
        self.assertEqual(row["basis"], "BASIC_NO_DILUTED_DISCLOSED")
        self.assertIn("추이", row["note"])

    def test_peer_multiples_without_registered_peers(self):
        coverage = self._coverage("005930", structured_optional={}, fundamentals={})
        self.assertEqual(coverage["peer_multiples"]["state"], "NOT_REGISTERED")

    def test_coverage_makes_no_judgment(self):
        coverage = self._coverage("IBM", structured_optional={}, fundamentals={})
        blob = repr(coverage)
        for word in ("싸다", "비싸다", "매수", "매도", "저평가", "고평가"):
            self.assertNotIn(word, blob)


if __name__ == "__main__":
    unittest.main()
