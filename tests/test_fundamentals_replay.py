"""재무 스냅샷은 봉인된 원문에서 그대로 다시 나와야 한다.

이 계층이 없을 때의 구멍: 재무 검사는 내부 항등식(분기 합 = TTM, 차입 − 현금 =
순부채)과 내용 지문만 봤다. 그래서 원문의 영업현금흐름이 40 인데 스냅샷의 분기·
TTM·FCF 를 일관되게 400 으로 바꾸고 지문을 새로 내면 `FRESH`, 계약 오류 없음으로
통과했다. 지문은 파일이 보존됐다는 증거이지 그 안의 숫자가 원문과 같다는 증거가
아니다.

같은 구멍으로 **추출 규칙이 바뀐 뒤의 옛 스냅샷**도 통과했다. SK하이닉스의 총차입은
옛 분류로 2.5조, 지금 분류로 21.8조인데 옛 스냅샷은 아무 표시 없이 계속 쓰였다.

여기서 지키는 성질은 하나다 — 수집과 검사가 같은 도출 함수를 쓰고, 검사는 그
함수를 원문에 다시 돌린 결과와 스냅샷을 값으로 대조한다.
"""

import copy
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT, point  # noqa: F401
from calculators.contracts import (
    SCHEMA_VERSION,
    canonical_payload_hash,
    fundamentals_differences,
    fundamentals_id_for,
)
from cio.refresh import RefreshTask, inspect_task
from collectors import dart, edgar

OCF = "ifrs-full_CashFlowsFromUsedInOperatingActivities"
CAPEX = "ifrs-full_PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"


def _row(sj_div, account_id, name, amount, cumulative=None):
    row = {"sj_div": sj_div, "account_id": account_id, "account_nm": name,
           "thstrm_amount": str(amount)}
    if cumulative is not None:
        row["thstrm_add_amount"] = str(cumulative)
    return row


def _report(quarter, *, with_debt=True):
    """한 보고서의 행. 분기 값은 매출 100·매출총이익 40·판관비 10·영업이익 30 이다."""
    rows = [
        _row("CF", OCF, "영업활동현금흐름", 20 * quarter),
        _row("CF", CAPEX, "유형자산의 취득", 5 * quarter),
        _row("IS", "ifrs-full_Revenue", "매출액", 100, 100 * quarter),
        _row("IS", "ifrs-full_GrossProfit", "매출총이익", 40, 40 * quarter),
        _row("IS", "dart_TotalSellingGeneralAdministrativeExpenses", "판매비와관리비",
             10, 10 * quarter),
        _row("IS", "dart_OperatingIncomeLoss", "영업이익", 30, 30 * quarter),
        _row("IS", "ifrs-full_ProfitLoss", "당기순이익", 20, 20 * quarter),
        _row("IS", "ifrs-full_ProfitLossAttributableToOwnersOfParent",
             "지배기업 소유주지분", 20),
        _row("IS", "ifrs-full_DilutedEarningsLossPerShare", "희석주당이익", 2),
        _row("BS", "ifrs-full_CashAndCashEquivalents", "현금및현금성자산", 500),
        _row("BS", "ifrs-full_CurrentAssets", "유동자산", 5_000),
        _row("BS", "ifrs-full_CurrentInvestments", "단기투자자산", 300),
        _row("BS", "ifrs-full_Liabilities", "부채총계", 2_000),
        _row("BS", "ifrs-full_CurrentLiabilities", "유동부채", 1_200),
        _row("BS", "ifrs-full_NoncurrentLiabilities", "비유동부채", 800),
        _row("BS", "ifrs-full_CurrentLeaseLiabilities", "리스부채", 50),
    ]
    if with_debt:
        rows += [
            _row("BS", "ifrs-full_CurrentBorrowingsAndCurrentPortionOf"
                 "NoncurrentBorrowings", "차입금", 400),
            _row("BS", "ifrs-full_LongtermBorrowings", "차입금", 600),
        ]
    return rows


def _annual():
    rows = _report(4)
    for row in rows:
        if row["sj_div"] == "IS" and "thstrm_add_amount" in row:
            row["thstrm_amount"] = row.pop("thstrm_add_amount")
    return rows


def dart_raw():
    return {
        "2025_1q": {"list": _report(1)},
        "2025_half": {"list": _report(2)},
        "2025_3q": {"list": _report(3)},
        "2025_annual": {"list": _annual()},
        "2026_1q": {"list": _report(1)},
        "2026_half": {"list": _report(2)},
        "_instrument": {"instrument_id": "999999", "corp_code": "00999999"},
    }


