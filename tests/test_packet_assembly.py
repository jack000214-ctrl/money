"""packet 을 조립하는 부분 중 지금까지 mock 뒤에 가려져 있던 자리.

`build_context` 를 도는 테스트는 있었지만, 그 테스트들이 `_filing_summary`·
`_market_summary`·`_attach_position_math` 를 전부 mock 으로 갈아끼우고 돌았다.
즉 "packet 만드는 경로는 테스트된다"가 아니라 **그 경로에서 이 부분들을 빼고
테스트된다**였다. `_snapshot_evidence` 는 mock 조차 없이 어느 테스트에도
나오지 않았다.

이 넷이 하는 일은 서로 다르지만 실패 방식은 같다 — **조용히 비는 것**이다.
공시 색인이 안 실리면 "이 종목은 공시가 없다"로, 시장 국면이 안 실리면 "국면을
안 본다"로, 원장 산술이 안 붙으면 "원장이 없다"로 읽힌다. 셋 다 packet 은
정상으로 보인다.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import content_hash
from calculators import position as position_module
from cio import context
from cio.context import ContextError


class PacketPart(unittest.TestCase):
    """임시 저장소. `relative_to(ROOT)` 가 성립하도록 안에 둔다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        self.addCleanup(self._tmp.cleanup)
        patcher = patch.multiple(
            context, KNOWLEDGE_DIR=self.knowledge, ROOT=self.root
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, relative, payload):
        path = self.knowledge / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        return path


def filing(form, date="2026-08-07", **extra):
    row = {"filing_date": date, "form": form,
           "accession_number": f"ACC-{form}-{date}",
           "description": f"{form} 문서"}
    row.update(extra)
    return row


class TestFilingIndex(PacketPart):
    """공시 색인. 여기가 비면 '이 종목은 공시가 없다'로 읽힌다."""

    ASSET = {"asset_id": "TSLA", "asset_type": "STOCK", "market": "US"}

    def index(self, **overrides):
        data = {
            "schema_version": "filing-index/1.0.0",
            "asset_id": "TSLA",
            "source": "SEC_SUBMISSIONS",
            "cik": "0001318605",
            "collected_at": "2026-08-07T22:00:00+09:00",
            "filings": [filing("10-Q"), filing("8-K")],
        }
        data.update(overrides)
        return data

    def summarize(self, data=None, asset=None):
        if data is not None:
            self.write("filings/TSLA.json", data)
        return context._filing_summary(asset or self.ASSET)

    def test_an_etf_has_no_filing_index(self):
        summary, source = context._filing_summary(
            {"asset_id": "SCHD", "asset_type": "ETF", "market": "US"})
        self.assertIsNone(summary)
        self.assertIsNone(source)

    def test_a_missing_index_still_reports_where_it_would_be(self):
        """출처를 지우면 '안 모았다'와 '모을 곳이 없다'가 구분되지 않는다."""
        summary, source = self.summarize()
        self.assertIsNone(summary)
        self.assertEqual(source["source_id"], "FILINGS:TSLA")
        self.assertIn("filings/TSLA.json", source["path"])

    def test_unreadable_json_becomes_a_contract_error(self):
        self.write("filings/TSLA.json", "{ 망가진")
        summary, _source = context._filing_summary(self.ASSET)
        self.assertIn("공시 색인 JSON을 읽을 수 없다", summary["contract_errors"])

    def test_a_wrong_schema_version_is_refused(self):
        summary, _ = self.summarize(self.index(schema_version="0.9.0"))
        self.assertIn("filing-index/1.0.0이 아니다", summary["contract_errors"])

    def test_an_index_for_another_asset_is_refused(self):
        """다른 종목의 공시를 이 종목 판단에 싣는 것이 최악이다."""
        summary, _ = self.summarize(self.index(asset_id="IBM"))
        self.assertIn("공시 색인의 asset_id가 registry와 다르다",
                      summary["contract_errors"])

    def test_an_empty_index_is_refused(self):
        summary, _ = self.summarize(self.index(filings=[]))
        self.assertIn("공시 색인에 제출 문서가 없다", summary["contract_errors"])

    def test_an_index_without_a_periodic_report_is_refused(self):
        """8-K 만 있는 색인은 정기보고서를 못 받았다는 뜻이다."""
        summary, _ = self.summarize(self.index(filings=[filing("8-K")]))
        self.assertIn("공시 색인의 제출 문서가 유효하지 않다",
                      summary["contract_errors"])

    def test_periodic_reports_and_events_are_separated(self):
        summary, _ = self.summarize(self.index())
        self.assertEqual(len(summary["latest_primary_reports"]), 1)
        self.assertEqual(summary["latest_primary_reports"][0]["form"], "10-Q")
        self.assertEqual(summary["latest_material_events"][0]["form"], "8-K")

    def test_the_collection_time_becomes_the_source_date(self):
        _summary, source = self.summarize(self.index())
        self.assertEqual(source["data_as_of"], "2026-08-07T22:00:00+09:00")

    def test_empty_keys_are_dropped_rather_than_sent_as_null(self):
        """DART 는 report_date 를 주지 않는다. null 8건이 매번 실려 나갔다."""
        summary, _ = self.summarize(self.index())
        self.assertNotIn("report_date", summary["latest_primary_reports"][0])

    def test_a_present_key_survives(self):
        summary, _ = self.summarize(self.index(
            filings=[filing("10-Q", report_date="2026-06-30")]))
        self.assertEqual(
            summary["latest_primary_reports"][0]["report_date"], "2026-06-30")

    def test_sec_item_labels_survive(self):
        """8-K 31건이 전부 '8-K' 면 임상 결과와 임원 교체가 구분되지 않는다."""
        summary, _ = self.summarize(self.index(filings=[
            filing("10-Q"), filing("8-K", item_labels=["Item 2.02"])]))
        self.assertEqual(
            summary["latest_material_events"][0]["item_labels"], ["Item 2.02"])

    def test_the_lists_are_capped(self):
        summary, _ = self.summarize(self.index(filings=(
            [filing("10-Q", f"2026-0{i}-01") for i in range(1, 7)]
            + [filing("8-K", f"2026-0{i}-02") for i in range(1, 10)]
        )))
        self.assertEqual(len(summary["latest_primary_reports"]), 4)
        self.assertEqual(len(summary["latest_material_events"]), 8)

    def test_a_non_object_filing_is_skipped_not_crashed(self):
        summary, _ = self.summarize(self.index(
            filings=[filing("10-Q"), "망가진 행"]))
        self.assertEqual(len(summary["latest_primary_reports"]), 1)

    def test_the_note_says_the_index_may_be_incomplete(self):
        """'여기 없으면 없다'로 읽히면 안 된다."""
        summary, _ = self.summarize(self.index())
        self.assertIn("원문을 직접", summary["_note"])


