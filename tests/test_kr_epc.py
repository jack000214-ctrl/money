"""EPC facts retain unit, scope and stock/flow identity across report formats."""

from copy import deepcopy
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import helpers  # noqa: F401
from calculators.contracts import canonical_payload_hash, write_json
from calculators.epc import summary
from collectors.kr_epc import collect, contract_errors, extract


def cell(value, *, year=2026, scope="Consolidated", decimal="0"):
    return f'<TE ACONTEXT="CFY{year}dHYA_ifrs-full_{scope}Member" ADECIMAL="{decimal}">{value}</TE>'


def table(label, value):
    return '<TABLE><THEAD><TR><TH></TH><TH>도급공사</TH></TR></THEAD><TBODY><TR><TE>' + label + '</TE>' + cell(value) + '</TR></TBODY></TABLE>'


def document(end="06-30", unit="천원", *, effect=30):
    return (f'<P>사업연도 2026년 01월 01일 부터 2026년 {int(end[:2])}월 {int(end[3:])}일 까지</P>'
            '<TITLE>18. 도급공사계약 (연결)</TITLE>' + (f'<P>(단위 : {unit})</P>' if unit else '') +
            table('총계약수익과 원가의 추정 변동이 당기손익에 미치는 영향', effect) +
            table('총계약수익과 원가의 추정 변동이 미래손익에 미치는 영향', 90) +
            table('단기미청구공사', 200) + table('단기초과청구공사', '(400)') +
            table('유동공사손실충당부채', 12) + table('공사손실충당부채전입액', '(12)') +
            table('선수금', 7) + table('나머지 수행의무에 배분된 거래가격', 1000) +
            '<TITLE>17. 도급공사계약</TITLE>' + table('단기미청구공사', 999999))


class TestEpcExtraction(unittest.TestCase):
    def test_te_cells_and_display_units_override_precision_metadata(self):
        result = extract(document(), '2026-06-30')
        self.assertEqual(result['values']['unbilled_balance']['amount_krw'], 200000)
        self.assertEqual(result['values']['customer_advances_balance']['amount_krw'], 7000)
        self.assertEqual(result['values']['overbilled_balance']['amount_krw'], 400000)
        self.assertEqual(result['values']['overbilled_balance']['reported_amount_krw'], -400000)
        self.assertEqual(result['values']['estimate_change_profit_ytd']['period_start'], '2026-01-01')
        self.assertEqual(result['values']['estimate_change_profit_future']['time_kind'], 'FUTURE_REMAINING')
        self.assertIsNone(result['values']['estimate_change_profit_future']['period_end'])
        self.assertEqual(result['values']['loss_provision_reported_charge']['state'], 'STOCK_FLOW_UNRESOLVED')
        self.assertIsNone(result['values']['loss_provision_reported_charge']['amount_krw'])

    def test_missing_units_and_period_are_not_guessed(self):
        result = extract(document(unit=None), '2026-06-30')  # pyright: ignore[reportArgumentType]
        self.assertIsNone(result['values']['unbilled_balance']['amount_krw'])
        source = document().split('</P>', 1)[1]
        result = extract(source, '2026-06-30')
        self.assertEqual(result['values']['estimate_change_profit_ytd']['state'], 'PERIOD_START_UNRESOLVED')
        self.assertEqual(result['values']['unbilled_balance']['amount_krw'], 200000)

    def test_wrong_financial_scope_and_wrong_report_date_are_rejected(self):
        result = extract(document().replace('_ConsolidatedMember', '_SeparateMember'), '2026-06-30')
        self.assertFalse(result['available'])
        with self.assertRaises(ValueError): extract(document(), '2026-03-31')

    def test_multirow_total_column_is_not_assumed_to_be_last(self):
        multi = ('<TABLE><THEAD><TR><TH ROWSPAN="2"></TH><TH>부문</TH><TH ROWSPAN="2">부문 합계</TH><TH>부문</TH></TR>'
                 '<TR><TH>화공</TH><TH>뉴에너지</TH></TR></THEAD><TBODY><TR><TE>단기미청구공사</TE>' +
                 cell(10) + cell(30) + cell(20) + '</TR></TBODY></TABLE>')
        source = '<TITLE>1. 도급공사계약 (연결)</TITLE><P>(단위 : 백만원)</P>' + multi
        self.assertEqual(extract(source, '2026-06-30')['values']['unbilled_balance']['amount_krw'], 30000000)

    def test_disagreeing_tables_are_not_silently_resolved(self):
        source = document().replace('<TITLE>17.', table('단기미청구공사', 201) + '<TITLE>17.')
        self.assertEqual(extract(source, '2026-06-30')['values']['unbilled_balance']['state'], 'CONFLICTING_VALUES')

    def test_liability_deduction_and_positive_balance_are_same_magnitude(self):
        source = document().replace('<TITLE>17.', table('유동공사손실충당부채', '(12)') + '<TITLE>17.')
        self.assertEqual(extract(source, '2026-06-30')['values']['loss_provision_balance']['amount_krw'], 12000)

    def test_existing_segment_parser_supports_dart_te_cells(self):
        from collectors.kr_segments import _cells
        self.assertEqual(_cells('<TR><TE>영업이익</TE><TE>100</TE><TE>10%</TE></TR>'), ['영업이익', '100', '10%'])


