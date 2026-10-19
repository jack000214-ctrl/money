"""packet 의 보조자료는 refresh 가 본 것과 같은 말을 해야 한다.

이 계층이 없을 때의 구멍 셋.

· **결측이 0 이 됐다.** 수급 파일에 순매수 값이 없어도 `None or 0` 으로 더해
  "기관·외국인 순매수 0주, AVAILABLE" 이 실려 나갔다.
· **낡은 값이 AVAILABLE 이었다.** refresh 는 30일 된 수급을 STALE 이라 하는데
  packet 은 같은 파일을 읽어 가용하다고 했다. 모델이 보는 것은 packet 이다.
· **파일 이름이 정체성이었다.** `segments/005930.json` 안에 다른 회사의
  asset_id 와 근거 없는 비중만 넣어도 삼성전자의 부문 비중으로 실렸다.

여기서 지키는 성질: 없는 값은 null 이고, 낡은 값은 낡았다고 적혀 있고, 계약을
어긴 값은 실리지 않는다.
"""

import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT  # noqa: F401
from calculators.contracts import canonical_payload_hash
from cio import context, refresh
from cio.assets import get_asset
from collectors import kr_segments

NOW = dt.datetime(2026, 10, 13, 3, 0, tzinfo=dt.timezone.utc)
ASSET = {
    "asset_id": "005930", "market": "KR", "asset_type": "STOCK", "peers": [],
    "optional_collectors": ["KR_FLOW", "KR_LENDING", "KR_SEGMENTS"],
}
FLOW_HEADER = "date,close,institution_net,foreign_net,foreign_pct\n"


