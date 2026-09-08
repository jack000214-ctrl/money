import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from calculators.contracts import canonical_payload_hash
from collectors.kr_capital import contract_errors, extract, make_report, selected_filing


def xml_fixture():
    return '''<DOCUMENT><COMPANY-NAME AREGCIK="12345678">Example</COMPANY-NAME>
      <CORRECTION><TABLE><TE ACODE="CST_CNT">9999</TE></TABLE></CORRECTION>
      <TABLE><TE ACODE="CST_CNT">20</TE><TE ACODE="BFR_CST_CNT">100</TE><TU AUNIT="CI_MTH" AUNITVALUE="3">배정</TU></TABLE>
      <TABLE><TE ACODE="PRE_CST_VAL">40</TE><TE ACODE="CST_ISS_VAL">-</TE><TU AUNIT="PYM_DT" AUNITVALUE="20260115"/><TU AUNIT="ALL_BS_DT" AUNITVALUE="20260110"/></TABLE>
      <TABLE><TE ACODE="CST_CNT">110</TE><TE ACODE="BFR_CST_CNT">120</TE><TE ACODE="NEW_ASN_CST">1</TE><TU AUNIT="ALL_BS_DT" AUNITVALUE="20260120"/><TU AUNIT="LST_PLN_DT" AUNITVALUE="20260201"/></TABLE>
      </DOCUMENT>'''


class CapitalAnnouncement(unittest.TestCase):
    def test_grouped_tables_and_correction_scope(self):
        r = extract({'doc.xml': xml_fixture()}, corp_code='12345678', description='유무상증자결정')
        self.assertEqual(r['share_bases']['POST_CASH_ELIGIBLE'], 110)
        self.assertEqual(r['share_bases']['POST_BONUS_ELIGIBLE'], 220)
        self.assertEqual(r['events'][0]['payment_date'], '2026-01-15')
        self.assertEqual(r['events'][0]['indicative_price'], 40)
        self.assertIsNone(r['events'][0]['issue_price'])
        self.assertIsNone(r['events'][0]['completed_at'])
        self.assertIsNone(r['events'][1]['ex_date'])
        # Past planned dates do not certify execution, nor create today's cash.
        self.assertTrue(all(e['state'] == 'ANNOUNCED' for e in r['events']))
        self.assertNotIn('cash_now', r)

    def test_wrong_company_and_inconsistent_share_rollforward_fail(self):
        with self.assertRaises(ValueError):
            extract({'x': xml_fixture()}, corp_code='87654321', description='증자')
        with self.assertRaises(ValueError):
            extract({'x': xml_fixture().replace('>120<', '>121<')}, corp_code='12345678', description='증자')

    def test_latest_cancellation_or_amendment_replaces_announcement(self):
        original={'filing_date':'2026-01-01','accession_number':'20260101000001','description':'주요사항보고서(유무상증자결정)'}
        cancelled={**original,'filing_date':'2026-01-05','accession_number':'20260105000001','description':'[철회]주요사항보고서(유무상증자결정)'}
        self.assertEqual(selected_filing({'filings':[cancelled,original]}),cancelled)
        self.assertEqual(extract({},corp_code='12345678',description=cancelled['description'])['state'],'CANCELLED')

    def test_changed_extraction_is_rejected_even_with_a_valid_source(self):
        with tempfile.TemporaryDirectory() as tmp, patch('collectors.kr_capital.ROOT',Path(tmp)):
            root=Path(tmp);path=root/'source.json';payload={'rcept_no':'20260101000001','documents':{'x':xml_fixture()}}
            raw={'payload':payload,'payload_sha256':canonical_payload_hash(payload)};path.write_text(json.dumps(raw))
            row={'filing_date':'2026-01-01','accession_number':'20260101000001','description':'주요사항보고서(유무상증자결정)'}
            index={'corp_code':'12345678','filings':[row]}
            report=make_report('TEST',index,row,raw,path)
            self.assertEqual(contract_errors(report,'TEST',index=index),[])
            forged=copy.deepcopy(report);forged['events'][0]['completed_at']='2026-01-15'
            self.assertTrue(contract_errors(forged,'TEST',index=index))
            forged=copy.deepcopy(report);forged['share_bases']['POST_CASH_ELIGIBLE']=120
            self.assertTrue(contract_errors(forged,'TEST',index=index))


if __name__ == '__main__':
    unittest.main()
