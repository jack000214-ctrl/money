"""`WAIT` 를 안 샀는데 산 것처럼 채점하지 않는다.

이 파일이 고정하는 경계는 하나다 — **질문가 기준 수익률 하나로 WAIT 의 승패를
읽으면 안 된다.** WAIT 는 "이 가격에는 안 산다"는 판단이므로, 채점은 (1) 그냥
샀다면 (2) 제안 진입가에 걸어두고 기다렸다면 (3) 현금이었다면 셋으로 갈라야
한다. 특히 **체결 여부 자체가 결과다** — 계획이 실행되지 못한 것과 실행됐는데
틀린 것은 다른 사건이고, 섞으면 진입 계획의 품질을 영원히 못 읽는다.
"""

import unittest
from unittest.mock import patch

from cio.decision import normalize_decision
from cio.memory import (
    EVALUATION_SCHEMA,
    READABLE_EVALUATION_SCHEMAS,
    _counterfactual,
    evaluate_assessment,
)
from test_cio_core import valid_context, valid_decision


def _row(date, close, *, low, high, adjusted=None, factor=1.0):
    return {
        "date": date,
        "close": close,
        "adjusted_close": close if adjusted is None else adjusted,
        "has_adjusted_close": True,
        "split_adjustment_factor": factor,
        "high": high,
        "low": low,
    }


def _evaluate(rows, decision=None):
    assessment = normalize_decision(decision or valid_decision())
    with (
        patch(
            "cio.memory.get_asset",
            return_value={"asset_id": "TSLA", "price_symbol": "TSLA"},
        ),
        patch("cio.memory.find_context", return_value=(valid_context(), None)),
    ):
        return evaluate_assessment(assessment, rows=rows)


# 판단일 2026-01-01, 질문가 100.0, 선호진입가 95.0, 지평 1개월(2026-02-01).
class TestFilled(unittest.TestCase):
    def test_waiting_that_filled_is_scored_from_the_entry_not_the_ask(self):
        """저가가 진입가를 찍었으면 그 가격에서 잰다."""
        rows = [
            _row("2026-01-02", 96.0, low=94.0, high=98.0),
            _row("2026-01-31", 110.0, low=105.0, high=112.0),
            _row("2026-02-05", 115.0, low=113.0, high=117.0),
        ]
        waited = _evaluate(rows)["counterfactual"]["waited_for_entry"]
        self.assertTrue(waited["filled"])
        self.assertEqual(waited["fill"]["date"], "2026-01-02")
        self.assertEqual(waited["entry_price"], 95.0)
        self.assertEqual(waited["entry_price_basis"], "PREFERRED_ENTRY")
        # 110/95 - 1 = 15.7895%, 질문가 기준 10% 보다 유리하다.
        self.assertAlmostEqual(waited["return_to_end_pct"], 15.7895, places=3)
        self.assertAlmostEqual(waited["advantage_pct"], 5.7895, places=3)

    def test_split_does_not_move_the_fill_or_the_entry_return(self):
        """진입가는 판단 시점 호가다. 분할 뒤 저가와 직접 비교하면 안 된다."""
        rows = [
            _row("2026-01-02", 100.0, low=99.0, high=101.0,
                 adjusted=50.0, factor=0.5),
            _row("2026-01-15", 55.0, low=45.0, high=65.0),
            _row("2026-02-02", 56.0, low=54.0, high=60.0),
        ]
        waited = _evaluate(rows)["counterfactual"]["waited_for_entry"]
        # 95 는 분할 보정 후 47.5 다. 99*0.5=49.5 로는 안 채워지고 45 에서 채워진다.
        self.assertTrue(waited["filled"])
        self.assertEqual(waited["fill"]["date"], "2026-01-15")
        # 55/47.5 - 1 = 15.7895%. 조정 없이 55/95 로 재면 -42% 가 된다.
        self.assertAlmostEqual(waited["return_to_end_pct"], 15.7895, places=3)