class Workspace(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        for name in ("flows", "lending", "segments", "filings"):
            (self.knowledge / name).mkdir(parents=True)
        for target, values in (
                (context, {"ROOT": self.root, "KNOWLEDGE_DIR": self.knowledge}),
                (refresh, {"ROOT": self.root})):
            patcher = patch.multiple(target, **values)  # pyright: ignore[reportCallIssue,reportArgumentType]
            patcher.start()
            self.addCleanup(patcher.stop)
        fx = patch.object(context, "_fx_quarters", return_value=(None, None))
        fx.start()
        self.addCleanup(fx.stop)

    def write_flow(self, body, *, collected="2026-10-13", owner="005930"):
        (self.knowledge / "flows" / "005930.csv").write_text(
            f"# {owner} 투자자별 순매수 / 단위 주\n# 수집일 {collected} / 출처 합성\n"
            + FLOW_HEADER + body, encoding="utf-8")

    def write_lending(self, *, collected="2026-10-13"):
        (self.knowledge / "lending" / "005930.csv").write_text(
            "# 005930 삼성전자 (코스피) 대차거래 / 단위 주·원\n"
            f"# 수집일 {collected} / 출처 합성\n"
            "date,balance_shares\n2026-10-08,100\n2026-10-12,120\n", encoding="utf-8")

    def tasks(self):
        planned: list = []
        refresh._append_optional(planned, ASSET)
        return [refresh.inspect_task(task, now=NOW) for task in planned]

    def summaries(self, tasks=None):
        return context._optional_collection_summaries(
            ASSET, refresh_tasks=self.tasks() if tasks is None else tasks)

    def coverage(self, summaries):
        return context._data_coverage(ASSET, structured_optional=summaries)


class TestMissingIsNotZero(Workspace):
    def test_days_without_a_value_are_left_out_of_the_sum(self):
        self.write_flow("2026-10-08,100,10,20,50\n2026-10-12,101,,30,50\n")
        flow = self.summaries()[0]["kr_flow"]
        self.assertEqual(flow["institution_net_shares"], 10)
        self.assertEqual(flow["foreign_net_shares"], 50)
        self.assertEqual(flow["observed_sessions"], {"institution": 1, "foreign": 2})
        self.assertEqual(flow["state"], "PARTIAL")
        self.assertEqual(self.coverage({"kr_flow": flow})["flow_and_lending"]["state"],
                         "PARTIAL")

    def test_a_fully_observed_window_carries_no_partial_marker(self):
        self.write_flow("2026-10-08,100,10,20,50\n2026-10-12,101,-4,30,50\n")
        flow = self.summaries()[0]["kr_flow"]
        self.assertEqual(flow["institution_net_shares"], 6)
        self.assertNotIn("observed_sessions", flow)
        self.assertNotIn("state", flow)

    def test_no_observation_at_all_is_null_never_zero(self):
        self.write_flow("2026-10-08,100,,,50\n2026-10-12,101,,,50\n")
        flow, _ = context._optional_collection_summaries(ASSET, refresh_tasks=[])
        self.assertIsNone(flow["kr_flow"]["institution_net_shares"])
        self.assertIsNone(flow["kr_flow"]["foreign_net_shares"])
        self.assertEqual(flow["kr_flow"]["state"], "NO_DATA")

    def test_refresh_refuses_a_flow_file_without_any_net_value(self):
        self.write_flow("2026-10-08,100,,,50\n2026-10-12,101,,,50\n")
        task = next(t for t in self.tasks() if t.capability == "KR_FLOW")
        self.assertEqual(task.state, "STALE")
        assert task.contract_error is not None
        self.assertIn("결측을 0 으로 읽지 않는다", task.contract_error)
        summaries, sources = self.summaries()
        self.assertEqual(summaries["kr_flow"]["state"], "INVALID")
        self.assertNotIn("institution_net_shares", summaries["kr_flow"])
        self.assertFalse([s for s in sources if s["source_id"] == "KR_FLOW:005930"])

    def test_a_file_missing_the_net_columns_is_a_contract_failure(self):
        (self.knowledge / "flows" / "005930.csv").write_text(
            "# 005930 투자자별 순매수 / 단위 주\n# 수집일 2026-10-13 / 출처 합성\n"
            "date,foreign_pct\n2026-10-12,50\n", encoding="utf-8")
        task = next(t for t in self.tasks() if t.capability == "KR_FLOW")
        assert task.contract_error is not None
        self.assertIn("필수 열", task.contract_error)


class TestStaleIsSaidWhereTheNumberIs(Workspace):
    def test_an_old_file_keeps_its_numbers_and_says_it_is_old(self):
        self.write_flow("2026-09-01,100,100,200,10\n", collected="2026-09-13")
        summaries, sources = self.summaries()
        flow = summaries["kr_flow"]
        self.assertEqual(flow["institution_net_shares"], 100)
        self.assertEqual(flow["freshness"], "STALE")
        self.assertIn("현재 값으로 읽지 않고", flow["_freshness_note"])
        self.assertTrue([s for s in sources if s["source_id"] == "KR_FLOW:005930"])
        row = self.coverage(summaries)["flow_and_lending"]
        self.assertEqual(row["state"], "STALE")
        self.assertEqual(row["as_of"], "2026-09-01")

    def test_a_fresh_file_is_available_and_unmarked(self):
        self.write_flow("2026-10-12,100,100,200,10\n")
        summaries, _ = self.summaries()
        self.assertNotIn("freshness", summaries["kr_flow"])
        self.assertEqual(self.coverage(summaries)["flow_and_lending"]["state"],
                         "AVAILABLE")

    def test_two_collectors_on_one_row_report_both_states(self):
        """한쪽이 낡았는데 다른 쪽이 있다고 줄 전체를 가용하다고 하지 않는다."""
        self.write_flow("2026-09-01,100,100,200,10\n", collected="2026-09-13")
        self.write_lending()
        row = self.coverage(self.summaries()[0])["flow_and_lending"]
        self.assertEqual(row["state"], "PARTIAL")
        self.assertEqual(row["parts"], {"kr_flow": "STALE", "kr_lending": "AVAILABLE"})

    def test_a_file_that_names_another_stock_is_not_carried(self):
        self.write_flow("2026-10-12,100,100,200,10\n", owner="000660")
        summaries, sources = self.summaries()
        self.assertEqual(summaries["kr_flow"]["state"], "INVALID")
        self.assertFalse([s for s in sources if s["source_id"] == "KR_FLOW:005930"])
        self.assertEqual(self.coverage(summaries)["flow_and_lending"]["state"],
                         "REGISTERED_NOT_COLLECTED")


SEGMENT_DOCUMENT = """<TABLE><TBODY>
<TR><TD>부문</TD><TD>구 분</TD><TD>제58기 1분기</TD><TD>제57기</TD></TR>
<TR><TD>금액</TD><TD>비중</TD><TD>금액</TD><TD>비중</TD></TR>
<TR><TD>DX 부문</TD><TD>매출액</TD><TD>526,547</TD><TD>39.3%</TD><TD>1,879,673</TD><TD>56.3%</TD></TR>
<TR><TD>영업이익</TD><TD>29,677</TD><TD>5.2%</TD><TD>128,527</TD><TD>29.5%</TD></TR>
<TR><TD>총자산</TD><TD>3,174,942</TD><TD>36.0%</TD><TD>2,810,397</TD><TD>36.0%</TD></TR>
<TR><TD>DS 부문</TD><TD>매출액</TD><TD>817,156</TD><TD>61.0%</TD><TD>1,301,282</TD><TD>39.0%</TD></TR>
<TR><TD>영업이익</TD><TD>536,633</TD><TD>93.8%</TD><TD>248,581</TD><TD>57.0%</TD></TR>
<TR><TD>총자산</TD><TD>4,364,341</TD><TD>49.5%</TD><TD>3,719,620</TD><TD>47.6%</TD></TR>
</TBODY></TABLE>"""
REPORT = "20260515002181"


class TestSegmentsBelongToTheCompany(Workspace):
    def setUp(self):
        super().setUp()
        payload = {"rcept_no": REPORT, "documents": {"report.xml": SEGMENT_DOCUMENT}}
        digest = canonical_payload_hash(payload)
        self.locator = f"data/raw/dart/objects/{digest}.json"
        (self.root / self.locator).parent.mkdir(parents=True)
        (self.root / self.locator).write_text(json.dumps({
            "fetched_at": "2026-08-06T00:00:00+00:00", "source": "opendart.fss.or.kr",
            "payload_sha256": digest, "payload": payload}), encoding="utf-8")
        self.index = {"filings": [
            {"form": "QUARTER_REPORT", "filing_date": "2026-05-15",
             "accession_number": REPORT},
            {"form": "PROVISIONAL_RESULT", "filing_date": "2026-07-30",
             "accession_number": "20260730800077"},
        ]}
        self.data = {
            "schema_version": kr_segments.SCHEMA, "asset_id": "005930",
            "collected_at": "2026-10-13T00:00:00+00:00", "rcept_no": REPORT,
            "raw_payload_sha256": digest, "raw_locator": self.locator,
            **kr_segments.extract(SEGMENT_DOCUMENT),
        }

    def errors(self, data=None, index: "dict | str" = "default"):
        resolved_index = self.index if index == "default" else index
        return kr_segments.contract_errors(
            self.data if data is None else data, "005930",
            index=resolved_index if isinstance(resolved_index, dict) else None,  # pyright: ignore[reportArgumentType]
            root=self.root)

    def test_material_that_replays_from_its_report_passes(self):
        self.assertEqual(self.errors(), [])
        self.assertEqual(self.data["latest_mix"][0]["name"], "DS 부문")

    def test_another_companys_file_under_this_name_is_refused(self):
        self.assertTrue(self.errors({**self.data, "asset_id": "000660"}))

    def test_numbers_that_do_not_come_from_the_report_are_refused(self):
        forged = {**self.data, "latest_mix": [
            {"name": "Wrong business", "revenue_share_pct": 100,
             "operating_income_share_pct": 100}]}
        self.assertIn("latest_mix", self.errors(forged)[0])

    def test_a_report_that_is_not_in_this_assets_index_is_refused(self):
        other = {"filings": [{"form": "QUARTER_REPORT", "filing_date": "2026-05-15",
                              "accession_number": "20260515009999"}]}
        self.assertIn("최신 정기보고서", self.errors(index=other)[0])

    def test_a_newer_periodic_report_makes_the_material_stale(self):
        """무효화는 색인을 다시 받았다는 사실이 아니라 새 보고서로 한다."""
        self.index["filings"].append({
            "form": "HALF_REPORT", "filing_date": "2026-08-14",
            "accession_number": "20260814003509"})
        self.assertIn("20260814003509", self.errors()[0])

    def test_segments_do_not_rerun_just_because_the_index_was_refetched(self):
        planned: list = []
        refresh._append_optional(planned, ASSET)
        task = next(t for t in planned if t.capability == "KR_SEGMENTS")
        self.assertEqual(task.dependencies, [])

    def test_packet_does_not_carry_a_file_that_names_another_company(self):
        (self.knowledge / "segments" / "005930.json").write_text(json.dumps({
            "schema_version": "wrong", "asset_id": "WRONG",
            "latest_period": "2026-06-30",
            "latest_mix": [{"name": "Wrong", "revenue_share_pct": 100}]}),
            encoding="utf-8")
        summaries, sources = context._optional_collection_summaries(
            ASSET, refresh_tasks=[])
        self.assertEqual(summaries["segments"]["state"], "INVALID")
        self.assertNotIn("mix", summaries["segments"])
        self.assertFalse([s for s in sources if s["source_id"].startswith("KR_SEG")])


class TestStaleEtfMaterial(unittest.TestCase):
    def test_constituent_multiples_older_than_the_refresh_window_are_marked(self):
        asset = {**get_asset("0167A0"), "optional_collectors": ["ETF_CONSTITUENTS"]}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "knowledge" / "etfs" / "0167A0"
            directory.mkdir(parents=True)
            (directory / "holdings-2026-09-01.csv").write_text(
                "# retrieved_at 2026-08-14T00:00:00+00:00 / source 합성\n"
                "etf_id,holding_id,holding_name,weight_pct,as_of\n"
                "0167A0,TEST,Test,100,2026-09-01\n", encoding="utf-8")
            (directory / "constituents-2026-09-01.json").write_text(json.dumps({
                "schema_version": "etf-constituents/1.0.0", "asset_id": "0167A0",
                "collected_at": "2026-09-13T00:00:00+00:00",
                "holdings_as_of": "2026-09-01",
                "holdings": [{"holding_id": "TEST", "weight_pct": 100,
                              "market": "US", "valuation": {"forward_pe": 20}}],
            }), encoding="utf-8")
            task = refresh.RefreshTask(
                "constituents:0167A0", "ETF_CONSTITUENTS",
                "knowledge/etfs/0167A0/constituents-*.json", 168, ["합성"],
                required=False,
                dependencies=["knowledge/etfs/0167A0/holdings-*.csv"])
            with patch.object(refresh, "ROOT", root), \
                    patch.object(context, "ROOT", root), \
                    patch.object(context, "KNOWLEDGE_DIR", root / "knowledge"):
                task = refresh.inspect_task(task, now=NOW)
                block, _ = context._etf_lookthrough(
                    asset, holdings_as_of="2026-09-01", refresh_tasks=[task])
                coverage = context._etf_data_coverage(
                    asset, {"as_of": "2026-09-01", "lookthrough": block})
        self.assertEqual(task.state, "STALE")
        assert block is not None
        self.assertEqual(block["valuation"]["forward_pe"], 20.0)
        self.assertEqual(block["freshness"], "STALE")
        self.assertEqual(coverage["lookthrough_valuation"]["state"], "STALE")


if __name__ == "__main__":
    unittest.main()
