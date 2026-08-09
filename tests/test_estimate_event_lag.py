"""신선도를 시간으로만 재면 사건 하나를 통째로 놓친다.

2026-08-09 파마리서치 packet 이 그랬다. `consensus:214450` 는 `as_of`
2026-07-28 로 refresh 가 FRESH 라고 답했는데, 그 사이 2026-08-07 에 2Q26
잠정실적이 공시됐다. 문제는 "12일 됐다"가 아니라 **그 EPS 가 실적 이전
세계의 숫자**라는 것이다.

그 위에서 나온 값이 판단에 그대로 들어갔다 — 검토 가격 378,500원의
"2026E PER 19.87배", "동종 중앙값 19.5배와 같은 수준". 실적을 반영하면
20배 초반이 되는데, packet 어디에도 그 EPS 가 사건 하나만큼 낡았다는 표시가
없었다.

시간 신선도로는 이것을 못 잡는다. 다시 수집해도 제공처가 아직 개정하지
않았으면 `as_of` 는 그대로 7/28 이다(실제로 그랬다). 그래서 이것은 **수집
지시가 아니라 판단 입력**이고, refresh 의 STALE 이 아니라 packet 의 관측으로
들어간다.

여기서 고정하는 성질은 둘이다. 사건이 지나갔으면 packet 이 그 사실을 말한다.
그리고 그 말은 배수가 계산되는 자리와 EPS 가 실린 자리 **둘 다**에 있다 —
한 곳만 보고 결론을 쓰는 경로가 실제로 있기 때문이다.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio import context
from cio.context import (
    _estimates_event_lag,
    _optional_collection_summaries,
    _with_event_lag,
)


FILINGS = {
    "schema_version": "filing-index/1.0.0",
    "asset_id": "214450",
    "source": "DART_LIST",
    "collected_at": "2026-08-09T08:53:32+00:00",
    "filings": [
        {
            "filing_date": "2026-08-07",
            "form": "PROVISIONAL_RESULT",
            "accession_number": "20260807900190",
            "description": "연결재무제표기준영업(잠정)실적(공정공시)",
        },
        {
            "filing_date": "2026-08-06",
            "form": "OTHER",
            "accession_number": "20260806900128",
            "description": "기업설명회(IR)개최",
        },
        {
            "filing_date": "2026-05-15",
            "form": "QUARTER_REPORT",
            "accession_number": "20260515001997",
            "description": "분기보고서 (2026.03)",
        },
    ],
}


class EventLagCase(unittest.TestCase):
    def lag(self, as_of, filings=None):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge = Path(tmp)
            (knowledge / "filings").mkdir()
            (knowledge / "filings" / "214450.json").write_text(
                json.dumps(FILINGS if filings is None else filings),
                encoding="utf-8",
            )
            with patch("cio.context.KNOWLEDGE_DIR", knowledge):
                return _estimates_event_lag("214450", as_of)


class TestEventLag(EventLagCase):
    def test_estimate_older_than_an_earnings_release_is_flagged(self):
        lag = self.lag("2026-07-28")
        self.assertEqual(lag["estimates_as_of"], "2026-07-28")
        self.assertEqual(lag["latest_event_date"], "2026-08-07")
        # 분기보고서(2026-05-15)는 기준일보다 앞이라 세지 않는다. 기준일
        # 이전의 실적까지 세면 추정은 언제나 "사건 뒤"가 된다.
        self.assertEqual(lag["events_since"], 1)

    def test_the_flagged_events_are_named_not_just_counted(self):
        # "사건이 있었다"만으로는 무엇을 다시 봐야 하는지 알 수 없다.
        lag = self.lag("2026-07-28")
        newest = lag["events"][0]
        self.assertEqual(newest["form"], "PROVISIONAL_RESULT")
        self.assertEqual(newest["accession_number"], "20260807900190")
        self.assertEqual(newest["filing_date"], "2026-08-07")
        self.assertIn("잠정", newest["description"])

    def test_periodic_reports_count_too_and_newest_comes_first(self):
        # 잠정실적만이 아니라 정기보고서도 실적을 새로 말한다.
        filings = {
            "filings": [
                {
                    "filing_date": "2026-08-14",
                    "form": "HALF_REPORT",
                    "accession_number": "half",
                    "description": "반기보고서 (2026.06)",
                },
                *FILINGS["filings"],
            ]
        }
        lag = self.lag("2026-07-28", filings=filings)
        self.assertEqual(lag["events_since"], 2)
        self.assertEqual(lag["latest_event_date"], "2026-08-14")
        self.assertEqual(
            [row["form"] for row in lag["events"]],
            ["HALF_REPORT", "PROVISIONAL_RESULT"],
        )

    def test_events_that_do_not_restate_earnings_are_ignored(self):
        # IR 개최 공시는 추정을 낡게 만들지 않는다. 전부 사건으로 세면
        # 이 표시는 항상 켜져 있게 되고 그러면 아무것도 말하지 않는다.
        lag = self.lag("2026-07-28")
        forms = [row["form"] for row in lag["events"]]
        self.assertNotIn("OTHER", forms)

    def test_estimate_newer_than_every_event_is_not_flagged(self):
        self.assertIsNone(self.lag("2026-08-08"))

    def test_estimate_on_the_same_day_as_the_event_is_not_flagged(self):
        # 같은 날 갱신된 추정은 그 공시를 봤을 수도 있다. 봤는지 못 봤는지
        # 로컬에서 가릴 수 없으므로 켜지 않는다 — 모르는 것을 경고로 바꾸면
        # 경고가 신호를 잃는다.
        self.assertIsNone(self.lag("2026-08-07"))

    def test_missing_inputs_do_not_raise(self):
        self.assertIsNone(self.lag(None))
        self.assertIsNone(self.lag(""))
        self.assertIsNone(self.lag("2026-07-28", filings={"filings": []}))
        self.assertIsNone(self.lag("2026-07-28", filings={}))

    def test_it_says_what_to_do_about_it(self):
        # 사실만 있고 무엇이 문제인지 없으면 모델이 그냥 지나친다.
        lag = self.lag("2026-07-28")
        self.assertIn("_role", lag)
        self.assertTrue(lag["_role"])


class TestFilingsWithoutDates(EventLagCase):
    def test_rows_without_a_filing_date_are_skipped(self):
        filings = {
            "filings": [
                {"form": "PROVISIONAL_RESULT", "accession_number": "x"},
                {
                    "filing_date": "2026-08-07",
                    "form": "PROVISIONAL_RESULT",
                    "accession_number": "y",
                },
            ]
        }
        lag = self.lag("2026-07-28", filings=filings)
        self.assertEqual(lag["events_since"], 1)
        self.assertEqual(lag["events"][0]["accession_number"], "y")


CONSENSUS = {
    "schema_version": "consensus-packet/1.0.0",
    "asset_id": "214450",
    "as_of": "2026-07-28",
    "source_grade": "EXPLORATORY",
    "summary": {"eps": 19052.0, "per": 19.87, "rating": 4.0},
    "estimates": {
        "annual": [
            {
                "period": "2026-12",
                "actual": False,
                "eps": 19052.0,
                "per": 19.87,
                "revenue_100m": 6782.0,
                "revenue_yoy_pct": 26.46,
                "operating_income_100m": 2704.0,
                "roe_pct": 28.39,
            }
        ]
    },
}


class TestItReachesThePacket(unittest.TestCase):
    """헬퍼가 맞아도 배선이 끊기면 packet 은 아무 말도 하지 않는다.

    이 표시는 **배수가 계산되는 자리**와 **EPS 가 실린 자리** 둘 다에 있어야
    한다. 2026-08-09 판단은 앞쪽의 19.87배를 인용하면서 뒤쪽은 개정 방향을
    보려고 따로 읽었다 — 하나만 표시하면 다른 쪽을 읽은 날 놓친다.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # `relative_to(ROOT)` 가 성립해야 하므로 knowledge 를 ROOT 안에 둔다.
        self.root = Path(self._tmp.name)
        self.knowledge = self.root / "knowledge"
        self.knowledge.mkdir()
        for name in ("filings", "consensus"):
            (self.knowledge / name).mkdir()
        (self.knowledge / "filings" / "214450.json").write_text(
            json.dumps(FILINGS), encoding="utf-8"
        )
        (self.knowledge / "consensus" / "214450.json").write_text(
            json.dumps(CONSENSUS), encoding="utf-8"
        )
        patcher = patch.multiple(
            context, KNOWLEDGE_DIR=self.knowledge, ROOT=self.root
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_eps_block_carries_it(self):
        summaries, _ = _optional_collection_summaries(
            {
                "asset_id": "214450",
                "market": "KR",
                "optional_collectors": [],
                "peers": [],
            }
        )
        lag = summaries["consensus"]["stale_across_event"]
        self.assertEqual(lag["latest_event_date"], "2026-08-07")

    def test_the_multiple_block_carries_it(self):
        block = _with_event_lag(
            {"consensus_as_of": "2026-07-28", "rows": []},
            "214450",
            as_of_key="consensus_as_of",
        )
        self.assertEqual(
            block["stale_across_event"]["latest_event_date"], "2026-08-07"
        )

    def test_a_current_estimate_is_left_alone(self):
        block = _with_event_lag(
            {"consensus_as_of": "2026-08-09", "rows": []},
            "214450",
            as_of_key="consensus_as_of",
        )
        self.assertNotIn("stale_across_event", block)

    def test_a_missing_block_stays_missing(self):
        # 미국 종목은 multiples_at_review_price 가 None 이다. 그 자리에
        # 빈 dict 를 만들어 주면 "배수가 있다"로 읽힌다.
        self.assertIsNone(
            _with_event_lag(None, "214450", as_of_key="consensus_as_of")
        )


if __name__ == "__main__":
    unittest.main()