class TestNeverFilled(unittest.TestCase):
    def test_unfilled_plan_has_no_return_because_it_had_no_position(self):
        """미체결은 '수익률 0'이 아니라 '포지션 없음'이다."""
        rows = [
            _row("2026-01-02", 105.0, low=99.0, high=106.0),
            _row("2026-01-31", 110.0, low=108.0, high=112.0),
            _row("2026-02-05", 115.0, low=113.0, high=117.0),
        ]
        result = _evaluate(rows)
        waited = result["counterfactual"]["waited_for_entry"]
        self.assertFalse(waited["filled"])
        self.assertIsNone(waited["fill"])
        self.assertIsNone(waited["return_to_end_pct"])
        self.assertIsNone(waited["return_to_latest_pct"])
        # 그냥 샀으면 +10% 였다. 기다린 대가는 그 기회를 통째로 놓친 것이다.
        self.assertAlmostEqual(
            result["price"]["return_to_end_pct"], 10.0, places=6
        )
        self.assertAlmostEqual(waited["advantage_pct"], -10.0, places=6)

    def test_unfilled_while_price_fell_credits_the_wait(self):
        """진입가까지는 안 왔지만 주가가 빠졌다면 안 산 것이 이득이다."""
        rows = [
            _row("2026-01-02", 97.0, low=96.0, high=99.0),
            _row("2026-01-31", 96.0, low=96.0, high=98.0),
            _row("2026-02-05", 95.5, low=95.2, high=97.0),
        ]
        result = _evaluate(rows)
        waited = result["counterfactual"]["waited_for_entry"]
        self.assertFalse(waited["filled"])
        self.assertAlmostEqual(result["price"]["return_to_end_pct"], -4.0, places=6)
        self.assertAlmostEqual(waited["advantage_pct"], 4.0, places=6)
        self.assertAlmostEqual(
            result["counterfactual"]["held_cash"]["advantage_pct"], 4.0, places=6
        )


class TestEntryPriceChoice(unittest.TestCase):
    def test_falls_back_to_range_high_and_says_so(self):
        """관대한 기준을 썼으면 그 사실이 출력에 남아야 한다."""
        decision = valid_decision()
        decision["price_plan"]["preferred_entry"] = None
        rows = [
            _row("2026-01-02", 99.0, low=98.0, high=101.0),
            _row("2026-01-31", 110.0, low=105.0, high=112.0),
            _row("2026-02-05", 115.0, low=113.0, high=117.0),
        ]
        waited = _evaluate(rows, decision)["counterfactual"]["waited_for_entry"]
        self.assertEqual(waited["entry_price"], 100.0)
        self.assertEqual(waited["entry_price_basis"], "ENTRY_RANGE_HIGH")
        self.assertTrue(waited["filled"])

    def test_no_plan_yields_no_entry_rather_than_a_guess(self):
        """가격계획이 없으면(INSUFFICIENT) 진입가를 지어내지 않는다."""
        block = _counterfactual(
            plan=None,
            path_until_horizon=[_row("2026-01-02", 100.0, low=90.0, high=110.0)],
            end=_row("2026-01-02", 100.0, low=90.0, high=110.0),
            latest=_row("2026-01-02", 100.0, low=90.0, high=110.0),
            adjustment_factor=1.0,
            realized=0.0,
            latest_return=0.0,
        )
        waited = block["waited_for_entry"]
        self.assertIsNone(waited["entry_price"])
        self.assertIsNone(waited["entry_price_basis"])
        self.assertIsNone(waited["filled"])
        self.assertIsNone(waited["advantage_pct"])


class TestExistingScoringUnchanged(unittest.TestCase):
    def test_ask_based_return_is_preserved_verbatim(self):
        """반사실은 **덧붙이는 것**이다. 고정 기간 비교의 기준점을 바꾸지 않는다.

        `price.return_to_end_pct` 와 `fixed_horizons` 는 모델 승격 비교가 쓰는
        자다. 여기를 진입가 기준으로 바꾸면 모델마다 다른 시작점에서 재게 되어
        고정 기간 평가가 존재하는 이유가 사라진다.
        """
        rows = [
            _row("2026-01-02", 96.0, low=94.0, high=98.0),
            _row("2026-01-31", 110.0, low=105.0, high=112.0),
            _row("2026-02-05", 115.0, low=113.0, high=117.0),
        ]
        result = _evaluate(rows)
        self.assertEqual(result["price"]["start"], 100.0)
        self.assertAlmostEqual(
            result["price"]["return_to_end_pct"], 10.0, places=6
        )
        self.assertEqual(
            result["counterfactual"]["bought_at_ask"]["return_to_end_pct"],
            result["price"]["return_to_end_pct"],
        )


class TestSchemaBumpDoesNotDropEvaluations(unittest.TestCase):
    def test_what_we_write_is_what_we_can_read(self):
        """판을 올릴 때 읽는 쪽을 같이 안 고치면 평가가 조용히 사라진다.

        `cio/context.py` 는 판이 안 맞는 평가 파일을 `continue` 로 건너뛴다.
        경고도 남기지 않으므로, 여기가 어긋나면 packet 에서 과거 성과가 통째로
        빠진 채 아무도 모른다. 판올림 자체를 막는 것이 아니라 **쓰는 판이 읽는
        목록에 들어 있는지**만 고정한다.
        """
        self.assertIn(EVALUATION_SCHEMA, READABLE_EVALUATION_SCHEMAS)
        # 옛 평가도 계속 읽혀야 한다. 안 그러면 판올림이 track record 를 지운다.
        self.assertIn("cio-evaluation/1.0.0", READABLE_EVALUATION_SCHEMAS)


if __name__ == "__main__":
    unittest.main()
