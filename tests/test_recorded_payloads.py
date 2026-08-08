"""저장해 둔 실제 공시 응답을 파서에 다시 흘려보낸다.

이 저장소는 모든 원본 응답을 `data/raw/` 에 남긴다. 그것을 파서에 재생하면
**네트워크 없이 파서 회귀를 잡을 수 있다.** 출처가 내일 모양을 바꾸는 것은 여전히
못 잡지만, 우리가 파서를 고치다 깨뜨리는 것은 잡힌다 — 지금까지는 그것도 못
잡았다.

합성 fixture 와 다른 점이 여기 있다. 손으로 만든 입력은 우리가 상상한 모양만
담는다. 실제 응답에는 상상하지 못한 것이 들어 있다 — 같은 표준코드에 회사마다
다른 계정명, 표준코드가 아예 없는 회사(`-표준계정코드 미사용-`), 같은 개념의
차원별 중복. 그 위에서 항등식이 성립하는지가 이 파일의 질문이다.

원본이 없으면 조용히 건너뛰지 않고 실패한다. 원본을 지우면 이 파일은 아무것도
검사하지 않게 되는데, 그 사실이 보이지 않으면 통과가 거짓말이 된다.
"""

import json
import unittest
from collections import defaultdict
from pathlib import Path

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.paths import ROOT
from collectors.dart import (
    acct,
    amount,
    back_derive_q4,
    declared_currency,
    income_identity_residual,
    quarter_metrics,
    to_int,
)

RAW = ROOT / "data" / "raw" / "dart"
REPORTS = {"11013": "1Q", "11012": "2Q", "11014": "3Q", "11011": "FY"}
REVENUE_IDS = ("ifrs-full_Revenue", "ifrs_Revenue")
REVENUE_NAMES = ("매출액", "수익(매출액)", "영업수익")
OPERATING_IDS = ("dart_OperatingIncomeLoss",)
OPERATING_NAMES = ("영업이익", "영업이익(손실)")


