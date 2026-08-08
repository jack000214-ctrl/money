"""저장된 산출물이 쓸 수 있는 상태인지 판정하는 자리.

`_artifact_contract_error` 는 수집기가 만든 파일을 **다시 읽을 때 통과시킬지**
정한다. 여기가 새면 모양이 깨진 파일이 FRESH 로 통과하고, packet 은 그 값을
그대로 싣는다. 화면에서도 packet 에서도 정상으로 보인다.

capability 마다 검사가 따로 있는데 실행률이 갈라져 있었다 — PORTFOLIO 4%,
EARNINGS_CALENDAR 5%, CONSENSUS 6%, EVENTS 11%, MARKET_STATE 14%. 즉 이
파일들은 사실상 아무 검사 없이 통과하고 있었던 것과 구분되지 않았다.

여기서 고정하는 성질은 하나다. **모양이 다르면 통과시키지 않는다.** 어떤 값이
옳은지는 다른 곳에서 보고, 이 계층은 "읽을 수 있는가"만 본다.
"""

import csv
import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio.refresh import RefreshTask, _artifact_contract_error


class ArtifactCase(unittest.TestCase):
    CAPABILITY = ""
    ASSET = "000660"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def task(self, capability=None, asset=None):
        return RefreshTask(
            name=f"x:{asset or self.ASSET}",
            capability=capability or self.CAPABILITY,
            artifact="unused",
            max_age_hours=720,
            command=None,
        )

    def check(self, payload, *, name="artifact.json", capability=None,
              asset=None):
        path = self.root / name
        path.write_text(
            payload if isinstance(payload, str)
            else json.dumps(payload, ensure_ascii=False),
            encoding="utf-8",
        )
        with patch("cio.refresh.ROOT", self.root):
            return _artifact_contract_error(
                self.task(capability, asset), path)

    def assertClean(self, payload, **kwargs):
        self.assertIsNone(self.check(payload, **kwargs))

    def assertFlags(self, payload, needle, **kwargs):
        error = self.check(payload, **kwargs)
        self.assertIsNotNone(error, f"{needle!r} 를 기대했는데 통과했다")
        self.assertIn(needle, error)


class TestConsensusArtifact(ArtifactCase):
    CAPABILITY = "CONSENSUS"

    def packet(self, **overrides):
        data = {
            "schema_version": "consensus-packet/1.0.0",
            "asset_id": "000660",
            "estimates": {"annual": [{"period": "2026-12", "per": 4.1}]},
            "dispersion": {"recent": {"count": 20, "spread_pct": 217.6}},
        }
        data.update(overrides)
        return data

    def test_a_valid_packet_passes(self):
        self.assertClean(self.packet())

    def test_unreadable_json_is_refused(self):
        self.assertFlags("{ 망가진", "컨센서스 packet을 읽을 수 없다")

    def test_an_old_schema_is_refused(self):
        self.assertFlags(self.packet(schema_version="0.9.0"),
                         "consensus-packet/1.0.0이 필요하다")

    def test_another_assets_consensus_is_refused(self):
        """다른 종목의 추정을 이 종목 밸류에이션에 쓰는 것이 최악이다."""
        self.assertFlags(self.packet(asset_id="005930"),
                         "asset_id가 요청 자산과 다르다")

    def test_a_packet_without_annual_estimates_is_refused(self):
        """목표주가만 있는 packet 은 결론만 있고 입력이 없는 상태다."""
        self.assertFlags(self.packet(estimates={"annual": []}),
                         "연도별 실적·추정이 없다")

    def test_a_packet_without_dispersion_is_refused(self):
        """평균만 남으면 '몇 곳이 얼마나 갈리는가'를 잃는다."""
        self.assertFlags(self.packet(dispersion={}),
                         "최근 목표가 분산이 없다")


class TestMarketStateArtifact(ArtifactCase):
    CAPABILITY = "MARKET_STATE"

    def test_a_valid_file_passes(self):
        self.assertClean({"schema_version": "market-regime/2.0.0"})

    def test_unreadable_json_is_refused(self):
        self.assertFlags("{ 망가진", "시장 상태 JSON을 읽을 수 없다")

    def test_an_old_schema_is_refused(self):
        """1.0.0 은 시장별로 나뉘어 있지 않아 KR 지표에 US 값이 섞인다."""
        self.assertFlags({"schema_version": "market-regime/1.0.0"},
                         "market-regime/2.0.0이 필요하다")


