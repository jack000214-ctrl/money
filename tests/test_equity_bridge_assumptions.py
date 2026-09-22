"""Pre-revenue valuation arithmetic includes burn and dilution, not a PC verdict."""

from copy import deepcopy
import unittest

import helpers  # noqa: F401
from calculators.assumptions import AssumptionError, analyze, calculate, parameter_values
from cio.assumptions import create_analysis, validate_review
import test_assumption_analysis as persistence
from test_assumption_analysis import spec_fixture


def bridge_spec():
    spec = spec_fixture()
    spec['inputs']['method'] = 'EQUITY_BRIDGE'
    for name, ev in [('bear', 200), ('base', 1000), ('bull', 2000)]:
        row = spec['inputs']['scenarios'][name]
        for key in ['eps', 'multiple', 'cash_distribution']:
            row.pop(key)
        row.update(enterprise_value=ev, cash=300, debt=100, shares=10)
    spec['bases'][0]['parameters'] = list(parameter_values(spec['inputs']))
    spec['tests'] = [{'id': 'funding', 'changes': {'scenarios.base.cash': 500, 'scenarios.base.shares': 15},
                      'rationale': '새 자금 유입과 주식수 증가를 함께 대조한다'}]
    spec['boundaries'] = [{'id': 'required_ev', 'parameter': 'scenarios.base.enterprise_value', 'low': 0, 'high': 3000}]
    return spec


class TestBridgeMath(unittest.TestCase):
    def test_cash_debt_dilution_and_boundary(self):
        result = analyze(bridge_spec(), 10)
        self.assertEqual(result['baseline']['scenarios']['base']['value_per_share'], 120)
        # 25%*40 + 50%*120 + 25%*220 = 125 (price 100).
        self.assertAlmostEqual(result['baseline']['expected_return_pct'], 25)
        self.assertAlmostEqual(result['boundaries'][0]['required_value'], 700)
        self.assertAlmostEqual(result['tests'][0]['expected_return_pct'], 11.6666666667)
        self.assertNotIn('verdict', result)

    def test_equity_floor_and_invalid_units(self):
        spec = bridge_spec()
        spec['inputs']['scenarios']['bear'].update(debt=1000)
        row = calculate(spec['inputs'])['scenarios']['bear']
        self.assertEqual(row['value_per_share'], 0)
        self.assertEqual(row['total_return_pct'], -100)
        self.assertEqual(row['raw_equity_value_per_share'], -50)
        for field, value in [('cash', -1), ('debt', -1), ('shares', 0), ('enterprise_value', float('inf'))]:
            mutated = deepcopy(spec)
            mutated['inputs']['scenarios']['base'][field] = value
            with self.subTest(field=field), self.assertRaises(AssumptionError):
                calculate(mutated['inputs'])


class TestBridgeBinding(unittest.TestCase):
    setUp = persistence.TestAssumptionPersistence.setUp
    decision = persistence.TestAssumptionPersistence.decision
    def test_bridge_is_bound_to_decision_and_cannot_be_bypassed(self):
        spec = bridge_spec()
        report = create_analysis(spec, self.context)
        decision = self.decision(report)
        decision['method']['valuation_family'] = 'EQUITY_BRIDGE'
        self.assertEqual(validate_review(decision, self.context), [])
        decision['method']['valuation_family'] = 'DCF'
        self.assertTrue(validate_review(decision, self.context))
        decision['method']['valuation_family'] = 'EQUITY_BRIDGE'
        decision['assumption_review'].update(status='UNSUPPORTED_METHOD', analysis_id=None, responses=[])
        self.assertTrue(validate_review(decision, self.context))


if __name__ == '__main__':
    unittest.main()
