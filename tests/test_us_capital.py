"""Capital facts retain stock/flow, date and class boundaries."""
import copy
import unittest
from unittest.mock import patch

from collectors import us_capital as cap


def context(name, *, instant=None, start=None, end=None, dims=None):
    segment = ''.join(f'<xbrldi:explicitMember dimension="{a}">{m}</xbrldi:explicitMember>' for a,m in (dims or {}).items())
    period = f'<xbrli:instant>{instant}</xbrli:instant>' if instant else f'<xbrli:startDate>{start}</xbrli:startDate><xbrli:endDate>{end}</xbrli:endDate>'
    return f'<xbrli:context id="{name}"><xbrli:entity><xbrli:identifier>123</xbrli:identifier><xbrli:segment>{segment}</xbrli:segment></xbrli:entity><xbrli:period>{period}</xbrli:period></xbrli:context>'


def fact(concept, ctx, value, *, unit="shares", scale=0, extra=""):
    return f'<ix:nonFraction name="{concept}" contextRef="{ctx}" unitRef="{unit}" scale="{scale}" format="ixt:num-dot-decimal" {extra}>{value}</ix:nonFraction>'


def document(body, *, report_date="2026-06-30", accession="a", filing_date="2026-08-01"):
    return dict(source_id="WEBSNAP:"+accession,path="raw.json",content_sha256="abc",url="https://www.sec.gov/example",
                report_date=report_date,filing_date=filing_date,form="10-Q",accession_number=accession,body=body)


def fixture():
    return ('<html><ix:header><xbrli:unit id="shares"><xbrli:measure>xbrli:shares</xbrli:measure></xbrli:unit>'
            '<xbrli:unit id="USD"><xbrli:measure>iso4217:USD</xbrli:measure></xbrli:unit>'
            +context('total',instant='2026-06-30')
            +context('old',instant='2025-12-31')
            +context('cover',instant='2026-07-31')
            +context('a',instant='2026-06-30',dims={cap.CLASS_AXIS:'us-gaap:CommonClassAMember'})
            +context('exchange',instant='2026-06-30',dims={cap.CLASS_AXIS:'rxrx:ExchangeableStockMember'})
            +context('nested',instant='2026-06-30',dims={cap.CLASS_AXIS:'rxrx:ExchangeableStockMember','us-gaap:BusinessAcquisitionAxis':'rxrx:TargetMember'})
            +context('q',start='2026-04-01',end='2026-06-30')
            +context('ytd',start='2026-01-01',end='2026-06-30')+'</ix:header>'
            +fact('us-gaap:CommonStockSharesOutstanding','total','1,000')
            +fact('us-gaap:CommonStockSharesOutstanding','old','800')
            +fact('us-gaap:CommonStockSharesOutstanding','a','900')
            +fact('us-gaap:CommonStockSharesOutstanding','exchange','100')
            +fact('us-gaap:CommonStockSharesOutstanding','nested','100')
            +fact('dei:EntityCommonStockSharesOutstanding','cover','950')
            +fact('us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding','q','850')
            +fact('us-gaap:ShareBasedCompensation','q','12',unit='USD',scale=6)
            +fact('us-gaap:ShareBasedCompensation','ytd','20',unit='USD',scale=6)
            +fact('us-gaap:ProceedsFromIssuanceOfCommonStock','ytd','0',unit='USD')
            +'<p>Our at-the-market sales agreement permits up to $500 million of stock, and no sales were made.</p></html>')