class TestEarningsCalendarArtifact(ArtifactCase):
    CAPABILITY = "EARNINGS_CALENDAR"

    def packet(self, **overrides):
        data = {
            "schema_version": "earnings-calendar/1.0.0",
            "asset_id": "000660",
            "next_provisional_result": {
                "disclosure_date": "2026-07-29",
                "evidence_ref": "DARTDOC:20260729800123",
            },
        }
        data.update(overrides)
        return data

    def test_a_valid_calendar_passes(self):
        self.assertClean(self.packet())

    def test_a_calendar_without_an_upcoming_date_passes(self):
        """다음 발표를 아직 모르는 것은 정상이다."""
        self.assertClean(self.packet(next_provisional_result=None))

    def test_an_old_schema_is_refused(self):
        self.assertFlags(self.packet(schema_version="0.9.0"),
                         "earnings-calendar/1.0.0이 필요하다")

    def test_another_assets_calendar_is_refused(self):
        self.assertFlags(self.packet(asset_id="TSLA"),
                         "asset_id가 요청 자산과 다르다")

    def test_a_non_object_entry_is_refused(self):
        self.assertFlags(self.packet(next_provisional_result="2026-07-29"),
                         "발표 예정 항목이 객체가 아니다")

    def test_a_date_without_a_filing_behind_it_is_refused(self):
        """추정한 날짜와 공시가 말한 날짜는 다르다 — 근거를 되짚을 수 있어야 한다."""
        self.assertFlags(self.packet(next_provisional_result={
            "disclosure_date": "2026-07-29", "evidence_ref": "추정"}),
            "공시 원문 근거가 없다")

    def test_a_missing_evidence_ref_is_refused(self):
        self.assertFlags(self.packet(next_provisional_result={
            "disclosure_date": "2026-07-29"}), "공시 원문 근거가 없다")

    def test_a_malformed_date_is_refused(self):
        self.assertFlags(self.packet(next_provisional_result={
            "disclosure_date": "2026년 7월 29일",
            "evidence_ref": "DARTDOC:1"}), "날짜 형식이 아니다")


class TestPortfolioArtifact(ArtifactCase):
    CAPABILITY = "PORTFOLIO"

    HEADER = "asset_id,risk_class,value_krw,quantity\n"

    def csv_text(self, body, header=None):
        return (header if header is not None else self.HEADER) + body

    def test_a_valid_ledger_passes(self):
        self.assertClean(self.csv_text("000660,GROWTH,1000000,10\n"),
                         name="holdings.csv")

    def test_comment_lines_are_ignored(self):
        """원장 맨 위의 메모가 헤더로 읽히면 안 된다."""
        self.assertClean("# 2026-08-08 기준\n"
                         + self.csv_text("000660,GROWTH,1000000,10\n"),
                         name="holdings.csv")

    def test_an_empty_ledger_is_refused(self):
        self.assertFlags(self.HEADER, "필수 열 또는 행이 없다",
                         name="holdings.csv")

    def test_a_missing_column_is_refused(self):
        """수량 열이 없으면 매도 수량을 낼 수 없다."""
        self.assertFlags(
            self.csv_text("000660,GROWTH,1000000\n",
                          header="asset_id,risk_class,value_krw\n"),
            "필수 열 또는 행이 없다", name="holdings.csv")

    def test_a_duplicate_asset_is_refused(self):
        """같은 종목이 두 줄이면 비중이 두 번 세어진다."""
        self.assertFlags(
            self.csv_text("000660,GROWTH,1000000,10\n"
                          "000660,GROWTH,2000000,20\n"),
            "asset_id가 중복됐다", name="holdings.csv")

    def test_a_non_numeric_value_is_refused(self):
        self.assertFlags(self.csv_text("000660,GROWTH,백만원,10\n"),
                         "value_krw가 숫자가 아니다", name="holdings.csv")

    def test_a_negative_value_is_refused(self):
        self.assertFlags(self.csv_text("000660,GROWTH,-1000,10\n"),
                         "value_krw가 유효하지 않다", name="holdings.csv")

    def test_a_non_finite_value_is_refused(self):
        """`float("nan")` 은 파싱을 통과하고 비중 계산을 통째로 오염시킨다."""
        self.assertFlags(self.csv_text("000660,GROWTH,nan,10\n"),
                         "value_krw가 유효하지 않다", name="holdings.csv")

    def test_zero_value_is_allowed(self):
        """전량 매도 직후의 줄이다 — 0 은 오류가 아니다."""
        self.assertClean(self.csv_text("000660,GROWTH,0,0\n"),
                         name="holdings.csv")


