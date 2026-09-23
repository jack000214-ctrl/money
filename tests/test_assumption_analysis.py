"""Assumptions can change a model's decision, but never make the PC decide."""

from contextlib import ExitStack
from copy import deepcopy
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from calculators.assumptions import AssumptionError, SCHEMA, analyze, calculate, parameter_values
from calculators.contracts import content_hash, write_json
from cio.assumptions import analysis_id, carried_review, comparison, create_analysis, load_analysis, report_path, validate_review
from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision


def spec_fixture():
    inputs = {"method": "EARNINGS_MULTIPLE", "price": 100, "currency": "USD", "horizon_months": 12,
              "win_return_threshold_pct": 10,
              "scenarios": {n: {"eps": eps, "multiple": multiple, "cash_distribution": 0,
                  "probability_pct": prob, "conditional_win_probability_pct": win}
                  for n, eps, multiple, prob, win in [("bear", 10, 5, 25, 5), ("base", 15, 8, 50, 50), ("bull", 20, 10, 25, 95)]}}
    return {"schema_version": SCHEMA, "inputs": inputs,
            "comparison": {"basis": "MODEL", "total_return_pct": 10, "rationale": "모델이 선택한 비교 수익률"},
            "bases": [{"id": "model_inputs", "parameters": list(parameter_values(inputs)), "origin": "MODEL_ESTIMATE",
                       "rationale": "매출·이익 관측에서 만든 가정이다. 배수·확률은 모델 추정이다.",
                       "evidence_refs": ["FUNDAMENTALS:TSLA"], "revisit_when": "다음 실적의 이익과 수익성이 달라지면 다시 추정"}],
            "tests": [{"id": "lower_multiple", "changes": {"scenarios.base.multiple": 6}, "rationale": "성장 프리미엄 축소"},
                      {"id": "risk_shift", "changes": {"scenarios.bear.probability_pct": 30, "scenarios.base.probability_pct": 45}, "rationale": "하방 불확실성 증가"}],
            "boundaries": [{"id": "required_multiple", "parameter": "scenarios.base.multiple", "low": 1, "high": 20}]}