class TestKoreanFilingIndex(PacketPart):
    """DART 는 서식 갈래도 부가 필드도 다르다."""

    ASSET = {"asset_id": "000660", "asset_type": "STOCK", "market": "KR"}

    def summarize(self, **overrides):
        data = {
            "schema_version": "filing-index/1.0.0",
            "asset_id": "000660",
            "source": "DART_LIST",
            "corp_code": "00164779",
            "since": "20250101",
            "collected_at": "2026-08-07T22:00:00+09:00",
            "filings": [filing("QUARTER_REPORT"),
                        filing("OTHER", remark="유")],
        }
        data.update(overrides)
        self.write("filings/000660.json", data)
        return context._filing_summary(self.ASSET)[0]

    def test_dart_report_forms_are_recognised(self):
        summary = self.summarize()
        self.assertEqual(summary["latest_primary_reports"][0]["form"],
                         "QUARTER_REPORT")

    def test_the_corp_code_travels_with_it(self):
        """원문을 다시 열려면 corp_code 가 필요하다."""
        summary = self.summarize()
        self.assertEqual(summary["corp_code"], "00164779")
        self.assertEqual(summary["since"], "20250101")

    def test_a_dart_remark_survives(self):
        self.assertEqual(
            self.summarize()["latest_material_events"][0]["remark"], "유")

    def test_dart_forms_are_labelled_as_our_own_grouping(self):
        """법정 서식 코드로 오해하면 없는 규정을 근거로 삼는다."""
        self.assertIn("법정 서식 코드가", self.summarize()["_form_note"])

    def test_a_sec_form_is_not_a_korean_periodic_report(self):
        """서식 집합이 섞이면 10-Q 가 한국 정기보고서 자리에 앉는다."""
        summary = self.summarize(filings=[filing("10-Q")])
        self.assertIn("contract_errors", summary)


