"""컨센서스 수집의 시점 혼합 방지 회귀 테스트.

목표주가 여러 개를 손으로 골라 평균 내면 시점이 다른 값이 섞인다. 주가가
저점이던 시절에 잡힌 목표가와 어제 잡힌 목표가는 같은 값이 아니다. 수집기가
그 실수를 대신 저지르면 안 된다.
"""

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch
from contextlib import redirect_stderr

from collectors import consensus


def broker(name, date, target, prior=None, change=None):
    return {
        "broker": name,
        "date": date,
        "target_price": target,
        "prior_target_price": prior,
        "change_pct": change,
        "rating": "BUY",
    }


class TestDispersionWindows(unittest.TestCase):
    def setUp(self):
        # 실제 파마리서치 2026-07-28 관측을 축약했다. 620,000은 5월 값이다.
        self.brokers = [
            broker("미래에셋", "2026-05-11", 600000.0),
            broker("대신", "2026-05-26", 620000.0),
            broker("KB", "2026-06-19", 430000.0),
            broker("삼성", "2026-07-16", 420000.0, 400000.0, 5.00),
            broker("키움", "2026-07-16", 510000.0, 440000.0, 15.91),
            broker("교보", "2026-07-16", 530000.0, 650000.0, -18.46),
            broker("상상인", "2026-07-28", 540000.0, 540000.0, 0.00),
        ]

    def test_recent_window_excludes_targets_set_at_a_different_price(self):
        dispersion, _ = consensus.summarize(self.brokers, "2026-07-28")
        wide = dispersion["consensus_window"]
        near = dispersion["recent"]
        self.assertEqual(wide["max"], 620000.0)
        self.assertEqual(near["max"], 540000.0)
        # 넓은 창은 47.6%, 좁은 창은 28.6% — 스프레드 자체가 다르게 읽힌다.
        self.assertGreater(wide["spread_pct"], near["spread_pct"])
        self.assertEqual(near["oldest"], "2026-07-16")

    def test_stale_target_lifting_the_mean_is_detectable(self):
        dispersion, _ = consensus.summarize(self.brokers, "2026-07-28")
        self.assertGreater(
            dispersion["consensus_window"]["max"], dispersion["recent"]["max"]
        )

    def test_revisions_count_direction_not_just_level(self):
        _, revisions = consensus.summarize(self.brokers, "2026-07-28")
        self.assertEqual(revisions["observed"], 4)
        self.assertEqual(revisions["raised"], 2)
        self.assertEqual(revisions["lowered"], 1)
        self.assertEqual(revisions["unchanged"], 1)
        self.assertEqual(revisions["largest_cut"]["broker"], "교보")
        self.assertEqual(revisions["largest_raise"]["broker"], "키움")


class TestParsing(unittest.TestCase):
    def test_missing_recommendations_do_not_shift_or_discard_earnings(self):
        html = ('<table id="cTB15"><tr><th>투자의견</th><th>목표주가(원)</th>'
                '<th>EPS(원)</th><th>PER(배)</th><th>추정기관수</th></tr>'
                '<tr><td>&nbsp;</td><td></td><td>857</td><td>54.66</td><td></td></tr></table>'
                '<table id="cTB24"><tr><td>최근 3개월 이내에 제시된 의견이 없습니다.</td></tr></table>')
        estimates = '<table id="cTB25"><tr><td>2027.12(E)</td>' + ''.join(f'<td>{v}</td>' for v in ['2293','39.86','666','545','1777','26.36','','20.11','20.55','42.84']) + '</tr></table>'
        packet = consensus.build('005930', html, estimates, collected_at='2026-09-08T13:00:00+09:00')
        self.assertIsNone(packet['summary']['target_price'])
        self.assertEqual(packet['summary']['eps'], 857)
        self.assertEqual(packet['summary']['per'], 54.66)
        self.assertIsNone(packet['summary']['institution_count'])
        self.assertEqual(packet['state'], 'PARTIAL')
        self.assertEqual(packet['components']['brokers']['state'], 'NO_DATA')
        self.assertEqual(packet['estimates']['annual'][0]['eps'], 1777)
        self.assertIsNone(packet['estimates']['annual'][0]['pbr'])
        self.assertEqual(packet['estimates']['annual'][0]['roe_pct'], 20.11)
        self.assertIsNone(packet['as_of'])
        self.assertIsNone(packet['estimates_as_of'])

    def test_parse_failure_is_not_absence(self):
        packet = consensus.build('005930', '<html>changed layout</html>', '<html>changed layout</html>')
        self.assertEqual(packet['state'], 'PARSE_ERROR')
        self.assertFalse(packet['available'])
        self.assertIsNone(packet['dispersion']['recent']['count'])
        self.assertIsNone(packet['revisions']['observed'])
        absent = consensus.build('005930', '', '추정실적이 없습니다')
        self.assertEqual(absent['state'], 'NO_DATA')

    def test_broker_rows_without_a_prior_target_are_kept(self):
        html = (
            '<table id="cTB24"><tr><td>제공처</td><td>최종일자</td>'
            "<td>목표가</td></tr>"
            "<tr><td>IBK투자</td><td>26/07/23</td><td>500,000</td>"
            "<td>매수</td></tr>"
            "<tr><td>키움</td><td>26/07/16</td><td>510,000</td>"
            "<td>440,000</td><td>15.91</td><td>BUY</td><td>BUY</td></tr>"
            "</table>"
        )
        rows = consensus.parse_brokers(html)
        self.assertEqual(len(rows), 2)
        self.assertIsNone(rows[0]["prior_target_price"])
        self.assertEqual(rows[1]["change_pct"], 15.91)
        self.assertEqual(rows[0]["date"], "2026-07-23")

    def test_summary_reads_the_value_row_after_the_header(self):
        html = (
            '<table id="cTB15">'
            "<tr><td>4.00</td><td>투자의견</td><td>목표주가(원)</td>"
            "<td>EPS(원)</td><td>PER(배)</td><td>추정기관수</td></tr>"
            "<tr><td>4.00</td><td>494,000</td><td>19,108</td>"
            "<td>19.65</td><td>15</td></tr>"
            "</table>"
        )
        summary = consensus.parse_summary(html)
        self.assertEqual(summary["target_price"], 494000.0)
        self.assertEqual(summary["eps"], 19108.0)
        self.assertEqual(summary["institution_count"], 15)


