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
from collectors import kr_segments
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


class TestUnavailableIsNotUncollected(PacketPart):
    """돌렸는데 못 낸 것과 아직 안 받은 것은 다르다.

    `kr_segments` 는 SK하이닉스에서 부문 표를 찾지 못한다. 예전에는 그 사실이
    stderr 에만 찍히고 끝나서 파일이 안 생겼고, packet 은
    `REGISTERED_NOT_COLLECTED` — "refresh 로 받는다"로 읽었다. 몇 번을 다시
    돌려도 같은 결과인데 계속 받을 수 있는 것처럼 보였고, 모델은 그 요청을
    반복하게 된다.

    **단정하지는 않는다.** 수집기는 "단일 부문 회사"와 "표 형식이 다르다"를
    구분하지 못한다. 아는 것은 "이 보고서에서 표를 특정하지 못했다"까지다.
    """

    ASSET = {"asset_id": "000660", "market": "KR", "asset_type": "STOCK",
             "optional_collectors": ["KR_SEGMENTS"]}

    def summaries(self, packet):
        self.write("segments/000660.json", packet)
        found, _sources = context._optional_collection_summaries(self.ASSET)
        return found

    def unavailable(self, **overrides):
        data = {
            "schema_version": "segment-summary/1.0.0",
            "asset_id": "000660",
            "available": False,
            "report_name": "분기보고서 (2026.03)",
            "_reason": "사업부문별 요약 표를 특정하지 못했다",
            "_retry_note": "같은 보고서에 대해 다시 돌려도 결과는 같다",
        }
        data.update(overrides)
        return data

    def mix(self):
        return {
            "schema_version": "segment-summary/1.0.0",
            "asset_id": "000660",
            "report_name": "분기보고서 (2026.03)",
            "latest_period": "2026-03",
            "latest_mix": [{"name": "DS", "operating_income_share_pct": 93.8}],
        }

    def coverage(self, packet):
        return context._data_coverage(
            self.ASSET, structured_optional=self.summaries(packet),
            fundamentals=None)["segment_revenue"]

    def test_a_failed_lookup_still_reaches_the_packet(self):
        block = self.summaries(self.unavailable())["segments"]
        self.assertFalse(block["available"])
        self.assertIn("표를 특정하지 못했다", block["_reason"])

    def test_the_packet_says_retrying_will_not_help(self):
        block = self.summaries(self.unavailable())["segments"]
        self.assertIn("다시 돌려도", block["_retry_note"])

    def test_the_state_is_not_the_one_that_means_go_collect_it(self):
        self.assertEqual(self.coverage(self.unavailable())["state"],
                         "COLLECTOR_FOUND_NOTHING")

    def test_the_note_tells_the_model_not_to_request_it_again(self):
        self.assertIn("재수집을 요청하지 않는다",
                      self.coverage(self.unavailable())["note"])

    def test_a_real_mix_is_still_available(self):
        self.assertEqual(self.coverage(self.mix())["state"], "AVAILABLE")

    def test_a_missing_file_still_means_go_collect_it(self):
        """파일이 아예 없는 것은 여전히 '아직 안 받았다'이다."""
        found, _sources = context._optional_collection_summaries(self.ASSET)
        state = context._data_coverage(
            self.ASSET, structured_optional=found,
            fundamentals=None)["segment_revenue"]["state"]
        self.assertEqual(state, "REGISTERED_NOT_COLLECTED")

    def test_the_two_states_are_never_the_same(self):
        """이 둘을 섞으면 채워지지 않을 것을 계속 요청하게 된다."""
        self.assertNotEqual(self.coverage(self.unavailable())["state"],
                            "REGISTERED_NOT_COLLECTED")


