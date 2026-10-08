"""CIO 판단·기억·라우팅의 적대 경계 회귀 테스트."""

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from calculators.contracts import (
    ContractError,
    calculation_id_for,
    content_hash,
    fundamentals_id_for,
    stable_hash,
)
from cio.assets import AssetRegistryError, get_asset, validate_asset
from cio.context import (
    _latest_calculation,
    _latest_fundamentals,
    _memory_digest,
    _research_requirements,
)
from cio.decision import (
    context_id_for,
    normalize_decision,
    validate_context_integrity,
    validate_decision,
)
from cio.memory import record_decision_file, score_asset
from cio.refresh import (
    SEC_FILING_FORMS,
    RefreshTask,
    _artifact_contract_error,
    event_contract_errors,
    tasks_for,
)
from test_cio_core import valid_context, valid_decision
from test_fundamentals import valid_fundamentals


def insufficient_decision(context):
    data = valid_decision()
    data["context_id"] = context["context_id"]
    data["verdict"] = "INSUFFICIENT"
    data["win_probability_pct"] = None
    data["confidence_pct"] = None
    data["expected_return_pct"] = {
        "bear": None,
        "base": None,
        "bull": None,
        "probability_weighted": None,
    }
    data["scenario_probability_pct"] = {
        "bear": None,
        "base": None,
        "bull": None,
    }
    for key in (
        "preferred_entry",
        "entry_range_low",
        "entry_range_high",
        "stop_price",
        "scale_out_start",
        "reask_below",
        "reask_above",
    ):
        data["price_plan"][key] = None
    data["proposed_trade"]["amount"] = None
    # 판단이 서지 않으면 보유분에 대해서도 지시하지 않는다. 여기가 HOLD 면
    # 시스템이 "모른다"를 "계속 보유하라"로 번역한다.
    data["action"] = {
        "direction": "UNDETERMINED",
        "quantity": None,
        "sell_reason": None,
        "undetermined_reason": "INSUFFICIENT_EVIDENCE",
        "rationale": "결론을 지탱할 근거가 없어 보유분 행동도 정하지 않는다",
    }
    return data


class TestDecisionIntegrity(unittest.TestCase):
    def test_blocked_context_only_accepts_null_insufficient(self):
        context = valid_context()
        context["validation"]["status"] = "BLOCKED"
        context["validation"]["problems"] = ["price:TSLA: STALE"]
        context["context_id"] = context_id_for(context)

        numeric = valid_decision()
        numeric["context_id"] = context["context_id"]
        numeric["verdict"] = "BUY_NOW"
        errors = validate_decision(numeric, context=context)
        self.assertTrue(any("BLOCKED" in error for error in errors))

        insufficient = insufficient_decision(context)
        self.assertEqual(validate_decision(insufficient, context=context), [])
        insufficient["price_plan"]["preferred_entry"] = 90.0
        errors = validate_decision(insufficient, context=context)
        self.assertTrue(any("INSUFFICIENT" in error for error in errors))

    def test_context_content_fingerprint_detects_tampering(self):
        context = valid_context()
        self.assertEqual(validate_context_integrity(context), [])
        context["request"]["price"] = 999.0
        errors = validate_context_integrity(context)
        self.assertTrue(any("내용 지문" in error for error in errors))

    def test_context_detects_mutated_evidence_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            evidence = root / "evidence.csv"
            evidence.write_text("date,close\n2026-07-28,100\n", encoding="utf-8")
            context = valid_context()
            context["evidence_index"] = [
                {
                    "source_id": "PRICE:TSLA",
                    "path": "evidence.csv",
                    "content_sha256": content_hash(evidence),
                }
            ]
            context["context_id"] = context_id_for(context)
            with patch("cio.decision.ROOT", root):
                self.assertEqual(validate_context_integrity(context), [])
                evidence.write_text(
                    "date,close\n2026-07-28,999\n", encoding="utf-8"
                )
                errors = validate_context_integrity(context)
            self.assertTrue(any("파일 지문" in error for error in errors))

    def test_model_sources_cannot_shadow_local_evidence_or_reverse_time(self):
        context = valid_context()
        data = valid_decision()
        data["sources"] = [
            {
                "source_id": "PRICE:TSLA",
                "type": "BLOG",
                "locator": "not-a-url",
                "published_at": "2026-07-28T10:00:00+09:00",
                "retrieved_at": "2026-07-28T09:30:00+09:00",
            }
        ]
        errors = validate_decision(data, context=context)
        self.assertTrue(any("충돌" in error for error in errors))
        self.assertTrue(any("published_at" in error for error in errors))
        self.assertTrue(any("http(s)" in error for error in errors))

    def test_verdict_and_current_trade_amount_cannot_contradict(self):
        context = valid_context()
        buy = valid_decision()
        buy["verdict"] = "BUY_NOW"
        self.assertTrue(
            any(
                "0보다 커야" in error
                for error in validate_decision(buy, context=context)
            )
        )
        wait = valid_decision()
        wait["proposed_trade"]["amount"] = 1.0
        self.assertTrue(
            any(
                "현재 집행액은 0" in error
                for error in validate_decision(wait, context=context)
            )
        )