def dart_snapshot(raw):
    derived = dart.derive_fundamentals(raw, fs="CFS", income_quarters=4)
    snapshot = {
        "schema_version": SCHEMA_VERSION,
        "instrument_id": "999999",
        "corp_code": "00999999",
        "fs_div": "CFS",
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        **derived,
    }
    snapshot["fundamentals_id"] = fundamentals_id_for(snapshot)
    return snapshot


class TestDartReplay(unittest.TestCase):
    def setUp(self):
        self.raw = dart_raw()
        self.snapshot = dart_snapshot(self.raw)

    def test_the_derivation_reads_what_the_fixture_says(self):
        values = self.snapshot["values"]
        self.assertEqual(self.snapshot["period_end"], "2026-06-30")
        self.assertEqual(values["ocf_ttm"], 80.0)
        self.assertEqual(values["long_term_debt"], 1_050.0)
        self.assertEqual(values["short_term_investments"], 300.0)
        self.assertEqual(values["net_debt"], 1_050.0 - 500.0 - 300.0)

    def test_a_snapshot_made_from_the_raw_replays_cleanly(self):
        self.assertEqual(dart.replay_fundamentals(self.snapshot, self.raw), [])

    def test_consistent_edits_that_keep_every_identity_are_still_caught(self):
        """분기·TTM·FCF 를 함께 바꾸면 내부 항등식은 전부 맞는다."""
        edited = copy.deepcopy(self.snapshot)
        for row in edited["quarters"]["ocf"]:
            row["val"] *= 10
        edited["values"]["ocf_ttm"] = 800.0
        edited["values"]["fcf_ttm"] = 800.0 - edited["values"]["capex_ttm"]
        differences = dart.replay_fundamentals(edited, self.raw)
        self.assertTrue(any("ocf" in item for item in differences))

    def test_a_snapshot_made_by_an_older_extraction_rule_is_stale(self):
        """옛 분류로 만든 스냅샷: 차입금 1,000 이 빠지고 리스 50 만 남았다."""
        old = copy.deepcopy(self.snapshot)
        old["values"]["long_term_debt"] = 50.0
        old["values"]["net_debt"] = 50.0 - 500.0 - 300.0
        old["debt_by_category"] = {"LEASE": 50.0}
        differences = dart.replay_fundamentals(old, self.raw)
        self.assertIn("values.long_term_debt: 스냅샷 50 / 원문 재생 1,050",
                      differences)

    def test_raw_that_can_no_longer_be_derived_is_reported_not_raised(self):
        broken = {key: value for key, value in self.raw.items()
                  if not key.startswith("2026")}
        differences = dart.replay_fundamentals(self.snapshot, broken)
        self.assertEqual(len(differences), 1)
        self.assertIn("다시 만들 수 없다", differences[0])

    def test_filing_dates_alone_do_not_make_a_snapshot_stale(self):
        """같은 분기 값이 이듬해 비교표에 다시 실리면 제출일만 바뀐다."""
        left = {"quarters": [{"val": 1.0, "filed": "2025-08-01"}]}
        right = {"quarters": [{"val": 1.0, "filed": "2026-08-01"}]}
        self.assertEqual(fundamentals_differences(left, right, ("quarters",)), [])
        right["quarters"][0]["val"] = 3.0
        self.assertEqual(len(fundamentals_differences(left, right, ("quarters",))), 1)


