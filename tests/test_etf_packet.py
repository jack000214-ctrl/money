"""ETF 가 packet 에 실리는 모양.

이 저장소는 단일 종목 위주로 다듬어졌고, ETF 를 실제로 물어 보니(2026-10-09
TIGER 차이나휴머노이드로봇) 주식용 표가 그대로 실려 나갔다.

  1. **`data_coverage` 가 주식의 것이었다.** ETF 에 "컨센서스를 refresh 로
     받으라", "재무 스냅샷에 희석주식수가 없다"가 실렸다. 영원히 채워지지 않는
     요청이고, 정작 ETF 에서 결론을 바꾸는 입력이 있는지는 어디에도 없었다.
  2. **이력이 한 행뿐인 벤치마크가 경고 없이 실렸다.** 수익률은 전부 null,
     고점 대비 낙폭은 0.0 — "고점에 있다"로 읽힌다.
  3. **조사 항목이 없는 렌즈가 조용히 빈손으로 나갔다.**
  4. **예산을 안 줬는데 "요청 금액이 현금을 넘는다"고 했다.**
  5. **사후 평가가 다른 통화의 지수와 그대로 차감했다.**
"""

import datetime as dt
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio import assets, context, memory, refresh
from cio.decision import CLAIM_EVIDENCE_FAMILIES, _evidence_family

ETF = {
    "asset_id": "0053L0", "name": "테스트 해외 테마 ETF", "market": "KR",
    "exchange": "KOSPI", "asset_type": "ETF", "currency": "KRW",
    "price_symbol": "0053L0.KS", "sector_code": "THEMATIC",
    "analysis_lens": "THEMATIC_EQUITY_ETF", "valuation_family": ["ETF_LOOKTHROUGH"],
    "benchmarks": ["399001.SZ"], "optional_collectors": ["ETF_CONSTITUENTS"],
    "etf": {"strategy": "PASSIVE_THEMATIC", "derivative_use": False,
            "leverage": 1.0, "holdings_source": "ISSUER",
            "distribution_policy": "DIVIDEND", "issuer": "MIRAE_TIGER",
            "isin": "KR70053L0005", "currency_hedged": False},
}
HOLDINGS_BLOCK = {"as_of": "2026-10-08", "known_coverage_pct": 100.16,
                  "by_market_pct": {"CN": 65.24, "HK": 34.92}}
LOOKTHROUGH = {
    "collected_at": "2026-10-08T17:54:22+00:00",
    "valuation": {"earnings_yield_weight_pct": 100.16},
    "return_attribution": {"3m": {"covered_weight_pct": 98.0}},
}


class TestEtfCoverageIsNotTheStockTable(unittest.TestCase):
    def coverage(self, asset=ETF, **etf_block):
        return context._data_coverage(asset, etf={**HOLDINGS_BLOCK, **etf_block})

    def test_company_rows_are_gone(self):
        """ETF 에는 컨센서스·희석·부문 매출이 없다. 받으라고 말하지 않는다."""
        rows = self.coverage()
        for key in ("forward_estimates", "dilution", "segment_revenue",
                    "inventory", "normalized_earnings", "peer_multiples"):
            self.assertNotIn(key, rows)

    def test_the_holdings_row_carries_date_and_source_grade(self):
        row = self.coverage()["holdings"]
        self.assertEqual((row["state"], row["as_of"], row["source_grade"]),
                         ("AVAILABLE", "2026-10-08", "ISSUER"))

    def test_registered_but_not_collected_says_how_to_collect(self):
        row = self.coverage()["lookthrough_valuation"]
        self.assertEqual(row["state"], "REGISTERED_NOT_COLLECTED")
        self.assertIn("--optional", row["note"])

    def test_not_registered_is_a_different_state(self):
        asset = {**ETF, "optional_collectors": []}
        row = self.coverage(asset)["return_attribution"]
        self.assertEqual(row["state"], "NOT_REGISTERED")
        self.assertIn("ETF_CONSTITUENTS", row["note"])

    def test_available_rows_say_how_much_weight_they_cover(self):
        rows = self.coverage(lookthrough=LOOKTHROUGH)
        self.assertEqual(rows["lookthrough_valuation"]["state"], "AVAILABLE")
        self.assertEqual(rows["lookthrough_valuation"]["covered_weight_pct"], 100.16)
        self.assertEqual(rows["return_attribution"]["covered_weight_pct"], 98.0)

    def test_the_date_is_the_korean_date_of_collection(self):
        """17:54 UTC 는 한국 시각으로 다음 날이다. 파일명과 같은 날짜여야 한다."""
        row = self.coverage(lookthrough=LOOKTHROUGH)["lookthrough_valuation"]
        self.assertEqual(row["as_of"], "2026-10-09")

    def test_material_built_on_old_holdings_is_not_called_available(self):
        stale = {**LOOKTHROUGH, "_warning": "구성이 바뀌었다"}
        row = self.coverage(lookthrough=stale)["lookthrough_valuation"]
        self.assertEqual(row["state"], "STALE_HOLDINGS")

    def test_a_broken_artifact_is_invalid_not_missing(self):
        row = self.coverage(
            lookthrough={"contract_errors": ["asset_id가 다르다"]}
        )["lookthrough_valuation"]
        self.assertEqual(row["state"], "INVALID")

    def test_issuer_facts_distinguish_no_route_from_not_collected(self):
        self.assertEqual(self.coverage()["issuer_facts"]["state"],
                         "REGISTERED_NOT_COLLECTED")
        other = {**ETF, "etf": {**ETF["etf"], "issuer": "SOMEONE_ELSE"}}
        self.assertEqual(self.coverage(other)["issuer_facts"]["state"],
                         "NO_COLLECTOR_FOR_ISSUER")

    def test_what_has_no_collector_is_said_plainly(self):
        rows = self.coverage()
        self.assertEqual(rows["nav_history"]["state"], "NO_COLLECTOR")
        self.assertEqual(rows["index_methodology"]["state"], "NO_COLLECTOR")

    def test_a_stock_still_gets_the_stock_table(self):
        stock = {"asset_id": "005930", "market": "KR", "asset_type": "STOCK",
                 "optional_collectors": []}
        self.assertIn("forward_estimates", context._data_coverage(stock))