class TestMemorySafety(unittest.TestCase):
    def test_malformed_and_traversal_outputs_stay_in_quarantine(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            malformed = root / "malformed.json"
            malformed.write_text("```json\n{broken", encoding="utf-8")
            traversal = root / "traversal.json"
            data = valid_decision()
            data["asset_id"] = "../../escape"
            traversal.write_text(json.dumps(data), encoding="utf-8")
            with (
                patch("cio.memory.ROOT", root),
                patch("cio.memory.KNOWLEDGE_DIR", knowledge),
                patch("cio.decision.ROOT", root),
                patch("cio.decision.KNOWLEDGE_DIR", knowledge),
            ):
                with self.assertRaises(ContractError):
                    record_decision_file(malformed)
                with self.assertRaises(ContractError):
                    record_decision_file(traversal)
            files = list((knowledge / "quarantine" / "unknown").glob("*.json"))
            self.assertEqual(len(files), 2)
            self.assertFalse((root / "escape").exists())
            self.assertIn(
                "model_output_raw",
                json.loads(files[0].read_text(encoding="utf-8")),
            )

    def test_record_deduplicates_and_score_rejects_tampered_assessment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            knowledge = root / "knowledge"
            context = valid_context()
            context_path = (
                knowledge
                / "contexts"
                / "TSLA"
                / f"{context['context_id']}.json"
            )
            context_path.parent.mkdir(parents=True)
            context_path.write_text(json.dumps(context), encoding="utf-8")
            decision_path = root / "decision.json"
            decision_path.write_text(
                json.dumps(valid_decision()), encoding="utf-8"
            )
            rows = [
                {
                    "date": "2026-01-02",
                    "close": 100.0,
                    "adjusted_close": 100.0,
                    "has_adjusted_close": True,
                    "split_adjustment_factor": 1.0,
                    "high": 101.0,
                    "low": 99.0,
                }
            ]
            with (
                patch("cio.memory.ROOT", root),
                patch("cio.memory.KNOWLEDGE_DIR", knowledge),
                patch("cio.decision.ROOT", root),
                patch("cio.decision.KNOWLEDGE_DIR", knowledge),
            ):
                saved = record_decision_file(decision_path)
                self.assertEqual(record_decision_file(decision_path), saved)
                assessment = json.loads(saved.read_text(encoding="utf-8"))
                assessment["thesis"] = "저장 뒤 변조"
                saved.write_text(json.dumps(assessment), encoding="utf-8")
                with patch("cio.memory._price_rows", return_value=rows):
                    with self.assertRaises(ContractError):
                        score_asset("TSLA")


class TestRoutingContracts(unittest.TestCase):
    def test_registry_rejects_path_like_asset_ids_and_symbols(self):
        profile = {key: value for key, value in get_asset("TSLA").items() if key != "asset_id"}
        with self.assertRaises(AssetRegistryError):
            validate_asset("../../escape", profile)
        profile["price_symbol"] = "../TSLA"
        with self.assertRaises(AssetRegistryError):
            validate_asset("SAFE", profile)

    def test_portfolio_is_optional_for_core_purchase_question(self):
        portfolio = next(
            task for task in tasks_for("TSLA") if task.capability == "PORTFOLIO"
        )
        self.assertFalse(portfolio.required)

    def test_old_calculation_at_another_candidate_price_is_excluded(self):
        asset = get_asset("TSLA")
        inputs = {"price": 315.7}
        outputs = {
            "scenarios": {
                "bear": {"return_pct": -20},
                "base": {"return_pct": 10},
                "bull": {"return_pct": 40},
            }
        }
        data = {
            "schema_version": "1.1.0",
            "calculation_id": calculation_id_for(
                "reverse_dcf", "1.0", inputs, outputs
            ),
            "model": "reverse_dcf",
            "model_version": "1.0",
            "instrument_id": "TSLA",
            "created_at": "2026-07-28T00:00:00+09:00",
            "source_ids": ["FUND-fixture"],
            "inputs": inputs,
            "outputs": outputs,
        }
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            directory = knowledge / "calculations"
            directory.mkdir()
            (directory / f"{data['calculation_id']}.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            with patch("cio.context.KNOWLEDGE_DIR", knowledge):
                calculation, source = _latest_calculation(
                    asset,
                    candidate_price=300.0,
                    fundamentals_id=None,
                )
        self.assertIsNone(calculation)
        self.assertIsNone(source)

    def test_fundamentals_cannot_be_renamed_across_assets(self):
        data = valid_fundamentals(instrument_id="IBM")
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            directory = knowledge / "fundamentals"
            directory.mkdir()
            path = directory / f"TSLA-{data['period_end']}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _latest_fundamentals(get_asset("TSLA"))
        self.assertTrue(summary.get("contract_errors"))

    def test_debt_composition_reaches_the_model(self):
        """총차입만 실으면 리스 포함 여부를 수집기가 대신 정해 버린다.

        리스·전환사채를 총차입에 넣을지는 방법론 판단이라 모델의 몫이다.
        합계만 주면 모델은 다시 셀 수 없고, 그 순간 판단 주체가 조용히 바뀐다.
        """
        data = valid_fundamentals()
        data["debt_by_category"] = {"BORROWING": 5000.0, "LEASE": 2721.0}
        # 내용 지문이라 필드를 더하면 다시 계산해야 한다.
        data["fundamentals_id"] = fundamentals_id_for(data)
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            directory = knowledge / "fundamentals"
            directory.mkdir()
            path = directory / f"TSLA-{data['period_end']}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _latest_fundamentals(get_asset("TSLA"))
        self.assertEqual(summary["debt_by_category"]["LEASE"], 2721.0)
        self.assertIn("LEASE", summary["debt_by_category"]["_role"])

    def test_legacy_fundamentals_without_raw_hash_is_stale(self):
        data = valid_fundamentals()
        task = RefreshTask(
            name="fundamentals:TSLA",
            capability="FUNDAMENTALS",
            artifact="unused",
            max_age_hours=720,
            command=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / f"TSLA-{data['period_end']}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with patch("cio.refresh.ROOT", Path(tmp)):
                error = _artifact_contract_error(task, path)
        self.assertIn("내용 주소형 원문", error)

    def test_each_registered_industry_lens_has_specific_research(self):
        expected = {
            "005930": "cycle_inputs",
            "214450": "growth_quality",
            "TSLA": "capital_returns",
            "IBM": "legacy_transition",
            "RXRX": "pipeline_rnpv",
            "SCHD": "etf_structure",
            "MSTY": "derivative_payoff",
            "0053L0": "theme_purity",
        }
        for asset_id, topic in expected.items():
            with self.subTest(asset_id=asset_id):
                topics = {
                    item["topic"]
                    for item in _research_requirements(get_asset(asset_id))
                }
                self.assertIn(topic, topics)

    def test_unusable_short_term_investments_is_flagged_not_silently_zero(self):
        """0 이 관측인지 결측인지 말하지 않으면 과대계상이 조용해진다."""
        data = valid_fundamentals()
        data["short_term_investments_basis"] = "STALE_EXCLUDED"
        data["short_term_investments_note"] = "단기투자자산: 기준일 2021-03-31"
        data["fundamentals_id"] = fundamentals_id_for(data)
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            (knowledge / "fundamentals").mkdir()
            path = knowledge / "fundamentals" / f"TSLA-{data['period_end']}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _latest_fundamentals(get_asset("TSLA"))
        flag = summary["short_term_investments_basis"]
        self.assertEqual(flag["basis"], "STALE_EXCLUDED")
        self.assertIn("과대계상", flag["_warning"])

    def test_observed_short_term_investments_costs_no_packet_space(self):
        """정상일 때까지 실으면 매 판단마다 쓸데없는 줄을 보낸다."""
        data = valid_fundamentals()
        data["short_term_investments_basis"] = "OBSERVED"
        data["fundamentals_id"] = fundamentals_id_for(data)
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            (knowledge / "fundamentals").mkdir()
            path = knowledge / "fundamentals" / f"TSLA-{data['period_end']}.json"
            path.write_text(json.dumps(data), encoding="utf-8")
            with (
                patch("cio.context.KNOWLEDGE_DIR", knowledge),
                patch("cio.context.ROOT", knowledge),
            ):
                summary, _source = _latest_fundamentals(get_asset("TSLA"))
        self.assertNotIn("short_term_investments_basis", summary)

    def test_us_packet_declares_what_it_does_not_have(self):
        """결측은 렌즈가 아니라 시장을 따라 갈린다.

        미국 자산에는 컨센서스가 통째로 없어 `multiples_at_review_price` 가
        null 이다. 그 사실이 어디에도 안 적히면 모델은 "볼 것이 없다"로 읽고,
        없는 줄도 모른 채 판단을 닫는다. 실제로 그 상태였다 — TSLA packet 은
        validation=PASS 에 problems=[] 였고 컨센서스 얘기가 한 줄도 없었다.
        """
        for asset_id in ("TSLA", "IBM", "RXRX"):
            with self.subTest(asset_id=asset_id):
                items = _research_requirements(get_asset(asset_id))
                topics = {item["topic"] for item in items}
                self.assertIn("forward_multiples", topics)
                gap = next(
                    item for item in items
                    if item["topic"] == "forward_multiples"
                )
                self.assertIn("컨센서스", gap["known_local_gap"])

    def test_korean_assets_do_not_inherit_the_us_gap(self):
        """한국 자산은 컨센서스가 실제로 있다. 없다고 말하면 그게 거짓이다."""
        for asset_id in ("005930", "214450"):
            with self.subTest(asset_id=asset_id):
                topics = {
                    item["topic"]
                    for item in _research_requirements(get_asset(asset_id))
                }
                self.assertNotIn("forward_multiples", topics)
                self.assertNotIn("ownership_and_short_interest", topics)

    def test_etf_holdings_contract_rejects_bad_rows(self):
        task = RefreshTask(
            name="holdings:SCHD",
            capability="ETF_HOLDINGS",
            artifact="unused",
            max_age_hours=168,
            command=None,
        )
        with tempfile.TemporaryDirectory() as tmp:
            valid = Path(tmp) / "valid.csv"
            valid.write_text(
                "etf_id,holding_id,holding_name,weight_pct,as_of\n"
                "SCHD,HD,Home Depot,4.2,2026-07-28\n"
                "SCHD,TXN,Texas Instruments,3.8,2026-07-28\n",
                encoding="utf-8",
            )
            self.assertIsNone(_artifact_contract_error(task, valid))
            invalid = Path(tmp) / "invalid.csv"
            invalid.write_text(
                "etf_id,holding_id,holding_name,weight_pct,as_of\n"
                "MSTY,HD,Home Depot,-1,2026-07-28\n"
                "SCHD,HD,Duplicate,NaN,2026-07-27\n",
                encoding="utf-8",
            )
            self.assertIsNotNone(_artifact_contract_error(task, invalid))

    def test_sec_filing_index_contract_rejects_wrong_or_unsafe_rows(self):
        task = RefreshTask(
            name="filings:TSLA",
            capability="FILINGS",
            artifact="unused",
            max_age_hours=24,
            command=None,
        )
        raw_payload = {
            "cik": "0001318605",
            "tickers": ["TSLA"],
            "filings": {
                "recent": {
                    "form": ["10-Q"],
                    "filingDate": ["2026-07-27"],
                    "reportDate": ["2026-06-30"],
                    "accessionNumber": ["0000000000-26-000001"],
                    "primaryDocument": ["tsla-20260630.htm"],
                    "primaryDocDescription": [""],
                    "items": [""],
                }
            },
        }
        raw_canonical = json.dumps(
            raw_payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        raw_digest = hashlib.sha256(raw_canonical.encode("utf-8")).hexdigest()
        base = {
            "schema_version": "filing-index/1.0.0",
            "asset_id": "TSLA",
            "cik": "0001318605",
            "collected_at": "2026-07-28T00:00:00+00:00",
            "source": "SEC_EDGAR_SUBMISSIONS",
            "forms_filter": sorted(SEC_FILING_FORMS),
            "raw_payload_sha256": raw_digest,
            "raw_locator": f"data/raw/edgar/objects/{raw_digest}.json",
            "filings": [
                {
                    "filing_date": "2026-07-27",
                    "report_date": "2026-06-30",
                    "form": "10-Q",
                    "accession_number": "0000000000-26-000001",
                    "primary_document": "tsla-20260630.htm",
                    "description": None,
                    "items": None,
                    "item_labels": None,
                    "document_url": (
                        "https://www.sec.gov/Archives/edgar/data/"
                        "1318605/000000000026000001/tsla-20260630.htm"
                    ),
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            raw = root / base["raw_locator"]
            raw.parent.mkdir(parents=True)
            raw.write_text(
                json.dumps(
                    {
                        "source": "sec.gov/edgar",
                        "payload_sha256": raw_digest,
                        "payload": raw_payload,
                    }
                ),
                encoding="utf-8",
            )
            path = root / "filings.json"
            path.write_text(json.dumps(base), encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                self.assertIsNone(_artifact_contract_error(task, path))
                invalid = dict(base)
                invalid["asset_id"] = "../../escape"
                invalid["filings"] = [
                    {
                        **base["filings"][0],
                        "document_url": "https://example.invalid/phishing",
                    }
                ]
                path.write_text(json.dumps(invalid), encoding="utf-8")
                self.assertIsNotNone(_artifact_contract_error(task, path))
                path.write_text(json.dumps(base), encoding="utf-8")
                tampered = json.loads(raw.read_text(encoding="utf-8"))
                tampered["payload"]["cik"] = "0000000001"
                raw.write_text(json.dumps(tampered), encoding="utf-8")
                self.assertIsNotNone(_artifact_contract_error(task, path))

    def test_event_packet_is_bound_to_registered_asset(self):
        asset = get_asset("005930")
        packet = {
            "schema_version": "event-packet/2.1.0",
            "asset_id": "005930",
            "symbol": "005930.KS",
            "collected_at": "2026-07-28T00:00:00+09:00",
            "benchmark": {
                "symbol": "^KS11",
                "available": True,
                "calendar_gaps": [],
            },
            "benchmark_comparison": {"benchmark_symbol": "^KS11", "legs": []},
            "judged_on": "벤치마크 대비 beta=1 상대변동",
            "last_bar": {"date": "2026-07-28", "settled": True},
            "moves": [],
        }
        self.assertEqual(event_contract_errors(packet, asset), [])
        packet["asset_id"] = "214450"
        self.assertTrue(event_contract_errors(packet, asset))

    def test_event_packet_rejects_superseded_schema(self):
        asset = get_asset("005930")
        packet = self._valid_event_packet()
        packet["schema_version"] = "event-packet/2.0.0"
        self.assertTrue(event_contract_errors(packet, asset))

    def test_event_packet_must_prove_it_checked_the_benchmark_calendar(self):
        """구멍이 없으면 빈 배열이어야 한다. 필드가 없다는 것은 대조하지
        않았다는 뜻이고, 그러면 상대변동을 믿을 근거가 없다."""
        asset = get_asset("005930")
        packet = self._valid_event_packet()
        del packet["benchmark"]["calendar_gaps"]
        self.assertIn(
            "이벤트 packet에 벤치마크 캘린더 대조 결과가 없다",
            event_contract_errors(packet, asset),
        )

    def test_event_packet_must_be_built_on_a_settled_session(self):
        asset = get_asset("005930")
        packet = self._valid_event_packet()
        packet["last_bar"] = {"date": "2026-07-29", "settled": False}
        self.assertIn(
            "이벤트 packet이 확정 거래일 위에서 계산되지 않았다",
            event_contract_errors(packet, asset),
        )

    @staticmethod
    def _valid_event_packet() -> dict:
        return {
            "schema_version": "event-packet/2.1.0",
            "asset_id": "005930",
            "symbol": "005930.KS",
            "collected_at": "2026-07-28T00:00:00+09:00",
            "benchmark": {
                "symbol": "^KS11",
                "available": True,
                "calendar_gaps": [],
            },
            "benchmark_comparison": {"benchmark_symbol": "^KS11", "legs": []},
            "judged_on": "벤치마크 대비 beta=1 상대변동",
            "last_bar": {"date": "2026-07-28", "settled": True},
            "moves": [],
        }


class TestDerivedPacketFreshness(unittest.TestCase):
    """파생 패킷이 재료보다 낡았는가.

    이 검사가 생긴 이유가 있다. 이 저장소의 정상화 packet 은 재무 스냅샷보다
    **2분 늦게** 만들어졌으면서 데이터는 한 분기 적었다 — 수집 시각만 비교하는
    의존성 검사에는 최신으로 보인다. 그래서 확인할 것은 시각이 아니라
    **덮는 기간**이다.

    낡은 정상화값은 틀린 값이 아니라 오래된 값이라 경고 없이 그대로 읽힌다.
    실측으로 5.02배와 6.19배의 차이였고, 이 배수는 "지금 이익이 정상 대비
    얼마나 부풀었나"라서 판단을 바꾼다.
    """

    def task(self, asset_id="000660"):
        return RefreshTask(
            name=f"normalized:{asset_id}",
            capability="NORMALIZED_EARNINGS",
            artifact="unused",
            max_age_hours=720,
            command=None,
        )

    def packet(self, asset_id, *periods):
        return {
            "instrument_id": asset_id,
            "ttm_series": [{"period_end": p, "fcf_ttm": 1.0} for p in periods],
        }

    def check(self, packet, fundamentals_periods, asset_id="000660"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "knowledge" / "fundamentals").mkdir(parents=True)
            for period in fundamentals_periods:
                (root / "knowledge" / "fundamentals"
                 / f"{asset_id}-{period}.json").write_text("{}", encoding="utf-8")
            path = root / "packet.json"
            path.write_text(json.dumps(packet), encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                return _artifact_contract_error(self.task(asset_id), path)

    def test_a_packet_that_lags_its_source_is_reported(self):
        error = self.check(
            self.packet("000660", "2025-09-30", "2025-12-31"), ["2026-03-31"]
        )
        self.assertIn("재료보다 낡았다", error)
        self.assertIn("2025-12-31", error)
        self.assertIn("2026-03-31", error)

    def test_a_packet_level_with_its_source_passes(self):
        self.assertIsNone(self.check(
            self.packet("000660", "2025-12-31", "2026-03-31"), ["2026-03-31"]
        ))

    def test_a_packet_ahead_of_its_source_is_not_flagged(self):
        """재무 스냅샷을 안 받고 정상화만 돌린 경우다. 낡음은 아니다."""
        self.assertIsNone(self.check(
            self.packet("000660", "2026-06-30"), ["2026-03-31"]
        ))

    def test_no_fundamentals_on_hand_means_nothing_to_compare(self):
        self.assertIsNone(self.check(self.packet("000660", "2025-12-31"), []))

    def test_another_assets_snapshots_do_not_count(self):
        """005930 의 최신 분기가 000660 을 낡았다고 만들면 안 된다."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "knowledge" / "fundamentals").mkdir(parents=True)
            for name in ("005930-2026-03-31.json", "000660-2025-12-31.json"):
                (root / "knowledge" / "fundamentals" / name).write_text(
                    "{}", encoding="utf-8")
            path = root / "packet.json"
            path.write_text(json.dumps(
                self.packet("000660", "2025-12-31")), encoding="utf-8")
            with patch("cio.refresh.ROOT", root):
                self.assertIsNone(_artifact_contract_error(self.task(), path))

    def test_an_empty_series_is_a_defect_not_a_pass(self):
        self.assertIn("TTM 시계열이 없다",
                      self.check({"instrument_id": "000660",
                                  "ttm_series": []}, ["2026-03-31"]))

    def test_a_packet_for_the_wrong_asset_is_refused(self):
        self.assertIn("instrument_id",
                      self.check(self.packet("005930", "2026-03-31"),
                                 ["2026-03-31"]))

    def test_the_series_order_is_not_trusted(self):
        """정렬은 생성기의 약속이고, 여기서 확인하려는 것이 그 생성기다."""
        self.assertIsNone(self.check(
            self.packet("000660", "2026-03-31", "2024-12-31"), ["2026-03-31"]
        ))


class TestMemoryDigest(unittest.TestCase):
    def test_digest_uses_created_time_context_price_and_latest_evaluation(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            assessment_dir = knowledge / "assessments" / "TSLA"
            context_dir = knowledge / "contexts" / "TSLA"
            evaluation_dir = knowledge / "evaluations" / "TSLA"
            assessment_dir.mkdir(parents=True)
            context_dir.mkdir(parents=True)
            evaluation_dir.mkdir(parents=True)
            latest_assessment = None
            latest_context = None
            for index in range(4):
                day = f"2026-0{index + 1}-01"
                context = valid_context()
                context["created_at"] = f"{day}T00:00:00+09:00"
                context["request"]["as_of"] = context["created_at"]
                context["request"]["price"] = 100 + index
                context["context_id"] = context_id_for(context)
                decision = valid_decision()
                decision["context_id"] = context["context_id"]
                decision["created_at"] = f"{day}T01:00:00+09:00"
                decision["win_probability_pct"] = 40 + index
                assessment = normalize_decision(decision)
                (assessment_dir / f"{assessment['assessment_id']}.json").write_text(
                    json.dumps(assessment), encoding="utf-8"
                )
                (context_dir / f"{context['context_id']}.json").write_text(
                    json.dumps(context), encoding="utf-8"
                )
                latest_assessment = assessment
                latest_context = context
            evaluation = {
                "schema_version": "cio-evaluation/1.0.0",
                "evaluation_id": None,
                "assessment_id": latest_assessment["assessment_id"],
                "context_id": latest_context["context_id"],
                "asset_id": "TSLA",
                "model": latest_assessment["model"],
                "verdict": latest_assessment["verdict"],
                "outcome_metric": latest_assessment["outcome_metric"],
                "evaluated_at": "2026-07-28T00:00:00+09:00",
                "window": {
                    "complete": True,
                    "latest_available_date": "2026-07-27",
                },
                "price": {"return_to_end_pct": 12.3},
                "thresholds": {"win_observed": True},
                "calibration": {"brier_score": 0.25},
            }
            evaluation["evaluation_id"] = stable_hash(
                {
                    key: value
                    for key, value in evaluation.items()
                    if key not in {"evaluation_id", "evaluated_at"}
                },
                "EVAL-",
            )
            (evaluation_dir / f"{evaluation['evaluation_id']}.json").write_text(
                json.dumps(evaluation), encoding="utf-8"
            )
            with patch("cio.context.KNOWLEDGE_DIR", knowledge):
                digest = _memory_digest(
                    "TSLA",
                    latest_close=75.0,
                    close_date="2026-07-27",
                    target_annual_return_pct=12,
                    return_currency="USD",
                    objective_currency="USD",
                )
            live = digest["live"]
            self.assertEqual(digest["state"], "CARRIED_FORWARD")
            self.assertEqual(digest["live_count"], 3)
            self.assertEqual([item["asked_price"] for item in live], [101, 102, 103])
            self.assertEqual(live[-1]["latest_evaluation"]["return_to_end_pct"], 12.3)

            carried = live[-1]["carried_forward"]
            self.assertIsNotNone(carried["price_plan"])
            self.assertTrue(carried["invalidation_signals"])
            # action 은 직전 판단이 미리 한 약속이다. 서술은 잘려도 이것은
            # 잘리면 안 된다 — 빠지면 이번 판단은 약속이 있었다는 사실조차
            # 모른 채 새 가격에서 다시 계산한다.
            self.assertEqual(carried["invalidation_signals"][0]["action"], "EXIT")
            # 조사하지 않은 동인이 다음 판단으로 넘어가야 같은 것을 계속
            # 미루고 있다는 사실이 드러난다.
            skipped = [
                item for item in carried["drivers"] if item["researched"] is False
            ]
            self.assertEqual(len(skipped), 1)
            self.assertEqual(skipped[0]["name"], "에너지 저장")
            self.assertTrue(skipped[0]["skipped_reason"])
            # 문턱 대조는 PC 가 산술로 답한다. 75달러는 손절가 80 아래이고
            # 재검토 하단 85 도 지났다.
            check = live[-1]["price_vs_prior_plan"]
            self.assertEqual(
                sorted(check["triggered"]), ["reask_below", "stop_price"]
            )
            self.assertFalse(check["in_entry_range"])
            # 문턱 대조만으로는 수익이 **언제** 왔는지 알 수 없다. 사용자가
            # 정한 연 목표수익률이 packet 까지 실제로 흘러오는지 고정한다.
            pace = live[-1]["pace_since_decision"]
            self.assertEqual(pace["decided_on"], "2026-04-01")
            self.assertEqual(pace["elapsed_days"], 117)
            self.assertEqual(pace["objective_annual_return_pct"], 12)
            self.assertIsNotNone(pace["realized_over_objective_x"])
            # 103 → 75 는 손실이므로 예정보다 뒤처져 있다.
            self.assertLess(pace["pace_progress_over_elapsed_x"], 0)


if __name__ == "__main__":
    unittest.main()
