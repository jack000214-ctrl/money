from copy import deepcopy
import unittest
from unittest.mock import patch

from cio.entry_plan import observations, plan_calculation, validate
from test_cio_core import valid_context, valid_decision


def plan_fixture():
    return {'currency':'USD','reference_basis':'질문 시점의 1주','reference_date':'2026-01-01','stages':[
        {'stage_id':'first','name':'첫 배분','budget':600,'reference_price':90,'share_multiplier':1,
         'requires_review':True,'condition':'실적을 확인한 뒤 모델이 성장 가정을 유지한다',
         'cancel_if':'모델이 논지 훼손을 확인하면 취소','valid_from':'2026-01-02','valid_until':'2026-12-31'}]}


class EntryPlanContract(unittest.TestCase):
    def test_split_on_first_following_session_does_not_create_a_loss(self):
        from cio.memory import evaluate_assessment
        d=valid_decision();d['assessment_id']='CIO-'+'1'*20
        bars=[{'date':date,'low':50,'high':50,'close':50,'adjusted_close':50,'has_adjusted_close':True,'split_adjustment_factor':factor}
              for date,factor in [('2026-01-01',.5),('2026-01-02',1),('2026-02-02',1)]]
        with patch('cio.memory.find_context',return_value=(valid_context(),None)), patch('cio.memory._price_session',return_value=None), patch('cio.memory._fixed_horizon_returns',return_value={}):
            result=evaluate_assessment(d,rows=bars)
        self.assertEqual(result['price']['return_to_end_pct'],0)
        self.assertEqual(result['price']['adjustment_factor_at_start'],.5)

    def test_budget_is_bound_to_context_and_current_allocation(self):
        d=valid_decision();ctx=valid_context();d['entry_plan']=plan_fixture()
        self.assertEqual(validate(d,ctx),[])
        result=plan_calculation(d['entry_plan'],d,ctx)
        self.assertEqual(result['total_deployed'],540)
        self.assertEqual(result['total_unspent'],460)
        d['proposed_trade']['amount']=500
        self.assertTrue(validate(d,ctx))
        d['proposed_trade']['amount']=0;ctx['request']['amount']=500
        self.assertTrue(validate(d,ctx))

    def test_duplicate_dates_currency_and_unbound_bonus_fail(self):
        for mutate in [lambda p:p['stages'].append(deepcopy(p['stages'][0])),
                       lambda p:p.update(currency='KRW'),
                       lambda p:p['stages'][0].update(valid_until='2025-12-31'),
                       lambda p:p['stages'][0].update(share_multiplier=2)]:
            d=valid_decision();d['entry_plan']=plan_fixture();mutate(d['entry_plan'])
            self.assertTrue(validate(d,valid_context()))

    def test_confirmation_must_reference_the_exact_old_condition(self):
        ctx=valid_context();plan=plan_fixture()
        ctx['context']={'prior_assessments':{'live':[{'assessment_id':'old','carried_forward':{'entry_plan':plan}}]}}
        d=valid_decision();d['entry_plan_checks']=[{'assessment_id':'old','stage_id':'first','condition':plan['stages'][0]['condition'],'state':'SATISFIED','note':'모델이 확인했다','evidence_refs':['PRICE:TSLA']}]
        self.assertEqual(validate(d,ctx),[])
        d['entry_plan_checks'][0]['condition']='다른 조건'
        self.assertTrue(validate(d,ctx))
        d['entry_plan_checks'][0]['condition']=plan['stages'][0]['condition'];d['entry_plan_checks'][0]['evidence_refs']=[]
        self.assertTrue(validate(d,ctx))

    def test_future_confirmation_cannot_backfill_an_earlier_cheap_price(self):
        a={'assessment_id':'old','created_at':'2026-01-01T00:00:00+00:00','entry_plan':plan_fixture()}
        bars=[{'date':date,'low':low,'split_adjustment_factor':1,'adjusted_close':110} for date,low in [('2026-01-02',80),('2026-01-03',80),('2026-01-04',100),('2026-01-05',85)]]
        check={'assessment_id':'old','stage_id':'first','state':'SATISFIED','recorded_at':'2026-01-03T09:00:00+00:00'}
        r=observations(a,bars,start_factor=1,end=bars[-1],reviews=[check])
        self.assertEqual(r['stages'][0]['price_touch_date'],'2026-01-02')
        self.assertEqual(r['stages'][0]['eligible_price_touch_date'],'2026-01-05')
        self.assertIsNone(r['strategy_return_pct'])
        no_review=observations(a,bars,start_factor=1,end=bars[-1])
        self.assertEqual(no_review['stages'][0]['observation'],'UNVERIFIABLE')
        self.assertIsNone(no_review['stages'][0]['hypothetical_return_pct'])

    def test_cancelled_stage_never_enters_after_a_later_touch(self):
        a={'assessment_id':'old','entry_plan':plan_fixture()}
        check={'assessment_id':'old','stage_id':'first','state':'CANCELLED','recorded_at':'2026-01-02T09:00:00+00:00'}
        bars=[{'date':'2026-01-03','low':80,'adjusted_close':100,'split_adjustment_factor':1}]
        r=observations(a,bars,start_factor=1,end=bars[-1],reviews=[check])
        self.assertIsNone(r['stages'][0]['eligible_price_touch_date'])

    def test_bonus_is_not_profit_and_wrong_share_basis_is_not_a_touch(self):
        a={'assessment_id':'old','entry_plan':plan_fixture()};a['entry_plan']['stages'][0]['share_multiplier']=2
        check={'assessment_id':'old','stage_id':'first','state':'SATISFIED','recorded_at':'2026-01-02T09:00:00+00:00'}
        bars=[{'date':'2026-01-03','low':40,'adjusted_close':45,'split_adjustment_factor':.5},
              {'date':'2026-01-04','low':40,'adjusted_close':45,'split_adjustment_factor':1}]
        r=observations(a,bars,start_factor=.5,end=bars[-1],reviews=[check])
        self.assertEqual(r['stages'][0]['eligible_price_touch_date'],'2026-01-04')
        self.assertEqual(r['stages'][0]['hypothetical_return_pct'],0)


if __name__=='__main__': unittest.main()