class TestEpcMath(unittest.TestCase):
    def data(self):
        return {'reports': [{'report_date': date, 'source_id': 'DARTDOC:' + date, **extract(document(date[5:], effect=effect), date)}
                            for date, effect in [('2026-06-30', 30), ('2026-03-31', 10)]]}

    def fundamentals(self):
        return {'period_end': '2026-06-30', 'income_statement_ttm': {'revenue': {'ttm': 1000000}},
                'earnings_bridge': {'rows': [{'end': '2026-03-31', 'operating_income': 50000}, {'end': '2026-06-30', 'operating_income': 50000}]}}

    def test_same_period_arithmetic_and_adjacent_ytd_difference(self):
        result = summary(self.data(), self.fundamentals())['arithmetic']
        self.assertEqual(result['estimate_change_profit_quarter_krw'], 20000)
        self.assertEqual(result['estimate_change_effect_pct_of_ytd_operating_income'], 30)
        self.assertEqual(result['unbilled_pct_of_ttm_revenue'], 20)
        self.assertNotIn('verdict', result)
        self.assertNotIn('action', result)

    def test_missing_quarter_or_mismatched_fundamentals_do_not_produce_ratios(self):
        data = self.data(); data['reports'].pop()
        f = self.fundamentals(); f['period_end'] = '2026-03-31'
        result = summary(data, f)['arithmetic']
        self.assertIsNone(result['estimate_change_profit_quarter_krw'])
        self.assertIsNone(result['estimate_change_effect_pct_of_ytd_operating_income'])
        self.assertIsNone(result['unbilled_pct_of_ttm_revenue'])
        f = self.fundamentals(); f['earnings_bridge']['rows'].pop(0)
        self.assertIsNone(summary(data, f)['arithmetic']['estimate_change_effect_pct_of_ytd_operating_income'])

    def test_noncalendar_fiscal_start_does_not_get_calendar_ytd_math(self):
        data = self.data(); data['reports'][0]['report_period_start'] = '2026-04-01'
        result = summary(data, self.fundamentals())['arithmetic']
        self.assertIsNone(result['estimate_change_profit_quarter_krw'])
        self.assertIsNone(result['estimate_change_effect_pct_of_ytd_operating_income'])


class TestEpcContract(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.knowledge = self.root / 'knowledge'
        for module in ['collectors.kr_epc', 'cio.context']:
            self.stack.enter_context(patch(module + '.ROOT', self.root))
            self.stack.enter_context(patch(module + '.KNOWLEDGE_DIR', self.knowledge))
        self.receipt = '20260814000001'
        payload = {'rcept_no': self.receipt, 'documents': {'main.xml': document()}}
        self.source = write_json(self.root / 'raw.json', {'payload_sha256': canonical_payload_hash(payload), 'payload': payload})
        write_json(self.knowledge / 'filings/028050.json', {'filings': [{'form': 'HALF_REPORT', 'report_date': '2026-06-30', 'filing_date': '2026-08-14', 'accession_number': self.receipt}]})
        mock_asset = {
            "asset_id": "028050",
            "name": "삼성엔지니어링",
            "market": "KR",
            "exchange": "KOSPI",
            "asset_type": "STOCK",
            "currency": "KRW",
            "price_symbol": "028050.KS",
            "benchmarks": ["^KS11"],
            "filing_source": "DART",
            "optional_collectors": ["KR_EPC"],
        }
        self.stack.enter_context(patch("cio.assets.get_asset", return_value=mock_asset))
        self.stack.enter_context(patch("cio.refresh.get_asset", return_value=mock_asset))
        self.stack.enter_context(patch("collectors.kr_epc.get_asset", return_value=mock_asset))
        self.stack.enter_context(patch('collectors.kr_epc._fetched_document', return_value=(document(), self.source)))

    def test_reextract_contract_detects_tampering(self):
        data = collect('028050')
        self.assertEqual(contract_errors(data, '028050'), [])
        bad = deepcopy(data); bad['reports'][0]['values']['unbilled_balance']['amount_krw'] = 1
        self.assertTrue(contract_errors(bad, '028050'))
        bad = deepcopy(data); bad['reports'][0]['raw_locator'] = '/not-in-the-repo.json'
        self.assertTrue(contract_errors(bad, '028050'))
        self.assertTrue(contract_errors(data, '000660'))

    def test_context_gets_valid_facts_and_suppresses_invalid_values(self):
        from cio.context import _optional_collection_summaries
        from cio.assets import get_asset
        data = collect('028050'); path = write_json(self.knowledge / 'epc/028050.json', data)
        context, sources = _optional_collection_summaries(get_asset('028050'), fundamentals=TestEpcMath().fundamentals())
        self.assertEqual(context['epc_contract_notes']['latest']['unbilled_balance']['amount_krw'], 200000)
        self.assertTrue(any(r['source_id'] == 'KR_EPC:028050' for r in sources))
        data['reports'][0]['values']['unbilled_balance']['amount_krw'] = 1; write_json(path, data)
        context, sources = _optional_collection_summaries(get_asset('028050'))
        self.assertEqual(context['epc_contract_notes']['state'], 'INVALID')
        self.assertFalse(any(r['source_id'] == 'KR_EPC:028050' for r in sources))

    def test_refresh_has_an_optional_task_with_filing_dependency(self):
        from cio.refresh import tasks_for
        tasks = tasks_for('028050', include_optional=True)
        task = next(t for t in tasks if t.capability == 'KR_EPC')
        self.assertFalse(task.required)
        self.assertIn('knowledge/filings/028050.json', task.dependencies)


if __name__ == '__main__': unittest.main()