class TestHistory(unittest.TestCase):
    def test_network_failure_preserves_last_good_file_and_records_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'knowledge'/'consensus';out.mkdir(parents=True)
            original=b'{"last_good": true}\n';(out/'005930.json').write_bytes(original)
            with patch.object(consensus,'ROOT',root), patch.object(consensus,'OUT_DIR',out), patch.object(consensus,'RAW_DIR',root/'raw'), patch.object(consensus,'fetch',side_effect=SystemExit(1)), patch('sys.argv',['consensus','005930']), redirect_stderr(io.StringIO()):
                self.assertEqual(consensus.main(),1)
            self.assertEqual((out/'005930.json').read_bytes(),original)
            status=json.loads((root/'knowledge'/'collection_status'/'consensus-005930.json').read_text(encoding="utf-8"))
            self.assertEqual(status['state'],'NETWORK_ERROR')
            self.assertEqual(status['components']['estimates']['state'],'NETWORK_ERROR')

    def test_declared_peer_reaches_collection_without_its_own_registration(self):
        # 동종은 registry 의 peers 로만 선언되고 자기 항목이 없다. 2026-09-08 에 경로
        # 검증용 get_asset 이 들어오면서 동종 수집이 전부 '등록되지 않은 자산'으로
        # 죽었고, packet 은 5주 동안 낡은 동종 배수를 실었다.
        registry={'214450':{'name':'파마리서치','peers':[{'asset_id':'145020','name':'휴젤'}]}}
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);out=root/'knowledge'/'consensus'
            with patch('cio.assets.all_assets',return_value=registry), patch.object(consensus,'ROOT',root), patch.object(consensus,'OUT_DIR',out), patch.object(consensus,'RAW_DIR',root/'raw'), patch.object(consensus,'fetch',side_effect=SystemExit(1)), patch('sys.argv',['consensus','145020']), redirect_stderr(io.StringIO()):
                self.assertEqual(consensus.main(),1)
            status=json.loads((root/'knowledge'/'collection_status'/'consensus-145020.json').read_text(encoding="utf-8"))
            self.assertEqual(status['state'],'NETWORK_ERROR')

    def test_id_neither_registered_nor_declared_as_peer_is_refused(self):
        from cio.assets import AssetRegistryError
        registry={'214450':{'name':'파마리서치','peers':[{'asset_id':'145020','name':'휴젤'}]}}
        with patch('cio.assets.all_assets',return_value=registry), patch.object(consensus,'fetch',side_effect=AssertionError('수집 전에 거부해야 한다')), patch('sys.argv',['consensus','999999']):
            with self.assertRaises(AssetRegistryError):
                consensus.main()

    def test_failure_status_reaches_packet_without_a_success_file(self):
        from cio.context import _optional_collection_summaries, _data_coverage
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);knowledge=root/'knowledge';folder=knowledge/'collection_status';folder.mkdir(parents=True)
            (folder/'consensus-005930.json').write_text(json.dumps({'asset_id':'005930','attempted_at':'2026-09-08T13:00:00+09:00','state':'PARSE_ERROR','components':{'estimates':{'state':'PARSE_ERROR'}}}))
            asset={'asset_id':'005930','market':'KR','optional_collectors':[]}
            with patch('cio.context.ROOT',root),patch('cio.context.KNOWLEDGE_DIR',knowledge):
                summaries,_=_optional_collection_summaries(asset)
            coverage=_data_coverage(asset,structured_optional=summaries)
            self.assertEqual(coverage['forward_estimates']['state'],'PARSE_ERROR')

    def test_same_basis_date_does_not_grow_the_series(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "214450.json"
            packet = {
                "as_of": "2026-07-28",
                "collected_at": "2026-07-29T11:00:00+09:00",
                "summary": {
                    "rating": 4.0,
                    "target_price": 494000.0,
                    "eps": 19108.0,
                    "per": 19.65,
                    "institution_count": 15,
                },
            }
            first = consensus.merge_history(path, dict(packet))
            path.write_text(json.dumps(first), encoding="utf-8")
            again = consensus.merge_history(path, dict(packet))
            self.assertEqual(len(again["history"]), 1)

            moved = dict(packet)
            moved["as_of"] = "2026-08-04"
            path.write_text(json.dumps(again), encoding="utf-8")
            grown = consensus.merge_history(path, moved)
            self.assertEqual(len(grown["history"]), 2)
            self.assertEqual(
                [row["as_of"] for row in grown["history"]],
                ["2026-07-28", "2026-08-04"],
            )


class TestShowSurvivesMissingNumbers(unittest.TestCase):
    """적자 연도에는 PER 이 없다. 그것이 표 전체를 지우면 안 된다.

    실제로 SK하이닉스 2023년(영업손실)의 PER 이 null 이라 화면 함수가
    TypeError 로 죽었고, packet 은 정상 저장됐는데 refresh 는 FAILED 로
    보고했다. 없는 값은 크래시도 0 도 아니고 **없다고 보여야** 한다 —
    0 으로 채우면 PER 밴드가 0 부터 시작한다.
    """

    def packet(self):
        return {
            "asset_id": "000660",
            "as_of": "2026-08-03",
            "summary": {
                "rating": 4.0,
                "target_price": 3322083.0,
                "eps": None,
                "per": None,
                "institution_count": 24,
            },
            "dispersion": {
                "consensus_window": {
                    "count": 24, "min": 1480000.0, "max": 4700000.0,
                    "median": 3300000.0, "mean": 3322083.0,
                    "spread_pct": 217.57,
                    "oldest": "2026-06-01", "newest": "2026-08-03",
                },
                "recent": {
                    "count": 0, "min": None, "max": None, "median": None,
                    "mean": None, "spread_pct": None,
                    "oldest": None, "newest": None,
                },
            },
            "revisions": {
                "window_days": 90, "observed": 0, "raised": 0,
                "lowered": 0, "unchanged": 0,
                "largest_raise": None, "largest_cut": None,
            },
            "estimates": {
                "annual": [
                    {
                        "period": "2023-12", "actual": True,
                        "revenue_100m": 327657.0, "revenue_yoy_pct": None,
                        "operating_income_100m": -77303.0, "eps": -12517.0,
                        "per": None, "roe_pct": -15.61,
                    },
                    {
                        "period": "2026-12", "actual": False,
                        "revenue_100m": 3456048.0, "revenue_yoy_pct": 255.76,
                        "operating_income_100m": 2663757.0, "eps": 345914.0,
                        "per": 4.32, "roe_pct": 101.22,
                    },
                ],
                "per_band_actual": {
                    "years": 0, "min": None, "max": None, "median": None,
                },
            },
            "history": [],
        }

    def render(self, packet):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            consensus.show(packet)
        return buffer.getvalue()

    def test_a_loss_year_without_per_does_not_kill_the_table(self):
        text = self.render(self.packet())
        self.assertIn("2023-12(A)", text)
        self.assertIn("2026-12(E)", text)
        self.assertIn("4.3", text)

    def test_missing_numbers_render_as_absent_not_as_zero(self):
        text = self.render(self.packet())
        row = next(line for line in text.splitlines() if "2023-12(A)" in line)
        self.assertIn("—", row)
        # 0.0 으로 채우면 '적자라 PER 이 없다'가 'PER 0배'로 읽힌다.
        self.assertNotIn("0.0", row.split("-77,303")[-1])

    def test_summary_line_survives_a_missing_eps(self):
        text = self.render(self.packet())
        self.assertIn("컨센서스 EPS", text)

    def test_estimates_row_shorter_than_the_contract_is_still_printed(self):
        # 출처 표에 열이 하나 빠져 파서가 None 을 채운 경우다.
        packet = self.packet()
        packet["estimates"]["annual"][1] = {
            "period": "2027-12", "actual": False,
            "revenue_100m": None, "revenue_yoy_pct": None,
            "operating_income_100m": None, "eps": None,
            "per": None, "roe_pct": None,
        }
        text = self.render(packet)
        self.assertIn("2027-12(E)", text)


if __name__ == "__main__":
    unittest.main()