PRICE_HEADER = (
    "# schema_version price-series/2.1.0\n"
    "# symbol {symbol} / currency {currency}\n"
    "# retrieved_at 2026-10-08T17:00:00+00:00 / source Yahoo Finance chart API\n"
    "# last_bar {last} / session SETTLED\n"
    "date,close,adjusted_close,split_adjustment_factor,high,low,volume\n"
)


class PriceFiles(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "knowledge" / "prices").mkdir(parents=True)

    def write(self, symbol, closes, *, currency="KRW", start="2026-01-02"):
        day = dt.date.fromisoformat(start)
        rows = []
        for close in closes:
            while day.weekday() >= 5:
                day += dt.timedelta(days=1)
            rows.append(f"{day.isoformat()},{close},{close},1,{close},{close},1000")
            day += dt.timedelta(days=1)
        last = rows[-1].split(",")[0]
        path = self.root / "knowledge" / "prices" / f"{refresh.safe_symbol(symbol)}.csv"
        path.write_text(
            PRICE_HEADER.format(symbol=symbol, currency=currency, last=last)
            + "\n".join(rows) + "\n", encoding="utf-8")
        return path


class TestThinPriceHistory(PriceFiles):
    def summary(self, symbol, closes):
        self.write(symbol, closes)
        with patch.object(context, "KNOWLEDGE_DIR", self.root / "knowledge"), \
                patch.object(context, "ROOT", self.root):
            return context._price_summary(symbol)[0]

    def test_one_bar_is_not_a_drawdown_of_zero(self):
        """한 행이면 고점이 곧 현재가라 0.0 이 나왔다. 관측이 아니다."""
        summary = self.summary("000300.SS", [4310.28])
        assert summary is not None
        self.assertIsNone(summary["drawdown_from_12m_high_pct"])
        self.assertEqual(summary["history_bars"], 1)
        self.assertIn("1행뿐", summary["_history_warning"])

    def test_a_series_shorter_than_a_year_says_so(self):
        summary = self.summary("NEW.KS", [100 + i for i in range(60)])
        assert summary is not None
        self.assertEqual(summary["history_bars"], 60)
        self.assertIn("12개월 창보다 짧다", summary["_history_warning"])
        assert summary["returns_pct"] is not None
        self.assertIsNotNone(summary["returns_pct"]["1m"])
        self.assertIsNone(summary["returns_pct"]["12m"])

    def test_a_full_history_carries_no_warning(self):
        summary = self.summary("OLD.KS", [100 + i % 7 for i in range(300)])
        assert summary is not None
        self.assertNotIn("_history_warning", summary)
        self.assertNotIn("history_bars", summary)