class TestMarketRegime(PacketPart):
    """시장 국면. 안 실리면 '국면을 안 본다'로 읽힌다."""

    def regime(self, **overrides):
        data = {
            "schema_version": "market-regime/2.0.0",
            "collected_at": "2026-08-07",
            "by_market": {"KR": {"regime": "RISK_ON", "score": 62.0,
                                 "notes": ["외국인 순매수"],
                                 "volatility": {"vkospi": 18.2},
                                 "indices": [{"symbol": "^KS11"}]}},
            "input_sources": [
                {"symbol": "^KS11", "mode": "CLOSE", "data_as_of": "2026-08-07"},
                {"symbol": "^VKOSPI", "mode": "CLOSE",
                 "data_as_of": "2026-08-05"},
            ],
            "macro": {"by_market": {"KR": [{"name": "기준금리", "value": 2.5}]}},
        }
        data.update(overrides)
        self.write("market/regime-KR.json", data)
        return context._market_summary("KR")

    def test_a_missing_file_reports_where_it_would_be(self):
        summary, source = context._market_summary("KR")
        self.assertIsNone(summary)
        self.assertIn("regime-KR.json", source["path"])

    def test_broken_json_yields_nothing_rather_than_a_crash(self):
        self.write("market/regime-KR.json", "{ 망가진")
        summary, _ = context._market_summary("KR")
        self.assertIsNone(summary)

    def test_an_old_schema_is_not_read(self):
        summary, _ = self.regime(schema_version="market-regime/1.0.0")
        self.assertIsNone(summary)

    def test_a_market_without_a_block_yields_nothing(self):
        summary, _ = self.regime(by_market={"US": {"regime": "RISK_OFF"}})
        self.assertIsNone(summary)

    def test_the_regime_and_its_score_are_carried(self):
        summary, _ = self.regime()
        self.assertEqual(summary["regime"], "RISK_ON")
        self.assertEqual(summary["score"], 62.0)
        self.assertEqual(summary["notes"], ["외국인 순매수"])

    def test_freshness_is_the_oldest_input_not_the_collection_time(self):
        """가장 낡은 입력이 이 블록의 나이다 — 수집 시각은 그걸 감춘다."""
        _summary, source = self.regime()
        self.assertEqual(source["data_as_of"], "2026-08-05")

    def test_without_input_dates_the_collection_time_is_used(self):
        _summary, source = self.regime(input_sources=[])
        self.assertEqual(source["data_as_of"], "2026-08-07")

    def test_macro_falls_back_to_a_flat_series(self):
        """옛 파일은 시장별로 나뉘어 있지 않다."""
        summary, _ = self.regime(
            macro={"series": [{"name": "미 10년물", "value": 4.1}]})
        self.assertEqual(summary["macro"][0]["name"], "미 10년물")

    def test_the_block_says_it_is_not_an_instruction(self):
        summary, _ = self.regime()
        self.assertIn("지시가 아니다", summary["_role"])