class TestRefreshContractReplays(unittest.TestCase):
    """계약 검사가 실제로 재생을 돌리는가 — 헬퍼가 맞아도 배선이 끊기면 소용없다."""

    def inspect(self, snapshot, raw):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            digest = canonical_payload_hash(raw)
            locator = f"data/raw/dart/objects/{digest}.json"
            (root / locator).parent.mkdir(parents=True)
            (root / locator).write_text(json.dumps({
                "fetched_at": snapshot["created_at"], "source": "opendart.fss.or.kr",
                "payload_sha256": digest, "payload": raw}), encoding="utf-8")
            data = {**snapshot, "raw_locator": locator, "raw_payload_sha256": digest}
            data["fundamentals_id"] = fundamentals_id_for(data)
            path = root / "knowledge" / "fundamentals" / "999999-2026-06-30.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(data), encoding="utf-8")
            task = RefreshTask(
                name="fundamentals:999999", capability="FUNDAMENTALS",
                artifact="knowledge/fundamentals/999999-*.json",
                max_age_hours=720, command=None)
            with patch("cio.refresh.ROOT", root):
                return inspect_task(task)

    def test_matching_snapshot_is_fresh(self):
        raw = dart_raw()
        task = self.inspect(dart_snapshot(raw), raw)
        self.assertEqual((task.state, task.contract_error), ("FRESH", None))

    def test_a_snapshot_that_disagrees_with_its_raw_is_not_fresh(self):
        raw = dart_raw()
        snapshot = dart_snapshot(raw)
        for row in snapshot["quarters"]["ocf"]:
            row["val"] *= 10
        snapshot["values"]["ocf_ttm"] = 800.0
        snapshot["values"]["fcf_ttm"] = 800.0 - snapshot["values"]["capex_ttm"]
        task = self.inspect(snapshot, raw)
        self.assertEqual(task.state, "STALE")
        self.assertIn("그대로 다시 나오지 않는다", task.contract_error)  # pyright: ignore[reportArgumentType]


class TestXbrlInterimPeriods(unittest.TestCase):
    """반기·3분기 XBRL 의 누계를 단독 분기로 읽던 구멍.

    전체계정 API 가 013 이면 XBRL 원본으로 보충한다. 그 경로가 같은 기말로 끝나는
    구간 중 가장 긴 것(연초 누계) 하나를 골라 단독·누계 두 칸에 모두 넣었고, 손익
    복원은 앞 칸을 3개월 값으로 읽었다. Q2=15, 반기 누계=25 인 회사에서 Q2 가 25,
    연간 합이 70 이 아니라 80 이 됐다.
    """

    FACTS = [
        {"start": "2026-04-01", "end": "2026-06-30", "value": 15},
        {"start": "2026-01-01", "end": "2026-06-30", "value": 25},
    ]

    def test_standalone_and_year_to_date_are_picked_by_their_length(self):
        self.assertEqual(dart.xbrl_period_values(self.FACTS, 2026, "half"), (15, 25))

    def test_a_missing_standalone_period_stays_missing(self):
        self.assertEqual(
            dart.xbrl_period_values(self.FACTS[1:], 2026, "half"), (None, 25))

    def test_annual_and_first_quarter_have_one_period(self):
        annual = [{"start": "2025-01-01", "end": "2025-12-31", "value": 70}]
        first = [{"start": "2026-01-01", "end": "2026-03-31", "value": 10}]
        self.assertEqual(dart.xbrl_period_values(annual, 2025, "annual"), (70, 70))
        self.assertEqual(dart.xbrl_period_values(first, 2026, "1q"), (10, 10))

    def reports(self, half_rows):
        revenue = lambda **fields: [{  # noqa: E731
            "sj_div": "IS", "account_id": "ifrs-full_Revenue", **fields}]
        return {
            "1q": revenue(thstrm_amount="10"),
            "half": half_rows,
            "3q": revenue(thstrm_amount="20", thstrm_add_amount="45"),
            "annual": revenue(thstrm_amount="70"),
        }

    def restored(self, half_rows):
        quarters = dart.restore_is_quarters(
            self.reports(half_rows), dart.IS_REVENUE_IDS)
        return {q: row["val"] for q, row in quarters.items()}

    def test_supplemented_half_year_restores_the_real_second_quarter(self):
        with patch.object(dart, "periodic_rcept", return_value="20260814000001"), \
                patch.object(dart, "xbrl_facts_many",
                             return_value={"Revenue": self.FACTS}):
            half = dart.xbrl_rows("00000001", 2026, "half", "KEY")
        self.assertEqual(self.restored(half), {1: 10.0, 2: 15.0, 3: 20.0, 4: 25.0})

    def test_year_to_date_only_is_differenced_not_reused(self):
        half = [{"sj_div": "IS", "account_id": "ifrs-full_Revenue",
                 "thstrm_add_amount": "25"}]
        quarters = dart.restore_is_quarters(self.reports(half), dart.IS_REVENUE_IDS)
        self.assertEqual(quarters[2], {"val": 15.0, "derived": True})

    def test_rows_stored_by_the_old_supplement_are_reread_as_year_to_date(self):
        """이미 저장된 옛 보충 행은 두 칸 모두 누계다. 다시 읽을 때 바로잡는다."""
        raw = {
            "2026_1q": {"list": self.reports([])["1q"]},
            "2026_half_xbrl": [{
                "sj_div": "IS", "account_id": "ifrs-full_Revenue",
                "thstrm_amount": "25", "thstrm_add_amount": "25", "_source": "xbrl"}],
        }
        quarters = dart.restore_is_quarters(
            dart.reports_by_year(raw)[2026], dart.IS_REVENUE_IDS)
        self.assertEqual(quarters[2]["val"], 15.0)


