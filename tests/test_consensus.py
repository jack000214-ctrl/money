"""컨센서스 수집의 시점 혼합 방지 회귀 테스트.

목표주가 여러 개를 손으로 골라 평균 내면 시점이 다른 값이 섞인다. 주가가
저점이던 시절에 잡힌 목표가와 어제 잡힌 목표가는 같은 값이 아니다. 수집기가
그 실수를 대신 저지르면 안 된다.
"""

import json
import tempfile
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