class TestResearchRequirements(unittest.TestCase):
    def topics(self, asset, etf=None):
        return {item["topic"]: item
                for item in context._research_requirements(asset, None, etf)}

    def test_the_thematic_lens_has_its_own_items(self):
        topics = self.topics(ETF, HOLDINGS_BLOCK)
        self.assertIn("etf_structure", topics)
        self.assertIn("theme_purity", topics)

    def test_a_lens_nobody_wrote_items_for_says_so(self):
        """새 렌즈 이름을 지어 등록하면 그 자산만 렌즈가 없는 것과 같아진다."""
        topics = self.topics({**ETF, "analysis_lens": "BRAND_NEW_LENS"})
        self.assertIn("lens_without_requirements", topics)
        self.assertIn("BRAND_NEW_LENS",
                      topics["lens_without_requirements"]["known_local_gap"])

    def test_foreign_holdings_bring_the_currency_and_market_hours_item(self):
        item = self.topics(ETF, HOLDINGS_BLOCK)["fx_and_underlying_market_hours"]
        self.assertIn("환헤지를 하지 않는다", item["known_local_gap"])
        self.assertIn("return_attribution", item["instruction"])

    def test_an_unknown_hedge_status_is_stated_as_unknown(self):
        asset = {**ETF, "etf": {k: v for k, v in ETF["etf"].items()
                                if k != "currency_hedged"}}
        item = self.topics(asset, HOLDINGS_BLOCK)["fx_and_underlying_market_hours"]
        self.assertIn("환헤지 여부가 없다", item["known_local_gap"])

    def test_a_domestic_etf_does_not_get_it(self):
        domestic = {**HOLDINGS_BLOCK, "by_market_pct": {"KR": 100.0}}
        self.assertNotIn("fx_and_underlying_market_hours", self.topics(ETF, domestic))

    def test_a_stock_does_not_get_it(self):
        stock = {"asset_id": "005930", "market": "KR", "asset_type": "STOCK",
                 "analysis_lens": "SEMICONDUCTOR_CYCLE"}
        self.assertNotIn("fx_and_underlying_market_hours", self.topics(stock))


class TestIssuerFacts(unittest.TestCase):
    def facts(self, price):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        directory = root / "knowledge" / "etfs" / "0053L0"
        directory.mkdir(parents=True)
        (directory / "issuer.json").write_text(
            '{"schema_version": "etf-issuer/1.0.0", "asset_id": "0053L0", '
            '"as_of": "2026-10-08", "nav": 8251.92, "nav_currency": "KRW", '
            '"total_fee_pct": 0.49, "hedge_statement": "환헤지를 하지 아니함"}',
            encoding="utf-8")
        with patch.object(context, "KNOWLEDGE_DIR", root / "knowledge"), \
                patch.object(context, "ROOT", root):
            return context._etf_issuer_facts(ETF, price)

    def price(self, as_of="2026-10-08", settled=True):
        return {"as_of": as_of, "close": 8245.0,
                "last_bar": {"is_settled_close": settled}}

    def test_same_day_settled_close_is_compared_with_nav(self):
        block, source = self.facts(self.price())
        assert block is not None
        assert source is not None
        self.assertEqual(block["price_vs_nav_pct"], -0.08)
        self.assertEqual(source["source_id"], "ETF_ISSUER:0053L0")

    def test_a_different_date_is_not_compared(self):
        block, _ = self.facts(self.price(as_of="2026-10-12"))
        assert block is not None
        self.assertIsNone(block["price_vs_nav_pct"])

    def test_an_unsettled_bar_is_not_compared(self):
        block, _ = self.facts(self.price(settled=False))
        assert block is not None
        self.assertIsNone(block["price_vs_nav_pct"])

    def test_the_hedge_statement_is_kept_verbatim(self):
        block, _ = self.facts(self.price())
        assert block is not None
        self.assertEqual(block["hedge_statement"], "환헤지를 하지 아니함")