class TestAssumptionMath(unittest.TestCase):
    def test_alternatives_and_boundary(self):
        result = analyze(spec_fixture(), 10)
        self.assertAlmostEqual(result["baseline"]["expected_return_pct"], 22.5)
        self.assertAlmostEqual(result["baseline"]["win_probability_pct"], 50)
        self.assertAlmostEqual(result["tests"][0]["expected_return_pct"], 7.5)
        self.assertEqual(result["tests"][0]["comparison_relation"], "BELOW")
        self.assertAlmostEqual(result["tests"][1]["expected_return_pct"], 19)
        self.assertAlmostEqual(result["boundaries"][0]["required_value"], 19 / 3)
        self.assertAlmostEqual(result["boundaries"][0]["return_at_boundary_pct"], 10)
        self.assertNotIn('"verdict"', json.dumps(result))
        self.assertNotIn('"action"', json.dumps(result))

    def test_annual_and_cumulative_returns_are_distinct(self):
        spec = spec_fixture(); spec["inputs"]["horizon_months"] = 24
        output = analyze(spec, 21)["baseline"]["scenarios"]["base"]
        self.assertAlmostEqual(output["total_return_pct"], 20)
        self.assertAlmostEqual(output["annualized_return_pct"], (1.2 ** .5 - 1) * 100)
        self.assertAlmostEqual(comparison({**spec, "comparison": {"basis": "POLICY_TARGET"}},
              {"context": {"investor_policy": {"objective": {"target_annual_return_pct": 10}}}})["total_return_pct"], 21)

    def test_missing_policy_has_no_invented_default(self):
        spec = spec_fixture(); spec["comparison"] = {"basis": "POLICY_TARGET"}
        with self.assertRaises(AssumptionError): comparison(spec, {"context": {}})

    def test_outside_search_range_has_no_fabricated_boundary(self):
        spec = spec_fixture(); spec["boundaries"][0]["high"] = 2
        boundary = analyze(spec, 10)["boundaries"][0]
        self.assertEqual(boundary["status"], "NOT_BRACKETED")
        self.assertIsNone(boundary["required_value"])

    def test_dcf_reuses_existing_formula_and_handles_decreasing_parameter(self):
        spec = spec_fixture()
        spec["inputs"]["method"] = "DCF"
        spec["inputs"]["dcf"] = {"fcf": 10, "shares": 1, "net_debt": 0, "discount_rate_pct": 10, "terminal_growth_pct": 2, "years": 1}
        for row in spec["inputs"]["scenarios"].values():
            for field in ("eps", "multiple", "cash_distribution"): del row[field]
            row["growth_pct"] = 0
        spec["bases"][0]["parameters"] = list(parameter_values(spec["inputs"]))
        spec["tests"] = [{"id": "higher_discount", "changes": {"dcf.discount_rate_pct": 12}, "rationale": "요구 수익률 변화"}]
        spec["boundaries"] = [{"id": "required_discount", "parameter": "dcf.discount_rate_pct", "low": 5, "high": 20}]
        result = analyze(spec, 10)
        self.assertAlmostEqual(result["baseline"]["scenarios"]["base"]["value_per_share"], 125)
        self.assertLess(result["tests"][0]["expected_return_pct"], 25)
        self.assertAlmostEqual(result["boundaries"][0]["return_at_boundary_pct"], 10)

    def test_invalid_calculations_are_rejected(self):
        mutations = [
            lambda s: s["inputs"].update(price=float("nan")),
            lambda s: s["inputs"]["scenarios"]["base"].update(eps=True),
            lambda s: s["inputs"]["scenarios"]["base"].update(probability_pct=40),
            lambda s: s["tests"][0].update(changes={"__import__('os')": 1}),
            lambda s: s["tests"][0].update(changes={"scenarios.base.multiple": 8}),
            lambda s: s["bases"][0]["parameters"].pop(),
            lambda s: s["bases"][0]["parameters"].append("scenarios.base.eps"),
            lambda s: s["bases"][0].update(evidence_refs=[]),
            lambda s: s["tests"].append(deepcopy(s["tests"][0])),
            lambda s: s["boundaries"][0].update(parameter="scenarios.base.probability_pct"),
            lambda s: s["inputs"]["scenarios"]["base"].update(conditional_win_probability_pct=101),
        ]
        for mutate in mutations:
            spec = spec_fixture(); mutate(spec)
            with self.subTest(mutate=mutate), self.assertRaises(AssumptionError): analyze(spec, 10)


