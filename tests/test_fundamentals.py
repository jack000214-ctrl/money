"""밸류에이션 입력 스냅샷 계약 테스트.

이 계층이 없을 때의 구멍: 프로젝트는 "기대수익은 손으로 적을 수 없다"를
내걸지만, `reverse_dcf --fcf 5762000000` 은 그냥 타이핑한 숫자였다.
계산 ID 가 입력의 지문이라 값을 바꾸면 ID 도 바뀌지만, **바뀐 값이 원문과
같은지는 아무도 보지 않았다.** 가장 단단한 자물쇠가 이미 안전한 구간에
걸려 있고 정작 열려 있는 문은 입력이었다.

여기서 지키는 성질은 두 가지다.
  1. 내부 항등식 — FCF = 영업현금흐름 − capex, 각 TTM = 연속 4분기 합
  2. 지문 — 항등식을 다 맞춰 놓고 위조해도 fundamentals_id 가 어긋난다
"""

import json
import tempfile
import unittest
from pathlib import Path

from helpers import ROOT  # noqa: F401
from contracts import (ContractError, fundamentals_id_for, load_fundamentals,
                       validate_fundamentals)


def quarters(values, tag="ocf"):
    """FY2025 Q3 ~ FY2026 Q2 연속 4분기."""
    keys = [(2025, 3), (2025, 4), (2026, 1), (2026, 2)]
    return [{"fy": fy, "quarter": q, "end": f"{fy}-{q * 3:02d}-30",
             "filed": "2026-07-23", "val": float(v), "derived": True}
            for (fy, q), v in zip(keys, values)]


def valid_fundamentals(**overrides):
    ocf = [6238e6, 3813e6, 3937e6, 4697e6]
    capex = [2248e6, 2393e6, 2493e6, 5789e6]
    data = {
        "schema_version": "1.1.0",
        "instrument_id": "TSLA",
        "company_name": "Tesla, Inc.",
        "cik": "0001318605",
        "currency": "USD",
        "period_end": "2026-06-30",
        "created_at": "2026-07-27T14:00:00+09:00",
        "tags": {"ocf": "NetCashProvidedByUsedInOperatingActivities",
                 "capex": "PaymentsToAcquirePropertyPlantAndEquipment"},
        "quarters": {"ocf": quarters(ocf), "capex": quarters(capex)},
        "values": {
            "ocf_ttm": sum(ocf),
            "capex_ttm": sum(capex),
            "fcf_ttm": sum(ocf) - sum(capex),
            "shares_diluted": 3540e6,
            "cash_and_equivalents": 15219e6,
            "short_term_investments": 28305e6,
            "long_term_debt": 7721e6,
            "net_debt": 7721e6 - 15219e6 - 28305e6,
        },
    }
    data.update(overrides)
    data["fundamentals_id"] = fundamentals_id_for(data)
    return data


class TestHappyPath(unittest.TestCase):
    def test_valid_snapshot_passes(self):
        self.assertEqual(validate_fundamentals(valid_fundamentals()), [])

    def test_fcf_is_ocf_minus_capex(self):
        data = valid_fundamentals()
        self.assertAlmostEqual(data["values"]["fcf_ttm"], 5762e6, places=0)


class TestIdentities(unittest.TestCase):
    """숫자 하나만 고친 파일은 항등식에서 걸린다."""

    def test_fcf_inconsistent_with_ocf_and_capex(self):
        data = valid_fundamentals()
        data["values"]["fcf_ttm"] = 9085e6
        errors = validate_fundamentals(data)
        self.assertTrue(any("fcf_ttm" in e for e in errors), errors)

    def test_ttm_must_equal_sum_of_quarters(self):
        data = valid_fundamentals()
        data["values"]["capex_ttm"] = 9600e6
        data["values"]["fcf_ttm"] = data["values"]["ocf_ttm"] - 9600e6
        errors = validate_fundamentals(data)
        self.assertTrue(any("분기 합" in e for e in errors), errors)

    def test_net_debt_identity(self):
        data = valid_fundamentals()
        data["values"]["net_debt"] = 0.0
        errors = validate_fundamentals(data)
        self.assertTrue(any("net_debt" in e for e in errors), errors)

    def test_three_quarters_is_rejected(self):
        """4분기가 안 되면 TTM 이 아니다. 모자란 채로 더하면 기간이 어긋난다."""
        data = valid_fundamentals()
        data["quarters"]["ocf"] = data["quarters"]["ocf"][:3]
        self.assertTrue(validate_fundamentals(data))

    def test_non_contiguous_quarters_rejected(self):
        data = valid_fundamentals()
        data["quarters"]["ocf"][0]["fy"] = 2024   # 한 해 건너뜀
        errors = validate_fundamentals(data)
        self.assertTrue(any("연속" in e for e in errors), errors)

    def test_zero_shares_rejected(self):
        data = valid_fundamentals()
        data["values"]["shares_diluted"] = 0.0
        data["fundamentals_id"] = fundamentals_id_for(data)
        self.assertTrue(validate_fundamentals(data))


class TestFingerprint(unittest.TestCase):
    """항등식을 전부 맞춰 놓은 위조도 지문에서 걸려야 한다."""

    def test_consistent_forgery_is_caught_by_id(self):
        data = valid_fundamentals()
        # capex 를 줄여 FCF 를 부풀리되 모든 항등식을 다시 맞춘다
        data["quarters"]["capex"][-1]["val"] = 2466e6
        data["values"]["capex_ttm"] = sum(q["val"] for q in data["quarters"]["capex"])
        data["values"]["fcf_ttm"] = (data["values"]["ocf_ttm"]
                                     - data["values"]["capex_ttm"])
        errors = validate_fundamentals(data)
        self.assertEqual(len(errors), 1, errors)
        self.assertIn("fundamentals_id", errors[0])
        self.assertIn("편집된", errors[0])

    def test_id_is_stable_across_created_at(self):
        """수집 시각이 달라도 같은 내용이면 같은 지문이다."""
        a = valid_fundamentals()
        b = valid_fundamentals(created_at="2027-01-01T00:00:00+09:00")
        self.assertEqual(a["fundamentals_id"], b["fundamentals_id"])


class TestLoader(unittest.TestCase):
    def test_load_rejects_forged_file(self):
        data = valid_fundamentals()
        data["values"]["fcf_ttm"] = 1.0
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as f:
            json.dump(data, f)
            path = f.name
        with self.assertRaises(ContractError):
            load_fundamentals(path)


class TestRealSnapshot(unittest.TestCase):
    """저장소에 실제로 들어온 스냅샷이 계약을 통과하는지."""

    def test_committed_snapshots_are_valid(self):
        d = ROOT / "knowledge" / "fundamentals"
        files = sorted(d.glob("*.json")) if d.exists() else []
        if not files:
            self.skipTest("수집된 스냅샷이 없다")
        for path in files:
            with self.subTest(path.name):
                self.assertEqual(validate_fundamentals(
                    json.loads(path.read_text(encoding="utf-8"))), [])


if __name__ == "__main__":
    unittest.main()