class TestEventsArtifact(ArtifactCase):
    CAPABILITY = "EVENTS"

    def test_unreadable_json_is_refused(self):
        self.assertFlags("{ 망가진", "이벤트 packet JSON을 읽을 수 없다",
                         asset="000660")

    def test_an_unknown_asset_is_refused(self):
        """registry 에 없는 종목의 이벤트는 대조할 기준이 없다."""
        error = self.check({"asset_id": "ZZZZZZ"}, asset="ZZZZZZ")
        self.assertIsNotNone(error)


class TestEtfHoldingsArtifact(ArtifactCase):
    """`_etf_summary` 가 읽기 전에 이 검사가 먼저 통과시킬지 정한다.

    두 곳이 같은 파일을 본다 — 여기(FRESH 판정)와 packet 조립. 한쪽만 막으면
    다른 쪽이 그대로 읽는다.
    """

    CAPABILITY = "ETF_HOLDINGS"
    ASSET = "SCHD"
    HEADER = "etf_id,holding_id,holding_name,weight_pct,as_of\n"

    def rows(self, body, header=None):
        return (header if header is not None else self.HEADER) + body

    def test_a_valid_file_passes(self):
        self.assertClean(self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"
            "SCHD,MSFT,Microsoft,4.1,2026-08-01\n"), name="h.csv")

    def test_comment_lines_are_ignored(self):
        self.assertClean("# 손으로 채운 파일\n" + self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"), name="h.csv")

    def test_an_empty_file_is_refused(self):
        self.assertFlags(self.HEADER, "필수 열 또는 행이 없다", name="h.csv")

    def test_a_missing_column_is_refused(self):
        """holding_name 이 없으면 무엇을 들고 있는지 화면에 못 쓴다."""
        self.assertFlags(
            self.rows("SCHD,AAPL,4.5,2026-08-01\n",
                      header="etf_id,holding_id,weight_pct,as_of\n"),
            "필수 열 또는 행이 없다", name="h.csv")

    def test_a_row_for_another_etf_is_refused(self):
        self.assertFlags(self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"
            "XLK,MSFT,Microsoft,4.1,2026-08-01\n"),
            "etf_id가 요청 자산과 다르다", name="h.csv")

    def test_a_blank_holding_id_is_refused(self):
        self.assertFlags(self.rows("SCHD,,Apple,4.5,2026-08-01\n"),
                         "holding_id가 비었거나 중복됐다", name="h.csv")

    def test_a_duplicate_holding_is_refused(self):
        """같은 종목을 두 번 적으면 비중이 두 번 세어진다."""
        self.assertFlags(self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"
            "SCHD,AAPL,Apple,4.1,2026-08-01\n"),
            "holding_id가 비었거나 중복됐다", name="h.csv")

    def test_mixed_as_of_dates_are_refused(self):
        """반쯤 갱신하다 만 파일은 한 시점의 구성이 아니다."""
        self.assertFlags(self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"
            "SCHD,MSFT,Microsoft,4.1,2026-07-01\n"),
            "기준일이 비었거나 행마다 다르다", name="h.csv")

    def test_a_blank_as_of_is_refused(self):
        self.assertFlags(self.rows("SCHD,AAPL,Apple,4.5,\n"),
                         "기준일이 비었거나 행마다 다르다", name="h.csv")

    def test_a_non_numeric_weight_is_refused(self):
        self.assertFlags(self.rows("SCHD,AAPL,Apple,4.5%,2026-08-01\n"),
                         "weight_pct가 숫자가 아니다", name="h.csv")

    def test_a_negative_weight_is_refused(self):
        self.assertFlags(self.rows("SCHD,AAPL,Apple,-4.5,2026-08-01\n"),
                         "weight_pct가 유효하지 않다", name="h.csv")

    def test_a_non_finite_weight_is_refused(self):
        self.assertFlags(self.rows("SCHD,AAPL,Apple,inf,2026-08-01\n"),
                         "weight_pct가 유효하지 않다", name="h.csv")

    def test_all_zero_weights_are_refused(self):
        """비중 칸을 안 채운 파일이 '구성 없음'으로 읽히면 안 된다."""
        self.assertFlags(self.rows("SCHD,AAPL,Apple,0,2026-08-01\n"),
                         "weight_pct가 유효하지 않다", name="h.csv")

    def test_a_single_zero_row_beside_real_weights_is_allowed(self):
        """편입 예정으로 0% 인 줄은 실제로 있다."""
        self.assertClean(self.rows(
            "SCHD,AAPL,Apple,4.5,2026-08-01\n"
            "SCHD,MSFT,Microsoft,0,2026-08-01\n"), name="h.csv")


if __name__ == "__main__":
    unittest.main()