class TestAssumptionPersistence(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.knowledge = self.root / "knowledge"
        for module in ("cio.assumptions", "cio.context", "cio.decision", "cio.memory"):
            self.stack.enter_context(patch(f"{module}.ROOT", self.root))
            self.stack.enter_context(patch(f"{module}.KNOWLEDGE_DIR", self.knowledge))
        self.stack.enter_context(patch("cio.exit_plan.ROOT", self.root))
        source = self.root / "source.json"; source.write_text('{"eps": [10, 15, 20]}')
        self.context = valid_context()
        self.context["context"] = {"portfolio": {"existing_position": {"held": True, "quantity": 10}}}
        self.context["model_contract"]["assumption_review_required"] = True
        self.context["evidence_index"] = [
            {"source_id": "PRICE:TSLA"},
            {"source_id": "FUNDAMENTALS:TSLA", "path": "source.json", "kind": "FILING_DERIVED", "content_sha256": content_hash(source)}]
        self.context["context_id"] = context_id_for(self.context)
        self.spec = spec_fixture()

    def decision(self, report):
        d = valid_decision(); d["context_id"] = self.context["context_id"]
        d["created_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        d["horizon_months"] = 12; d["method"]["valuation_family"] = "EARNINGS_MULTIPLE"
        d["expected_return_pct"] = {n: report["outputs"]["baseline"]["scenarios"][n]["total_return_pct"] for n in ("bear", "base", "bull")}
        d["expected_return_pct"]["probability_weighted"] = report["outputs"]["baseline"]["expected_return_pct"]
        d["win_probability_pct"] = report["outputs"]["baseline"]["win_probability_pct"]
        d["assumption_review"] = {"status": "EVALUATED", "analysis_id": report["analysis_id"], "interpretation": "대체 가정에서 수익 여지가 줄지만 모델은 사업 근거를 함께 고려한다.",
            "responses": [{"id": row["id"], "effect": "KEEP", "verdict": "WAIT", "action": "HOLD", "rationale": "가정의 현실성과 다른 사업 근거를 함께 판단해 유지한다."} for row in [*report["outputs"]["tests"], *report["outputs"]["boundaries"]]]}
        return d

    def test_full_decision_validation_and_recording(self):
        from cio.memory import record_decision_file
        report = create_analysis(self.spec, self.context)
        d = self.decision(report)
        self.assertEqual(validate_decision(d, context=self.context), [])
        write_json(self.knowledge / "contexts" / "TSLA" / f"{self.context['context_id']}.json", self.context)
        draft = write_json(self.root / "decision.json", d)
        path = record_decision_file(draft)
        self.assertTrue(path.is_file())
        summary = carried_review(d)
        self.assertEqual(summary["analysis_id"], report["analysis_id"])
        self.assertAlmostEqual(summary["boundaries"][0]["required_value"], 19 / 3)
        # A return below the comparison does NOT force the model to sell.
        self.assertEqual(d["assumption_review"]["responses"][0]["action"], "HOLD")
        from cio.context import _memory_digest
        with patch("cio.context.retired_summary", return_value={}):
            memory = _memory_digest("TSLA")
        self.assertEqual(memory["live"][-1]["carried_forward"]["assumption_review"]["analysis_id"], report["analysis_id"])
        from cio.report import format_assessment
        rendered = format_assessment(d, status="LIVE", warnings=[])
        self.assertIn("22.50%", rendered)
        self.assertIn("lower_multiple", rendered)

    def test_advisory_plan_survives_record_show_next_packet_and_evaluation(self):
        from cio.memory import record_decision_file, evaluate_assessment
        from cio.context import _memory_digest
        from cio.report import format_assessment
        from test_entry_plan_contract import plan_fixture
        report = create_analysis(self.spec, self.context)
        d = self.decision(report)
        d['entry_plan'] = plan_fixture()
        write_json(self.knowledge / 'contexts' / 'TSLA' / f"{self.context['context_id']}.json", self.context)
        path = record_decision_file(write_json(self.root / 'plan.json', d))
        saved = json.loads(path.read_text())
        self.assertEqual(saved['entry_plan'], d['entry_plan'])
        self.assertIn('first: 예산 600', format_assessment(saved,status='LIVE',warnings=[]))
        memory = _memory_digest('TSLA')
        self.assertEqual(memory['live'][-1]['carried_forward']['entry_plan'], d['entry_plan'])
        # An intervening review without a new plan must not erase the older plan.
        followup = deepcopy(d);followup.pop('entry_plan');followup['created_at']=(dt.datetime.fromisoformat(d['created_at'])+dt.timedelta(seconds=1)).isoformat()
        record_decision_file(write_json(self.root/'followup.json',followup))
        memory = _memory_digest('TSLA')
        self.assertEqual(memory['latest_entry_plan']['assessment_id'],saved['assessment_id'])
        from cio.entry_plan import carried_plans
        with patch('cio.entry_plan.ROOT',self.root):
            remembered=carried_plans({'request':{'asset_id':'TSLA'},'context':{'prior_assessments':memory}})
        self.assertEqual(remembered[(saved['assessment_id'],'first')]['condition'],d['entry_plan']['stages'][0]['condition'])
        bars=[{'date':'2026-09-09','close':110,'high':115,'low':80,'adjusted_close':110,'has_adjusted_close':True,'split_adjustment_factor':1}]
        # No confirmation was recorded: touching the price is not a strategy fill.
        with patch('cio.memory.find_context',return_value=(self.context,None)), patch('cio.memory._price_session',return_value=None), patch('cio.memory._fixed_horizon_returns',return_value={}):
            evaluated = evaluate_assessment(saved,rows=bars)
        self.assertEqual(evaluated['entry_plan']['actual_execution'],'UNKNOWN')
        self.assertIsNone(evaluated['entry_plan']['stages'][0]['hypothetical_return_pct'])
        self.assertIn('counterfactual',evaluated)
        self.assertIn('fixed_horizons',evaluated)

    def test_sell_action_and_future_narrative_survive_next_packet(self):
        from cio.memory import record_decision_file
        from cio.context import _memory_digest
        report = create_analysis(self.spec, self.context)
        decision = self.decision(report)
        decision['action'].update(direction='TRIM', quantity=4, limit_price=110,
                                  sell_reason='VALUATION',
                                  rationale='4주 매도 체결 후 새 공시가 없으면 115달러에 3주, 120달러에 3주를 줄인다.')
        for response in decision['assumption_review']['responses']:
            response['action'] = 'TRIM'
        write_json(self.knowledge / 'contexts' / 'TSLA' / f"{self.context['context_id']}.json", self.context)
        record_decision_file(write_json(self.root / 'sell.json', decision))
        carried = _memory_digest('TSLA')['live'][-1]['carried_forward']
        self.assertEqual(carried['action'], decision['action'])
        self.assertEqual(carried['action_execution']['status'], 'UNKNOWN')
        self.assertEqual(carried['action_execution']['staged_sell_plan'], 'NARRATIVE_ONLY')

    def test_structured_future_sell_plan_survives_record_and_later_reference(self):
        from cio.memory import record_decision_file
        from cio.context import _memory_digest
        from cio.exit_plan import carried_plans
        report = create_analysis(self.spec, self.context)
        decision = self.decision(report)
        decision['action'].update(direction='TRIM', quantity=4, limit_price=110,
                                  sell_reason='VALUATION', rationale='현재 4주 축소 후 새 근거에 따라 나머지를 검토한다.')
        for response in decision['assumption_review']['responses']:
            response['action'] = 'TRIM'
        start = dt.datetime.fromisoformat(decision['created_at']).date()
        decision['exit_plan'] = {
            'currency': 'USD', 'reference_date': self.context['request']['as_of'][:10],
            'reference_basis': '질문 시점 보통주 1주 기준',
            'stages': [
                {'stage_id': 'next_sale', 'name': '다음 축소', 'quantity': 3, 'limit_price': 115,
                 'requires_review': True, 'depends_on': 'CURRENT_ACTION',
                 'condition': '다음 실적 원문을 확인한다', 'cancel_if': '논지가 깨지면 취소',
                 'valid_from': start.isoformat(), 'valid_until': (start + dt.timedelta(days=90)).isoformat()},
                {'stage_id': 'last_sale', 'name': '마지막 축소', 'quantity': 3, 'limit_price': 120,
                 'requires_review': True, 'depends_on': 'next_sale',
                 'condition': '추가 상승과 사업 근거를 다시 확인한다', 'cancel_if': '가격 계획을 바꾸면 취소',
                 'valid_from': start.isoformat(), 'valid_until': (start + dt.timedelta(days=90)).isoformat()},
            ],
        }
        write_json(self.knowledge / 'contexts' / 'TSLA' / f"{self.context['context_id']}.json", self.context)
        saved_path = record_decision_file(write_json(self.root / 'exit.json', decision))
        saved = json.loads(saved_path.read_text())
        self.assertEqual(saved['exit_plan'], decision['exit_plan'])
        carried = _memory_digest('TSLA')['live'][-1]['carried_forward']
        self.assertEqual(carried['exit_plan'], decision['exit_plan'])
        self.assertEqual(carried['action_execution']['staged_sell_plan'], 'STRUCTURED')
        self.assertEqual(carried['action_execution']['status'], 'UNKNOWN')

        followup = deepcopy(decision)
        followup.pop('exit_plan')
        followup['created_at'] = (dt.datetime.fromisoformat(decision['created_at']) + dt.timedelta(seconds=1)).isoformat()
        record_decision_file(write_json(self.root / 'exit-followup.json', followup))
        memory = _memory_digest('TSLA')
        self.assertEqual(memory['latest_exit_plan']['assessment_id'], saved['assessment_id'])
        self.assertEqual(memory['latest_exit_plan']['execution'], 'UNKNOWN')
        self.assertEqual(memory['latest_exit_plan']['status'], 'HISTORICAL_REFERENCE')
        with patch('cio.exit_plan.ROOT', self.root):
            recovered = carried_plans({'request': {'asset_id': 'TSLA'}, 'context': {'prior_assessments': memory}})
        self.assertEqual(recovered[(saved['assessment_id'], 'next_sale')]['quantity'], 3)

    def test_large_future_sell_plan_becomes_a_reference_before_signals_are_dropped(self):
        from cio.context import _fit_context_budget, _compact_size
        plan = {'stages': [{'stage_id': 'later', 'condition': 'x' * 1800, 'cancel_if': 'y' * 1800}]}
        identifier = 'CIO-' + 'a' * 20
        path = self.knowledge / 'assessments' / 'TSLA' / f'{identifier}.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({'exit_plan': plan}), encoding='utf-8')
        carried = {'exit_plan': plan, 'invalidation_signals': [{'signal': 'risk', 'action': 'EXIT'}]}
        packet = {'request': {'asset_id': 'TSLA'}, 'validation': {'optional_gaps': []},
                  'context': {'prior_assessments': {'live': [{'assessment_id': identifier,
                                                              'carried_forward': carried}]}}}
        with patch('cio.context.MAX_CONTEXT_JSON_BYTES', 2000):
            _fit_context_budget(packet)
        self.assertNotIn('exit_plan', carried)
        self.assertEqual(carried['exit_plan_ref']['content_sha256'], content_hash(path))
        self.assertEqual(carried['invalidation_signals'][0]['action'], 'EXIT')
        self.assertLess(_compact_size(packet), 2000)

    def test_cancelled_exit_stage_stays_cancelled_after_an_intervening_judgment(self):
        from cio.context import _assessment_market_date, _memory_digest
        from cio.exit_plan import validate as validate_exit_plan
        from cio.memory import record_decision_file

        def next_context(prior):
            context = deepcopy(self.context)
            context['context']['prior_assessments'] = prior
            context['context_id'] = context_id_for(context)
            write_json(self.knowledge / 'contexts' / 'TSLA' / f"{context['context_id']}.json", context)
            return context

        def next_decision(context, prior_created_at):
            analysis = create_analysis(self.spec, context)
            decision = self.decision(analysis)
            decision['context_id'] = context['context_id']
            after_analysis = dt.datetime.fromisoformat(analysis['created_at']) + dt.timedelta(seconds=1)
            after_prior = dt.datetime.fromisoformat(prior_created_at) + dt.timedelta(seconds=1)
            decision['created_at'] = max(after_analysis, after_prior).isoformat()
            live = ((context['context'].get('prior_assessments') or {}).get('live') or [])
            carried = (live[-1].get('carried_forward') or {}) if live else {}
            decision['prior_signal_checks'] = [
                {'signal_index': signal['index'], 'signal': signal['signal'],
                 'state': 'UNVERIFIABLE', 'committed_action': signal['action'],
                 'taken_action': None, 'override_reason': None,
                 'note': '이 테스트 판단에서는 해당 신호의 발생 여부를 확인하지 않았다'}
                for signal in (carried.get('invalidation_signals') or [])
            ]
            return decision

        write_json(self.knowledge / 'contexts' / 'TSLA' / f"{self.context['context_id']}.json", self.context)
        first = next_decision(self.context, self.context['created_at'])
        first['action'].update(direction='TRIM', quantity=4, limit_price=110,
                               sell_reason='VALUATION', rationale='4주를 먼저 줄인다')
        for response in first['assumption_review']['responses']:
            response['action'] = 'TRIM'
        first['exit_plan'] = {
            'currency': 'USD', 'reference_date': self.context['request']['as_of'][:10],
            'reference_basis': '질문 시점 보통주 1주',
            'stages': [{'stage_id': 'later', 'name': '추가 축소', 'quantity': 3, 'limit_price': 115,
                        'requires_review': True, 'depends_on': 'CURRENT_ACTION',
                        'condition': '실적 원문을 확인한다', 'cancel_if': '논지가 바뀌면 취소',
                        'valid_from': first['created_at'][:10],
                        'valid_until': (dt.date.fromisoformat(first['created_at'][:10]) + dt.timedelta(days=90)).isoformat()}],
        }
        plan = json.loads(record_decision_file(write_json(self.root / 'sell-plan.json', first)).read_text())

        second_context = next_context(_memory_digest('TSLA'))
        second = next_decision(second_context, first['created_at'])
        second['exit_plan_checks'] = [{
            'assessment_id': plan['assessment_id'], 'stage_id': 'later',
            'condition': first['exit_plan']['stages'][0]['condition'], 'state': 'CANCELLED',
            'note': '새 근거로 이전 가격 계획을 취소했다', 'evidence_refs': ['PRICE:TSLA'],
        }]
        cancelled = json.loads(record_decision_file(write_json(self.root / 'sell-cancel.json', second)).read_text())

        third_context = next_context(_memory_digest('TSLA'))
        third = next_decision(third_context, second['created_at'])
        record_decision_file(write_json(self.root / 'sell-later.json', third))
        memory = _memory_digest('TSLA')
        reference = memory['latest_exit_plan']
        self.assertEqual(reference['status'], 'HISTORICAL_REFERENCE')
        self.assertEqual(reference['execution'], 'UNKNOWN')
        review = reference['stage_reviews'][0]
        self.assertEqual((review['stage_id'], review['state']), ('later', 'CANCELLED'))
        self.assertEqual(review['review_assessment_id'], cancelled['assessment_id'])
        self.assertEqual(review['review_date'], _assessment_market_date(cancelled['created_at'], 'US'))
        self.assertEqual(review['reviewed_at'], cancelled['created_at'])

        fourth_context = next_context(memory)
        attempted_revival = next_decision(fourth_context, third['created_at'])
        attempted_revival['exit_plan_checks'] = [{
            'assessment_id': plan['assessment_id'], 'stage_id': 'later',
            'condition': first['exit_plan']['stages'][0]['condition'], 'state': 'SATISFIED',
            'note': '옛 계획을 다시 쓰려 한다', 'evidence_refs': ['PRICE:TSLA'],
        }]
        self.assertTrue(validate_exit_plan(attempted_revival, fourth_context))

    def test_exit_review_date_uses_market_timezone(self):
        from cio.context import _assessment_market_date
        timestamp = '2026-09-24T00:30:00+09:00'
        self.assertEqual(_assessment_market_date(timestamp, 'US'), '2026-09-23')
        self.assertEqual(_assessment_market_date(timestamp, 'KR'), '2026-09-24')

    def test_cli_calculate_then_load(self):
        from cio.__main__ import build_parser
        from contextlib import redirect_stdout
        import io
        write_json(self.knowledge / "contexts" / "TSLA" / f"{self.context['context_id']}.json", self.context)
        path = write_json(self.root / "spec.json", self.spec)
        out = io.StringIO()
        args = build_parser().parse_args(["assumptions", str(path), "--context", self.context["context_id"], "--json"])
        with redirect_stdout(out): self.assertEqual(args.func(args), 0)
        report = json.loads(out.getvalue())
        out = io.StringIO()
        args = build_parser().parse_args(["assumptions", report["analysis_id"], "--asset", "TSLA"])
        with redirect_stdout(out): self.assertEqual(args.func(args), 0)
        self.assertIn("MODEL_ESTIMATE", out.getvalue())
        self.assertIn("7.50%", out.getvalue())

    def test_different_context_and_currency_are_rejected(self):
        bad = deepcopy(self.spec); bad["inputs"]["currency"] = "KRW"
        with self.assertRaises(AssumptionError): create_analysis(bad, self.context)
        report = create_analysis(self.spec, self.context)
        other = deepcopy(self.context); other["request"]["price"] = 101
        other["context_id"] = context_id_for(other)
        with self.assertRaises(AssumptionError): load_analysis("TSLA", report["analysis_id"], other)

    def test_bare_price_data_does_not_support_earnings_estimates(self):
        ctx = deepcopy(self.context)
        ctx["evidence_index"] = [{**ctx["evidence_index"][1], "source_id": "PRICE:TSLA", "kind": "MARKET_DATA"}]
        ctx["context_id"] = context_id_for(ctx)
        spec = deepcopy(self.spec); spec["bases"][0]["evidence_refs"] = ["PRICE:TSLA"]
        with self.assertRaisesRegex(AssumptionError, "재무 가정"):
            create_analysis(spec, ctx)

    def test_recomputation_catches_tampering_even_with_a_new_hash(self):
        report = create_analysis(self.spec, self.context)
        report["outputs"]["baseline"]["expected_return_pct"] += 1
        report["analysis_id"] = analysis_id(report)
        write_json(report_path("TSLA", report["analysis_id"]), report)
        with self.assertRaisesRegex(AssumptionError, "재계산"):
            load_analysis("TSLA", report["analysis_id"], self.context)

    def test_missing_or_modified_evidence_is_rejected(self):
        report = create_analysis(self.spec, self.context)
        source = self.root / report["evidence_index"][0]["path"]
        source.write_text('{}')
        with self.assertRaises(AssumptionError): load_analysis("TSLA", report["analysis_id"], self.context)

    def test_report_cannot_redefine_a_context_source(self):
        report = create_analysis(self.spec, self.context)
        fake = self.root / "wrong.json"; fake.write_text('{}')
        report["evidence_index"][0].update(path="wrong.json", content_sha256=content_hash(fake))
        report["analysis_id"] = analysis_id(report)
        write_json(report_path("TSLA", report["analysis_id"]), report)
        with self.assertRaisesRegex(AssumptionError, "재정의"):
            load_analysis("TSLA", report["analysis_id"], self.context)

    def test_decision_cannot_skip_or_rewrite_its_experiment(self):
        report = create_analysis(self.spec, self.context)
        mutations = [lambda d: d.pop("assumption_review"),
                     lambda d: d["assumption_review"]["responses"].pop(),
                     lambda d: d["assumption_review"]["responses"][0].update(id="invented"),
                     lambda d: d.update(win_probability_pct=55),
                     lambda d: d["expected_return_pct"].update(base=99),
                     lambda d: d.update(horizon_months=24),
                     lambda d: d["assumption_review"]["responses"][0].update(effect="CHANGE"),
                     lambda d: d.update(created_at="2026-07-28T10:00:00+09:00")]
        for mutate in mutations:
            d = self.decision(report); mutate(d)
            with self.subTest(mutate=mutate): self.assertTrue(validate_review(d, self.context))

    def test_legacy_decisions_are_not_retroactively_blocked(self):
        ctx = valid_context()
        self.assertEqual(validate_review(valid_decision(), ctx), [])

    def test_unsupported_method_must_be_visible_and_cannot_bypass_supported_one(self):
        d = valid_decision()
        d["assumption_review"] = {"status": "UNSUPPORTED_METHOD", "analysis_id": None, "interpretation": "옵션 손익 구조에 맞는 계산이 필요하다", "responses": []}
        self.assertTrue(validate_review(d, self.context))  # DCF is supported.
        d["method"]["valuation_family"] = "OPTION_PAYOFF"
        self.assertEqual(validate_review(d, self.context), [])


if __name__ == "__main__": unittest.main()