class TestPositionMathAttachment(PacketPart):
    """원장 산술 배선. 산술 자체는 `calculators.position` 이 따로 검증한다."""

    ASSET = {"asset_id": "000660", "currency": "KRW"}

    def held(self, **overrides):
        block = {"available": True,
                 "existing_position": {"held": True, "quantity": 10}}
        block.update(overrides)
        return block

    def attach(self, portfolio, trades=None):
        """`calculators.position` 은 자기 경로 상수를 import 시점에 굳힌다.

        `cio.context` 만 임시 저장소로 돌려놓으면 존재 검사는 통과하고 실제
        읽기는 진짜 `knowledge/` 를 본다 — 둘을 같이 걸어야 원장 경로가 하나가
        된다.
        """
        if trades is not None:
            self.write("portfolio/trades/000660.csv", trades)
        with patch.object(position_module, "TRADES_DIR",
                          self.knowledge / "portfolio" / "trades"):
            return context._attach_position_math(
                portfolio, self.ASSET, 1_000_000.0)

    def test_no_portfolio_means_nothing_to_attach(self):
        self.assertIsNone(self.attach(None))

    def test_an_unavailable_portfolio_is_left_alone(self):
        self.assertIsNone(self.attach({"available": False}))

    def test_a_position_not_held_gets_no_ledger(self):
        portfolio = {"available": True, "existing_position": {"held": False}}
        self.assertIsNone(self.attach(portfolio))

    def test_a_missing_trade_ledger_says_so_in_place(self):
        """없다는 사실이 남아야 평단이 역산값 하나뿐임을 모델이 안다."""
        portfolio = self.held()
        self.attach(portfolio)
        ledger = portfolio["existing_position"]["ledger"]
        self.assertFalse(ledger["available"])
        self.assertIn("trades/000660.csv", ledger["_reason"])

    def test_a_broken_ledger_reports_its_reason_rather_than_vanishing(self):
        portfolio = self.held()
        self.attach(portfolio, trades="이건,CSV,가,아니다\n")
        ledger = portfolio["existing_position"]["ledger"]
        self.assertFalse(ledger["available"])
        self.assertTrue(ledger["_reason"])

    LEDGER = (
        "settle_date,side,quantity,price_krw,fee_krw,tax_krw,note\n"
        "2026-02-09,BUY,10,300000,0,0,\n"
        "2026-03-09,BUY,10,200000,0,0,\n"
        "2026-04-09,SELL,5,400000,0,500,\n"
    )

    def test_a_real_ledger_is_folded_into_the_position(self):
        portfolio = self.held()
        self.attach(portfolio, trades=self.LEDGER)
        ledger = portfolio["existing_position"]["ledger"]
        self.assertTrue(ledger["available"])
        self.assertEqual(ledger["quantity"], 15)
        self.assertEqual(ledger["trade_count"], 3)

    def test_the_moving_average_cost_comes_from_the_ledger(self):
        """10주 30만 + 10주 20만 = 평단 25만. 5주 매도해도 평단은 그대로다."""
        portfolio = self.held()
        self.attach(portfolio, trades=self.LEDGER)
        self.assertEqual(
            portfolio["existing_position"]["ledger"]["average_cost"], 250_000.0)

    def test_realized_pnl_exists_only_in_the_ledger(self):
        """증권사 스냅샷에는 실현손익이 없다 — 여기서만 나온다."""
        portfolio = self.held()
        self.attach(portfolio, trades=self.LEDGER)
        self.assertIsNotNone(
            portfolio["existing_position"]["ledger"]["realized_pnl"])

    def test_the_snapshot_keeps_authority_over_the_ledger(self):
        """원장은 교차검증값이다 — 권위를 뒤집으면 결론이 바뀐다."""
        portfolio = self.held()
        self.attach(portfolio, trades=self.LEDGER)
        role = portfolio["existing_position"]["ledger"]["_role"]
        self.assertIn("평단의 권위는", role)
        self.assertIn("교차검증값", role)


class TestEvidenceSnapshot(PacketPart):
    """근거를 내용 주소로 얼려 두는 자리. 어느 테스트에도 없던 함수다."""

    def origin(self, name="price.csv", body="date,close\n2026-08-08,1422000\n"):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return path, content_hash(path)

    def test_a_copy_lands_under_its_own_digest(self):
        origin, digest = self.origin()
        snapshot = context._snapshot_evidence(origin, digest)
        self.assertEqual(snapshot.name, f"{digest}.csv")
        self.assertEqual(content_hash(snapshot), digest)

    def test_the_extension_is_kept_so_the_file_stays_readable(self):
        origin, digest = self.origin("report.md", "# 제목\n")
        self.assertTrue(context._snapshot_evidence(origin, digest).name
                        .endswith(".md"))

    def test_a_file_without_an_extension_gets_bin(self):
        origin, digest = self.origin("payload", "본문")
        self.assertTrue(context._snapshot_evidence(origin, digest).name
                        .endswith(".bin"))

    def test_snapshotting_twice_is_the_same_file(self):
        """같은 내용은 한 번만 저장한다 — 그것이 내용 주소의 뜻이다."""
        origin, digest = self.origin()
        first = context._snapshot_evidence(origin, digest)
        second = context._snapshot_evidence(origin, digest)
        self.assertEqual(first, second)

    def test_a_stored_object_that_no_longer_matches_its_name_is_fatal(self):
        """이미 있는 파일이 자기 이름과 다르면 근거 저장소가 깨진 것이다."""
        origin, digest = self.origin()
        snapshot = context._snapshot_evidence(origin, digest)
        snapshot.write_text("누가 고쳤다", encoding="utf-8")
        with self.assertRaises(ContextError) as caught:
            context._snapshot_evidence(origin, digest)
        self.assertIn("지문이 충돌했다", str(caught.exception))

    def test_the_original_is_not_moved(self):
        origin, digest = self.origin()
        context._snapshot_evidence(origin, digest)
        self.assertTrue(origin.exists())

    def test_no_temporary_files_are_left_behind(self):
        """중간 파일이 남으면 다음 실행이 그것을 근거로 센다."""
        origin, digest = self.origin()
        context._snapshot_evidence(origin, digest)
        objects = self.knowledge / "evidence" / "objects"
        self.assertEqual([p.name for p in objects.iterdir()],
                         [f"{digest}.csv"])


if __name__ == "__main__":
    unittest.main()