def recorded():
    """(파일, 계정행 목록). 재무제표 응답만."""
    for path in sorted(RAW.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = ((payload.get("payload") or payload).get("list")) or []
        if rows:
            yield path, rows


class RecordedCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.files = list(recorded())
        if not cls.files:
            raise AssertionError(
                f"{RAW.relative_to(ROOT)} 에 저장된 응답이 없다. 원본이 사라지면 "
                "이 파일은 아무것도 검사하지 않으면서 통과한다"
            )


class TestEveryRecordedResponseParses(RecordedCase):
    def test_the_corpus_is_not_trivially_small(self):
        """표본이 한둘이면 '회사마다 다르다'를 검사할 수 없다."""
        rows = sum(len(rows) for _path, rows in self.files)
        corps = {row.get("corp_code") for _p, rows in self.files
                 for row in rows} - {None}
        self.assertGreaterEqual(len(self.files), 20)
        self.assertGreaterEqual(rows, 1000)
        self.assertGreaterEqual(len(corps), 3)

    def test_every_amount_in_the_corpus_reads_as_a_number(self):
        """읽지 못한 금액은 None 이 되고, None 은 '계정을 못 찾았다'와 같아 보인다."""
        unreadable = []
        for path, rows in self.files:
            for row in rows:
                for field in ("thstrm_amount", "frmtrm_amount",
                              "bfefrmtrm_amount"):
                    raw = row.get(field)
                    if raw in (None, "", "-"):
                        continue
                    if to_int(raw) is None:
                        unreadable.append((path.name, row.get("account_nm"),
                                           field, raw))
        self.assertEqual(unreadable[:5], [], f"총 {len(unreadable)}건")

    def test_every_response_declares_one_currency(self):
        """통화가 섞이면 원 단위 가정이 깨진다."""
        for path, rows in self.files:
            with self.subTest(path.name):
                self.assertEqual(declared_currency(rows), "KRW")

    def test_revenue_and_operating_income_resolve_in_every_response(self):
        """표준코드가 없는 회사(`-표준계정코드 미사용-`)가 실제로 있다.

        계정명 폴백이 죽으면 그 회사만 조용히 빈다.
        """
        missing = []
        for path, rows in self.files:
            if not any(row.get("sj_div") in ("IS", "CIS") for row in rows):
                continue
            revenue = acct(rows, ("IS", "CIS"), REVENUE_IDS, REVENUE_NAMES)
            operating = acct(rows, ("IS", "CIS"), OPERATING_IDS,
                             OPERATING_NAMES)
            if amount(revenue) is None or amount(operating) is None:
                missing.append(path.name)
        self.assertEqual(missing, [])


class TestQuarterlySeriesFromRecordedData(RecordedCase):
    """실제 분기 응답으로 복원한 시계열이 성립하는가."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        series = defaultdict(dict)
        for _path, rows in cls.files:
            head = rows[0]
            corp, year = head.get("corp_code"), head.get("bsns_year")
            report = REPORTS.get(head.get("reprt_code"))
            if not (corp and year and report):
                continue
            revenue = acct(rows, ("IS", "CIS"), REVENUE_IDS, REVENUE_NAMES)
            operating = acct(rows, ("IS", "CIS"), OPERATING_IDS,
                             OPERATING_NAMES)
            series[corp][(int(year), report)] = (amount(revenue),
                                                 amount(operating))
        cls.series = series

    def test_several_companies_are_covered(self):
        self.assertGreaterEqual(len(self.series), 3)

    def test_the_fourth_quarter_back_derives_to_a_positive_revenue(self):
        """음수가 나오면 분기 금액이 누계라는 뜻이고, 그때는 값을 내지 않는다."""
        derived = 0
        for corp, table in self.series.items():
            for year in {key[0] for key in table}:
                if (year, "FY") not in table:
                    continue
                quarters = {
                    label: (*table[(year, label)], "All")
                    for label in ("1Q", "2Q", "3Q")
                    if (year, label) in table
                }
                result = back_derive_q4(table[(year, "FY")], quarters)
                if result is None:
                    continue
                derived += 1
                with self.subTest(f"{corp} {year}"):
                    self.assertGreater(result[0], 0)
        self.assertGreaterEqual(derived, 3, "역산이 한 건도 성립하지 않았다")

    def test_operating_margins_stay_inside_a_plausible_range(self):
        """부호가 뒤집히거나 100 배 어긋나면 여기서 걸린다."""
        for corp, table in self.series.items():
            rows = quarter_metrics({
                f"{label}{str(year)[2:]}": (*table[(year, label)], "All")
                for (year, label) in sorted(table) if label != "FY"
            })
            for row in rows:
                margin = row["operating_margin_pct"]
                if margin is None:
                    continue
                with self.subTest(f"{corp} {row['label']}"):
                    self.assertGreater(margin, -100.0)
                    self.assertLess(margin, 100.0)

    def test_a_year_over_year_rate_never_comes_from_a_nonpositive_base(self):
        for corp, table in self.series.items():
            labels = {f"{label}{str(year)[2:]}": (*table[(year, label)], "All")
                      for (year, label) in sorted(table) if label != "FY"}
            for row in quarter_metrics(labels):
                if row["revenue_yoy_pct"] is None:
                    continue
                prior = labels.get(
                    f"{row['label'][:2]}{int(row['label'][2:]) - 1}")
                with self.subTest(f"{corp} {row['label']}"):
                    self.assertIsNotNone(prior)
                    self.assertGreater(prior[0], 0)


class TestIncomeIdentityOnStoredSnapshots(unittest.TestCase):
    """저장된 재무 스냅샷에서 매출총이익 − 비용 = 영업이익 이 성립하는가.

    비용 계정을 못 찾으면 영업이익이 매출총이익에서 일부만 뺀 값이 되고, 마진이
    과대계상된 채 저장된다. 숫자 자체는 그럴듯하게 나오므로 항등식 없이는 아무도
    눈치채지 못한다.
    """

    @classmethod
    def setUpClass(cls):
        cls.snapshots = []
        for path in sorted((ROOT / "knowledge" / "fundamentals").glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("expense_lines") and data.get("income_statement"):
                cls.snapshots.append((path.name, data))
        if not cls.snapshots:
            raise AssertionError("손익 항등식을 검사할 스냅샷이 없다")

    def latest(self, statement, key):
        quarters = (statement.get(key) or {}).get("quarters") or []
        return quarters[-1]["val"] if quarters else None

    def test_the_identity_holds_for_every_stored_snapshot(self):
        for name, data in self.snapshots:
            statement = data["income_statement"]
            gross = self.latest(statement, "gross_profit")
            operating = self.latest(statement, "operating_income")
            expenses = [self.latest(statement, line["key"])
                        for line in data["expense_lines"]]
            if gross is None or operating is None or None in expenses:
                continue
            residual, tolerance = income_identity_residual(
                gross, expenses, operating)
            with self.subTest(name):
                self.assertLessEqual(abs(residual), tolerance)

    def test_the_corpus_spans_very_different_company_sizes(self):
        """조 단위와 백억 단위가 같은 코드를 지나는지가 요점이다."""
        revenues = [
            self.latest(data["income_statement"], "revenue")
            for _name, data in self.snapshots
        ]
        revenues = [value for value in revenues if value]
        self.assertGreater(max(revenues) / min(revenues), 100)


if __name__ == "__main__":
    unittest.main()
