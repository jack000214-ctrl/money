"""The display must not change an order's price, quantity or budget."""

import unittest
from unittest.mock import patch

import helpers  # noqa: F401
from cio.report import format_evaluations
from test_cio_core import valid_decision
from test_decision_report import _render


class TestReportNumericPrecision(unittest.TestCase):
    def test_usd_cents_and_fractional_quantity_survive_rendering(self):
        decision = valid_decision()
        decision['price_plan'].update(currency='USD', preferred_entry=3.3,
            entry_range_low=3.1, entry_range_high=3.45, stop_price=2.8,
            scale_out_start=4.7, reask_below=3.1, reask_above=4.7)
        decision['action'].update(direction='TRIM', quantity=10.004156,
                                 limit_price=3.95, sell_reason='VALUATION')
        decision['proposed_trade'].update(amount=25.75, currency='USD')
        output = _render(decision)
        self.assertIn('10.004156주 · 지정가 3.95 USD', output)
        self.assertIn('제안 집행액 25.75 USD', output)
        for label, value in [('선호 진입', '3.30'), ('진입 범위 상단', '3.45'),
                             ('손절', '2.80'), ('분할매도 시작', '4.70')]:
            line = next(s for s in output.splitlines() if s.strip().startswith(label))
            self.assertTrue(line.rstrip().endswith(value), line)

    def test_sub_cent_and_small_fraction_are_not_rounded_to_zero(self):
        decision = valid_decision()
        decision['price_plan']['currency'] = 'USD'
        decision['action'].update(direction='TRIM', quantity=0.00000012,
                                 limit_price=0.0065, sell_reason='VALUATION')
        self.assertIn('0.00000012주 · 지정가 0.0065 USD', _render(decision))

    def test_krw_integer_execution_stays_integer(self):
        decision = valid_decision()
        decision['price_plan']['currency'] = 'KRW'
        decision['action'].update(direction='TRIM', quantity=10,
                                 limit_price=390000, sell_reason='VALUATION')
        self.assertIn('10주 · 지정가 390,000 KRW', _render(decision))

    def test_evaluation_plan_preserves_usd_prices(self):
        row = dict(created_at='2026-09-22', assessment_id='CIO-'+'a'*20,
                   source='LIVE', asked_price=3.97, verdict='WAIT', action='TRIM', triggered=[],
                   price_plan=dict(preferred_entry=3.3, stop_price=2.8, scale_out_start=4.7))
        report = dict(asset_id='TEST', name='Test', assessment_count=1,
                      scored_count=0, unscored_count=1, rows=[row])
        with patch('cio.report.evaluations_for', return_value=report):
            output = format_evaluations('TEST', with_plan=True)
        for value in ('3.97', '3.3', '2.8', '4.7'):
            self.assertIn(value, output)


if __name__ == '__main__':
    unittest.main()