class CapitalExtractionTests(unittest.TestCase):
    def test_cover_total_classes_and_eps_do_not_mix(self):
        result=cap.extract(fixture(),'2026-06-30')
        self.assertEqual(result['period_end_shares']['value'],1000)
        self.assertEqual(result['cover_shares']['facts'][0]['value'],950)
        self.assertEqual(result['cover_shares']['as_of'],'2026-07-31')
        self.assertEqual([r['value'] for r in result['share_classes']],[100,900])
        self.assertEqual(result['class_reconciliation']['state'],'MATCH')
        self.assertEqual(result['class_reconciliation']['component_sum'],1000)
        self.assertEqual(result['metrics']['weighted_average_shares']['facts'][0]['value'],850)
        self.assertEqual(len(result['share_observations']),6)

    def test_no_reported_total_does_not_promote_class_subtotal(self):
        body=fixture().replace(fact('us-gaap:CommonStockSharesOutstanding','total','1,000'),'')
        result=cap.extract(body,'2026-06-30')
        self.assertIsNone(result['period_end_shares']['value'])
        self.assertEqual(result['class_reconciliation']['component_sum'],1000)
        self.assertEqual(result['class_reconciliation']['state'],'NOT_COMPARABLE')

    def test_conflicts_remain_unknown_not_last_value(self):
        body=fixture()+fact('us-gaap:CommonStockSharesOutstanding','total','1,200')
        result=cap.extract(body,'2026-06-30')
        self.assertEqual(result['period_end_shares']['state'],'CONFLICTING_VALUES')
        self.assertIsNone(result['period_end_shares']['value'])

    def test_missing_is_distinct_from_observed_zero(self):
        result=cap.extract(fixture(),'2026-06-30')
        self.assertEqual(result['metrics']['issuance_proceeds']['facts'][0]['value'],0)
        self.assertEqual(result['metrics']['issuance_proceeds']['state'],'AVAILABLE')
        self.assertEqual(result['metrics']['convertible_debt']['state'],'NOT_EXTRACTED')
        self.assertEqual(result['metrics']['convertible_debt']['facts'],[])

    def test_durations_scales_and_atm_capacity_preserved(self):
        result=cap.extract(fixture(),'2026-06-30')
        rows=result['metrics']['stock_compensation_expense']['facts']
        self.assertEqual([(r['start'],r['value']) for r in rows],[('2026-01-01',20000000),('2026-04-01',12000000)])
        self.assertEqual(result['disclosures']['atm']['state'],'REVIEW_ORIGINAL')
        self.assertIsNone(result['disclosures']['atm']['capacity'])
        self.assertIsNone(result['disclosures']['atm']['used'])
        self.assertIn('$500 million',result['disclosures']['atm']['excerpts'][0]['text'])

    def test_history_changes_only_comparable_period_end_totals(self):
        data=cap.build('TEST',[document(fixture())])
        self.assertEqual([r['value'] for r in data['share_history']],[800,1000])
        change=data['share_history'][1]['change_from_previous']
        self.assertEqual(change['shares'],200)
        self.assertEqual(change['pct'],25)
        self.assertNotIn(950,[r['value'] for r in data['share_history']])

    def test_restatement_wins_by_filing_date_not_input_order(self):
        first=document(fixture(),accession='old',filing_date='2026-07-01')
        amended=document(fixture().replace('>1,000<','>1,100<'),accession='new',filing_date='2026-08-01')
        data=cap.build('TEST',[amended,first])
        self.assertEqual(data['share_history'][-1]['value'],1100)
        self.assertEqual(data['share_history'][-1]['source_id'],'WEBSNAP:new')

    def test_summary_keeps_current_quarter_and_ytd_separate(self):
        report=cap.summary(cap.build('TEST',[document(fixture())]))
        rows=report['metrics']['stock_compensation_expense']['facts']
        self.assertEqual(len(rows),2)
        self.assertEqual({r['start'] for r in rows},{'2026-01-01','2026-04-01'})
        self.assertIsNone(report['atm']['remaining'])

    def test_tampering_rejected_by_reextraction(self):
        doc=document(fixture())
        data=cap.build('TEST',[doc])
        with patch('collectors.sec_inline.validate_document_sources',return_value=[]), patch('collectors.sec_inline.read_document',return_value=doc):
            self.assertEqual(cap.validate_artifact(data,'TEST'),[])
            tampered=copy.deepcopy(data)
            tampered['reports'][0]['period_end_shares']['value']=9999
            self.assertTrue(cap.validate_artifact(tampered,'TEST'))
            tampered=copy.deepcopy(data)
            tampered['share_history'][-1]['change_from_previous']['shares']=0
            self.assertTrue(cap.validate_artifact(tampered,'TEST'))

    def test_source_error_propagates(self):
        data=cap.build('TEST',[document(fixture())])
        with patch('collectors.sec_inline.validate_document_sources',return_value=['source fingerprint mismatch']):
            self.assertEqual(cap.validate_artifact(data,'TEST'),['source fingerprint mismatch'])


if __name__=='__main__':
    unittest.main()
