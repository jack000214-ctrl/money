from copy import deepcopy
import unittest

from calculators.assumptions import AssumptionError
from cio.assumptions import validate_earnings_basis
from test_assumption_analysis import spec_fixture


class EarningsShareBasis(unittest.TestCase):
    def fixture(self):
        s=spec_fixture()
        s['bases'][0]['evidence_refs'].append('CAPITAL:TSLA')
        s['earnings_basis']={'shares':100,'basis':'POST_CASH_ELIGIBLE','source_id':'CAPITAL:TSLA',
                             'net_income':{'bear':1000,'base':1500,'bull':2000},'price_unit_multiplier':1,'price_basis':'PRE_BONUS',
                             'rationale':'이익은 모델 전망이며 공시의 예정 분모를 선택했다'}
        c={'context':{'capital_changes':{'source_id':'CAPITAL:TSLA','share_bases':{'POST_CASH_ELIGIBLE':100},'events':[{'kind':'BONUS','share_multiplier':2}]}},'model_contract':{'earnings_basis_required':True}}
        return s,c

    def test_matching_profit_division_and_declared_share_basis(self):
        s,c=self.fixture();validate_earnings_basis(s,c)
        for mutate in [lambda s:s['earnings_basis'].update(shares=80),
                       lambda s:s['inputs']['scenarios']['base'].update(eps=16),
                       lambda s:s['earnings_basis'].update(price_unit_multiplier=3),
                       lambda s:s['bases'][0]['evidence_refs'].remove('CAPITAL:TSLA')]:
            bad=deepcopy(s);mutate(bad)
            with self.assertRaises(AssumptionError):validate_earnings_basis(bad,c)

    def test_new_context_requires_basis_but_old_context_is_compatible(self):
        s,c=self.fixture();s.pop('earnings_basis')
        with self.assertRaises(AssumptionError):validate_earnings_basis(s,c)
        validate_earnings_basis(s,{'model_contract':{}})

    def test_model_forecast_is_not_replaced_by_a_python_default(self):
        s,c=self.fixture();s['earnings_basis'].update(basis='MODEL_SCENARIO',source_id=None,shares=200)
        for name in ('bear','base','bull'):
            s['inputs']['scenarios'][name]['eps']/=2
        validate_earnings_basis(s,c)
        self.assertEqual(s['earnings_basis']['shares'],200)
        self.assertEqual(s['inputs']['scenarios']['base']['multiple'],8)
        self.assertEqual(s['inputs']['scenarios']['base']['probability_pct'],50)


if __name__=='__main__': unittest.main()