class TestRefreshPlanning(unittest.TestCase):
    def tasks(self, asset):
        with patch.object(refresh, "get_asset", return_value=asset):
            return {task.name: task
                    for task in refresh.tasks_for(asset["asset_id"], include_optional=True)}

    def test_a_supported_issuer_gets_a_collection_command(self):
        task = self.tasks(ETF)["holdings:0053L0"]
        assert task.command is not None
        self.assertIn("collectors.kr_etf_holdings", task.command)
        self.assertTrue(task.required)

    def test_an_unsupported_issuer_is_still_a_manual_task(self):
        """자동 수집이 없다는 사실을 숨기지 않는다."""
        asset = {**ETF, "etf": {**ETF["etf"], "issuer": "SOMEONE_ELSE"}}
        task = self.tasks(asset)["holdings:0053L0"]
        self.assertIsNone(task.command)
        assert task.reason is not None
        self.assertIn("etf.issuer", task.reason)

    def test_an_issuer_without_an_isin_is_manual(self):
        asset = {**ETF, "etf": {k: v for k, v in ETF["etf"].items() if k != "isin"}}
        self.assertIsNone(self.tasks(asset)["holdings:0053L0"].command)

    def test_constituents_are_optional_and_follow_the_holdings(self):
        task = self.tasks(ETF)["constituents:0053L0"]
        self.assertFalse(task.required)
        self.assertEqual(task.dependencies, ["knowledge/etfs/0053L0/holdings-*.csv"])

    def test_constituents_are_not_planned_unless_registered(self):
        asset = {**ETF, "optional_collectors": []}
        self.assertNotIn("constituents:0053L0", self.tasks(asset))


class TestConstituentsContractInRefresh(unittest.TestCase):
    def check(self, *, material_as_of, holdings_as_of):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        directory = root / "knowledge" / "etfs" / "0053L0"
        directory.mkdir(parents=True)
        (directory / f"holdings-{holdings_as_of}.csv").write_text("x\n", encoding="utf-8")
        path = directory / "constituents-2026-10-09.json"
        path.write_text(
            '{"schema_version": "etf-constituents/1.0.0", "asset_id": "0053L0", '
            '"collected_at": "2026-10-08T17:00:00+00:00", '
            f'"holdings_as_of": "{material_as_of}", '
            '"holdings": [{"holding_id": "2432.HK", "weight_pct": 10.21}]}',
            encoding="utf-8")
        task = refresh.RefreshTask(
            name="constituents:0053L0", capability="ETF_CONSTITUENTS",
            artifact="unused", max_age_hours=168, command=None)
        with patch.object(refresh, "ROOT", root):
            return refresh._artifact_contract_error(task, path)

    def test_material_on_the_current_holdings_passes(self):
        self.assertIsNone(self.check(material_as_of="2026-10-08",
                                     holdings_as_of="2026-10-08"))

    def test_material_on_older_holdings_is_refused(self):
        """구성이 바뀌었는데 옛 재료를 실으면 빠진 종목의 배수가 실린다."""
        error = self.check(material_as_of="2026-09-30", holdings_as_of="2026-10-08")
        assert error is not None
        self.assertIn("2026-09-30", error)


class TestRegistryFields(unittest.TestCase):
    def validate(self, **etf):
        asset = {key: value for key, value in ETF.items() if key != "asset_id"}
        asset["etf"] = {**ETF["etf"], **etf}
        assets.validate_asset("0053L0", asset)

    def test_the_registered_shape_is_valid(self):
        self.validate()

    def test_a_short_code_is_not_an_isin(self):
        """단축코드를 넣으면 다른 펀드의 구성을 받거나 아무것도 못 받는다."""
        with self.assertRaises(assets.AssetRegistryError):
            self.validate(isin="0053L0")

    def test_hedge_status_is_a_boolean_or_absent(self):
        with self.assertRaises(assets.AssetRegistryError):
            self.validate(currency_hedged="no")

    def test_a_fee_in_basis_points_is_refused(self):
        """0.49% 를 49 로 적는 실수를 막는다."""
        with self.assertRaises(assets.AssetRegistryError):
            self.validate(total_fee_pct=49)

    def test_constituents_collector_is_for_etfs_only(self):
        self.assertIn("ETF_CONSTITUENTS",
                      assets.OPTIONAL_COLLECTORS_BY_ROUTE[("KR", "ETF")])
        self.assertNotIn("ETF_CONSTITUENTS",
                         assets.OPTIONAL_COLLECTORS_BY_ROUTE[("KR", "STOCK")])


class TestEvidenceFamilies(unittest.TestCase):
    def test_constituent_material_is_secondary_evidence(self):
        family = _evidence_family("ETF_CONSTITUENTS:0053L0", "SECONDARY_MARKET_DATA")
        self.assertEqual(family, "SECONDARY")
        self.assertIn(family, CLAIM_EVIDENCE_FAMILIES["VALUATION"])
        self.assertNotIn(family, CLAIM_EVIDENCE_FAMILIES["EARNINGS"])

    def test_issuer_facts_sit_with_the_holdings(self):
        self.assertEqual(_evidence_family("ETF_ISSUER:0053L0", "ISSUER_FACTS"),
                         "ETF_HOLDINGS")


