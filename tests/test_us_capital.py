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


def filing_index(*accessions):
    periods = (("2026-06-30", "10-Q"), ("2026-03-31", "10-Q"), ("2025-12-31", "10-K"))
    return {"filings": [dict(report_date=periods[i][0], filing_date="2026-08-01",
                             form=periods[i][1], accession_number=accession) for i, accession in enumerate(accessions)]}


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

    def test_latest_unresolved_cover_does_not_inherit_earlier_available_state(self):
        body=fixture().replace('</ix:header>',context('later',instant='2026-08-15')+'</ix:header>')
        body+=fact('dei:EntityCommonStockSharesOutstanding','later','N/A')
        cover=cap.extract(body,'2026-06-30')['cover_shares']
        self.assertEqual(cover['as_of'],'2026-08-15')
        self.assertEqual(cover['state'],'NOT_EXTRACTED')
        self.assertEqual([(row['state'],row['value']) for row in cover['facts']],[('UNRESOLVED',None)])

    def test_conflicting_latest_cover_values_are_not_available(self):
        body=fixture()+fact('dei:EntityCommonStockSharesOutstanding','cover','1,200')
        cover=cap.extract(body,'2026-06-30')['cover_shares']
        self.assertEqual(cover['as_of'],'2026-07-31')
        self.assertEqual(cover['state'],'CONFLICTING_VALUES')
        self.assertEqual({row['value'] for row in cover['facts']},{950,1200})

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

    def test_only_explicit_report_date_atm_remaining_is_observed(self):
        sentence=('At-The-Market Offerings covered sales of Class A common stock. '
                  'As of June 30, 2026, an amount of '
                  '$300.0 million remained available for future sales under the Sales Agreement.')
        body=fixture().replace('</html>',f'<p>{sentence}</p></html>')
        remaining=cap.extract(body,'2026-06-30')['disclosures']['atm']['remaining']
        self.assertEqual(remaining['state'],'AVAILABLE')
        self.assertEqual((remaining['amount'],remaining['currency'],remaining['as_of']),
                         (300_000_000,'USD','2026-06-30'))
        self.assertIsNone(cap.extract(body,'2026-03-31')['disclosures']['atm']['remaining'])

    def test_multiple_report_date_atm_amounts_are_not_combined(self):
        text=('At-The-Market Offerings covered Class A common stock. '
              'As of June 30, 2026, $300 million remained available for future sales under the Sales Agreement. '
              'As of June 30, 2026, $200 million remained available for future sales under the Second Sales Agreement.')
        body=fixture().replace('</html>',f'<p>{text}</p></html>')
        remaining=cap.extract(body,'2026-06-30')['disclosures']['atm']['remaining']
        self.assertEqual(remaining['state'],'MULTIPLE_AMOUNTS_UNRESOLVED')
        self.assertIsNone(remaining['amount'])

    def test_adjacent_customer_sales_are_not_atm_capacity(self):
        text=('The at-the-market sales agreement was terminated. '
              'As of June 30, 2026, $300 million remained available for future sales of cloud services under a customer contract.')
        body=fixture().replace('</html>',f'<p>{text}</p></html>')
        self.assertIsNone(cap.extract(body,'2026-06-30')['disclosures']['atm']['remaining'])

    def test_customer_sales_agreement_does_not_inherit_atm_context(self):
        body=('<html><p>The at-the-market agreement was terminated. '
              'As of June 30, 2026, $300 million remained available for future sales '
              'under the Customer Sales Agreement.</p></html>')
        self.assertIsNone(cap._disclosures(body,'2026-06-30')['atm']['remaining'])

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

    def test_packet_omits_overlapping_scoped_sbc_but_artifact_retains_it(self):
        scoped=context('scoped_q',start='2026-04-01',end='2026-06-30',
                       dims={'us-gaap:StatementOfIncomeLocationBalanceAxis':'us-gaap:ResearchAndDevelopmentExpense'})
        body=fixture().replace('</ix:header>',scoped+'</ix:header>')
        body+=fact('us-gaap:ShareBasedCompensation','scoped_q','4',unit='USD',scale=6)
        data=cap.build('TEST',[document(body)])
        group=data['reports'][0]['metrics']['stock_compensation_expense']
        self.assertEqual(len(group['facts']),3)
        packet=cap.summary(data)['metrics']['stock_compensation_expense']
        self.assertEqual(len(packet['facts']),2)
        self.assertEqual(packet['omitted_current_observations'],1)
        self.assertEqual(packet['scoped_observation_count'],1)

    def test_tampering_rejected_by_reextraction(self):
        doc=document(fixture())
        data=cap.build('TEST',[doc])
        with patch('collectors.sec_inline.validate_document_sources',return_value=[]), patch('collectors.sec_inline.read_document',return_value=doc), patch('collectors.sec_inline.load_filings_index',return_value=filing_index('a')):
            self.assertEqual(cap.validate_artifact(data,'TEST'),[])
            tampered=copy.deepcopy(data)
            tampered['reports'][0]['period_end_shares']['value']=9999
            self.assertTrue(cap.validate_artifact(tampered,'TEST'))
            tampered=copy.deepcopy(data)
            tampered['share_history'][-1]['change_from_previous']['shares']=0
            self.assertTrue(cap.validate_artifact(tampered,'TEST'))

    def test_every_selected_filing_needs_a_report_or_collection_error(self):
        doc=document(fixture())
        with patch('collectors.sec_inline.validate_document_sources',return_value=[]), patch('collectors.sec_inline.read_document',return_value=doc), patch('collectors.sec_inline.load_filings_index',return_value=filing_index('a','b','c')):
            incomplete=cap.build('TEST',[doc],limit=3)
            self.assertIn('coverage',cap.validate_artifact(incomplete,'TEST')[0])
            partial=cap.build('TEST',[doc],collection_errors=[
                {'accession_number':'b','error':'ORIGINAL_NOT_COLLECTED'},
                {'accession_number':'c','error':'ORIGINAL_NOT_COLLECTED'}],limit=3)
            self.assertEqual(cap.validate_artifact(partial,'TEST'),[])
            partial['collection_errors'].append({'accession_number':'b','error':'duplicate'})
            self.assertIn('coverage',cap.validate_artifact(partial,'TEST')[0])

    def test_source_error_propagates(self):
        data=cap.build('TEST',[document(fixture())])
        with patch('collectors.sec_inline.validate_document_sources',return_value=['source fingerprint mismatch']):
            self.assertEqual(cap.validate_artifact(data,'TEST'),['source fingerprint mismatch'])


if __name__=='__main__':
    unittest.main()