def _flow(tag, per_quarter):
    """두 회계연도의 누계 흐름 사실. 분기마다 `per_quarter` 씩 쌓인다."""
    units = []
    for year in (2025, 2026):
        for fp, end, count, form in (
                ("Q1", f"{year}-03-31", 1, "10-Q"), ("Q2", f"{year}-06-30", 2, "10-Q"),
                ("Q3", f"{year}-09-30", 3, "10-Q"), ("FY", f"{year}-12-31", 4, "10-K")):
            units.append(point(year, fp, f"{year}-01-01", end, per_quarter * count,
                               form=form, filed=f"{year + (fp == 'FY')}-02-01"))
    return {"tag": tag, "units": units}


def edgar_raw():
    shares = [{"fy": 2026, "fp": "FY", "start": "2026-10-01", "end": "2026-12-31",
               "val": 100, "form": "10-K", "filed": "2027-02-01"}]
    instant = lambda value: [{  # noqa: E731
        "end": "2026-12-31", "val": value, "form": "10-K", "filed": "2027-02-01"}]
    return {
        "_instrument": {"instrument_id": "SYN", "cik": "0000000001"},
        "영업현금흐름": _flow("NetCashProvidedByUsedInOperatingActivities", 40),
        "capex": _flow("PaymentsToAcquirePropertyPlantAndEquipment", 10),
        "손익_revenue": _flow("Revenues", 100),
        "손익_net_income": _flow("NetIncomeLoss", 20),
        "현금성자산": {"tag": "CashAndCashEquivalentsAtCarryingValue",
                    "units": instant(500)},
        "단기투자자산": {"tag": None, "units": []},
        "희석주식수": {"tag": "WeightedAverageNumberOfDilutedSharesOutstanding",
                   "units": shares},
        "차입_후보태그": {
            "LongTermDebtNoncurrent": {"end": "2026-12-31", "val": 600},
            "ShortTermBorrowings": {"end": "2026-12-31", "val": 100},
            "LongTermDebtCurrent": {"end": "2026-12-31", "val": 300},
        },
        "부채총계": {"tag": "Liabilities", "units": instant(5_000)},
    }


class TestEdgarReplay(unittest.TestCase):
    def setUp(self):
        self.raw = edgar_raw()
        self.snapshot = edgar.derive_us_fundamentals(self.raw)

    def test_the_derivation_reads_what_the_fixture_says(self):
        values = self.snapshot["values"]
        self.assertEqual(values["ocf_ttm"], 160.0)
        # 단기차입금과 장기차입금 유동분을 둘 다 센다.
        self.assertEqual(values["long_term_debt"], 1_000.0)
        self.assertEqual(self.snapshot["short_term_investments_basis"],
                         "NOT_DISCLOSED")

    def test_a_snapshot_made_from_the_raw_replays_cleanly(self):
        self.assertEqual(edgar.replay_fundamentals(self.snapshot, self.raw), [])

    def test_consistent_edits_are_caught(self):
        edited = copy.deepcopy(self.snapshot)
        for row in edited["quarters"]["ocf"]:
            row["val"] *= 10
        edited["values"]["ocf_ttm"] = 1_600.0
        edited["values"]["fcf_ttm"] = 1_600.0 - edited["values"]["capex_ttm"]
        self.assertTrue(edgar.replay_fundamentals(edited, self.raw))

    def test_a_snapshot_missing_the_overlap_flags_is_stale(self):
        raw = edgar_raw()
        raw["차입_후보태그"] = {
            "LongTermDebt": {"end": "2026-12-31", "val": 600},
            "LongTermDebtCurrent": {"end": "2026-12-31", "val": 300},
        }
        snapshot = edgar.derive_us_fundamentals(raw)
        self.assertEqual(edgar.replay_fundamentals(snapshot, raw), [])
        snapshot.pop("debt_overlap_candidates")
        self.assertIn("debt_overlap_candidates",
                      edgar.replay_fundamentals(snapshot, raw)[-1])


if __name__ == "__main__":
    unittest.main()