class TestTheCollectorRecordsWhatItCouldNotFind(unittest.TestCase):
    """수집기가 그 관측을 **만들어 내는지**.

    packet 쪽만 검사하면 파일을 손으로 써 놓고 읽는 셈이라, 수집기가 다시
    `fail()` 로 돌아가도 아무도 모른다. 실제로 그 변이를 넣어 보니 이 파일의
    다른 테스트는 전부 통과했다.
    """

    FILING = {"accession_number": "20260515000123",
              "description": "분기보고서 (2026.03)",
              "filing_date": "2026-05-15", "report_date": "2026-03-31"}

    def collect(self, table, document="<html/>"):
        digest = "a" * 64
        with patch.object(kr_segments, "_periodic_filing",
                          return_value=self.FILING), \
                patch.object(kr_segments, "_fetched_document",
                             return_value=(document, Path(
                                 f"data/raw/dart/objects/{digest}.json"))), \
                patch.object(kr_segments, "find_segment_table",
                             return_value=table), \
                patch.object(kr_segments, "ROOT", Path(".")):
            return kr_segments.collect("000660")

    def test_a_missing_table_produces_a_record_not_an_exit(self):
        packet = self.collect(None)
        self.assertIs(packet["available"], False)

    def test_the_record_names_the_report_it_looked_at(self):
        """다음 정기보고서에서는 달라질 수 있으므로 어느 보고서인지가 남아야 한다."""
        packet = self.collect(None)
        self.assertEqual(packet["report_name"], "분기보고서 (2026.03)")
        self.assertEqual(packet["rcept_no"], "20260515000123")

    def test_without_a_declaration_it_does_not_guess(self):
        """단일 부문이라는 공시가 없으면 표 형식이 바뀐 것일 수 있다."""
        packet = self.collect(None)
        self.assertEqual(packet["absence"], "NO_TABLE_FOUND")
        self.assertIn("원문을 직접", packet["_reason"])
        self.assertIsNone(packet["single_segment_statement"])

    def test_a_declared_single_segment_is_a_fact_not_a_failure(self):
        """K-IFRS 1108 은 단일 영업부문인 회사도 그 사실을 공시하게 한다.

        추측할 필요가 없다 — 원문이 직접 말한다. 실제로 SK하이닉스 분기보고서에
        그 문장이 있고, 그것을 읽지 않으면 "표가 없다"까지밖에 말하지 못한다.
        """
        packet = self.collect(None, document=(
            "<p>4. 영업부문 (연결)</p>"
            "<p>연결회사는 반도체 제품의 제조 및 판매의 단일영업부문으로 "
            "구성되어 있습니다.</p>"))
        self.assertEqual(packet["absence"], "SINGLE_SEGMENT")
        self.assertIn("단일영업부문", packet["single_segment_statement"])

    def test_the_declaration_is_quoted_rather_than_summarised(self):
        """'단일 부문이다'만 남기면 우리 판정인지 회사의 공시인지 알 수 없다."""
        packet = self.collect(None, document=(
            "<p>연결회사는 반도체 제품의 제조 및 판매의 단일영업부문으로 "
            "구성되어 있습니다.</p>"))
        self.assertIn(packet["single_segment_statement"], packet["_reason"])

    def test_a_single_segment_company_is_not_asked_to_retry(self):
        packet = self.collect(None, document="<p>단일영업부문으로 구성됩니다.</p>")
        self.assertIn("다시 받을 것이 없다", packet["_retry_note"])

    def test_spacing_variants_of_the_declaration_are_recognised(self):
        for text in ("단일 영업 부문", "하나의 영업부문", "단일 보고 부문"):
            with self.subTest(text):
                packet = self.collect(
                    None, document=f"<p>회사는 {text}으로 구성되어 있습니다.</p>")
                self.assertEqual(packet["absence"], "SINGLE_SEGMENT")

    def test_the_record_points_at_the_raw_document(self):
        """판단이 걸리면 원문을 열어야 하므로 위치가 남아야 한다."""
        packet = self.collect(None)
        self.assertIn("data/raw/dart/objects/", packet["raw_locator"])
        self.assertEqual(len(packet["raw_payload_sha256"]), 64)

    def test_no_segment_allocation_is_invented(self):
        packet = self.collect(None)
        self.assertNotIn("latest_mix", packet)
        self.assertNotIn("segments", packet)


if __name__ == "__main__":
    unittest.main()