class TestBenchmarkInAnotherCurrency(PriceFiles):
    def horizons(self, *, benchmark_currency):
        self.write("399001.SZ", [100 + i for i in range(40)],
                   currency=benchmark_currency)
        rows = [{"date": f"2026-02-{day:02d}", "adjusted_close": 100.0 + day}
                for day in range(1, 20)]
        with patch.object(memory, "KNOWLEDGE_DIR", self.root / "knowledge"), \
                patch.object(memory, "ROOT", self.root):
            return memory._fixed_horizon_returns(
                rows=rows, start=dt.date(2026, 1, 30), adjusted_cost=100.0,
                benchmarks=["399001.SZ"], asset_currency="KRW")

    def test_a_different_currency_is_flagged(self):
        """원화 ETF 를 위안 지수와 차감하면 환율이 초과수익으로 보인다."""
        fixed = self.horizons(benchmark_currency="CNY")
        self.assertEqual(fixed["benchmark_currency"], {"399001.SZ": "CNY"})
        self.assertIn("환율", fixed["_currency_warning"])

    def test_the_same_currency_adds_nothing(self):
        fixed = self.horizons(benchmark_currency="KRW")
        self.assertNotIn("_currency_warning", fixed)
        self.assertNotIn("benchmark_currency", fixed)


@dataclass
class FreshTask:
    name: str
    required: bool = True
    state: str = "FRESH"
    reason: str | None = None


class TestCashGapNeedsAnAmount(unittest.TestCase):
    """예산이 없으면 현금을 넘고 말고가 없다."""

    def gaps(self, proposed_trade, *, amount):
        price = {"symbol": "0167A0", "currency": "USD", "as_of": "2026-07-27",
                 "close": 99.0, "returns_pct": {"1m": 1.0, "3m": 2.0, "12m": 3.0},
                 "returns_basis": "ADJUSTED_CLOSE"}
        source = {"source_id": "PRICE:0167A0", "kind": "MARKET_DATA",
                  "path": None, "data_as_of": "2026-07-27"}
        fundamentals = ({"fundamentals_id": "FUND-x", "period_end": "2026-06-30"},
                        {"source_id": "FUNDAMENTALS:0167A0", "kind": "FILING_DERIVED",
                         "path": None, "data_as_of": "2026-06-30"})
        filings = ({"collected_at": "2026-07-28T00:00:00+00:00",
                    "latest": [{"form": "10-Q"}]},
                   {"source_id": "FILINGS:0167A0", "kind": "REGULATOR_FILING_INDEX",
                    "path": None, "data_as_of": "2026-07-28T00:00:00+00:00"})
        portfolio = {"available": True, "existing_position": {"held": False},
                     "proposed_trade": proposed_trade}
        with (
            patch("cio.context._price_summary", return_value=(price, source)),
            patch("cio.context._latest_fundamentals", return_value=fundamentals),
            patch("cio.context._filing_summary", return_value=filings),
            patch("cio.context._latest_calculation", return_value=(None, None)),
            patch("cio.context._market_summary", return_value=(
                {"market": "US"}, {"source_id": "MARKET_STATE:US", "kind": "DERIVED",
                                   "path": None, "data_as_of": "2026-07-27"})),
            patch("cio.context._event_summary", return_value=(None, None)),
            patch("cio.context._etf_summary", return_value=(None, None)),
            patch("cio.context._portfolio_summary", return_value=(portfolio, None)),
            patch("cio.context._attach_position_math", return_value=None),
            patch("cio.context._memory_digest", return_value=[]),
            patch("cio.context.inspect_plan", return_value=[FreshTask("price:0167A0")]),
        ):
            packet, _ = context.build_context(
                "0167A0", price=100.0, amount=amount, amount_currency="KRW", save=False)
        return [gap for gap in packet["validation"]["optional_gaps"]
                if gap["task"] == "portfolio:cash"]

    def test_no_amount_means_no_cash_comparison(self):
        trade = {"side": "BUY", "amount_krw": None, "sufficient_cash": False}
        self.assertEqual(self.gaps(trade, amount=None), [])

    def test_an_amount_above_cash_is_still_reported(self):
        trade = {"side": "BUY", "amount_krw": 5e7, "sufficient_cash": False,
                 "estimated_quantity": 10}
        self.assertEqual(len(self.gaps(trade, amount=5e7)), 1)


if __name__ == "__main__":
    unittest.main()
