"""매도를 묻는 packet 이 매수 사다리를 만들면 안 된다.

직전 세션에서 실제로 이렇게 나왔다.

    intent:   TRIM_REVIEW
    question: "이 가격과 금액으로 매수하면 …"
    proposed_trade: 40,000,000원 → 803주, 매수 후 비중 27.76%

`--amount` 가 모든 intent 에 필수였고 `proposed_trade` 가 언제나 추가매수
수량·현금차감·매수 후 비중을 계산했기 때문이다. **묻지 않은 질문에 답을
만들어 주면 모델은 그 답을 근거로 받아들인다.**

두 번째 축은 준비상태다. 종목에 대한 관점을 세울 수 있는가와 실제 수량
지시를 낼 수 있는가는 다른 질문인데, 원장이 낡았거나 없어도 전부
`optional_gaps` 경고로만 남고 `PASS` 로 통과했다 — fail-open 이다. 수량
지시는 원장이 뒷받침할 때만 낼 수 있다.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio.assets import get_asset
from cio.context import _portfolio_summary, build_context
from cio.decision import context_id_for, validate_decision
from test_cio_core import valid_context, valid_decision

@dataclass
class Task:
    name: str
    required: bool = True
    state: str = "FRESH"
    reason: str = ""


HOLDINGS = """# 기준일 2026-08-06
asset_id,name,market,asset_type,risk_class,value_krw,fx_exposure,sector,quantity,pnl_krw,pnl_pct,note
214450,파마리서치,KR,STOCK,STOCK,9000000,,,30,2000000,28.6,
CASH_KRW,원화,KR,CASH,CASH,41000000,KRW,,,,,
"""


def _summary(**kwargs):
    """임시 원장으로 `_portfolio_summary` 만 돌린다."""
    options = {
        "amount": None,
        "amount_currency": "KRW",
        "price": 300_000.0,
        "intent": "TRIM_REVIEW",
    }
    options.update(kwargs)
    with tempfile.TemporaryDirectory() as tmp:
        knowledge = Path(tmp)
        path = knowledge / "portfolio" / "holdings.csv"
        path.parent.mkdir(parents=True)
        path.write_text(HOLDINGS, encoding="utf-8")
        with (
            patch("cio.context.KNOWLEDGE_DIR", knowledge),
            patch("cio.context.ROOT", knowledge),
        ):
            portfolio, _source = _portfolio_summary(get_asset("214450"), **options)
    return portfolio


class TestSellQuestionGetsNoBuyPlan(unittest.TestCase):
    def test_trim_review_does_not_size_a_purchase(self):
        trade = _summary(review_quantity=10)["proposed_trade"]
        self.assertEqual(trade["side"], "SELL")
        blob = json.dumps(trade, ensure_ascii=False)
        for field in ("estimated_quantity", "estimated_deployed_krw",
                      "estimated_unspent_budget_krw", "sufficient_cash"):
            self.assertNotIn(field, blob, f"매도 질문에 매수 계산 {field} 가 있다")

    def test_new_buy_still_sizes_a_purchase(self):
        trade = _summary(intent="NEW_BUY", amount=40_000_000.0)["proposed_trade"]
        self.assertEqual(trade["side"], "BUY")
        self.assertEqual(trade["estimated_quantity"], 133)

    def test_buy_question_gets_no_sale_arithmetic(self):
        """경로를 하나만 막으면 반대쪽으로 새어 나간다."""
        trade = _summary(intent="NEW_BUY", amount=40_000_000.0)["proposed_trade"]
        blob = json.dumps(trade, ensure_ascii=False)
        for field in ("review_quantity", "estimated_proceeds_krw",
                      "remaining_quantity"):
            self.assertNotIn(field, blob)

    def test_add_ladder_is_only_for_buy_questions(self):
        portfolio = _summary(review_quantity=10)
        self.assertIsNone(portfolio.get("add_ladder"))


class TestSaleArithmetic(unittest.TestCase):
    def test_remaining_quantity_and_proceeds(self):
        trade = _summary(review_quantity=10)["proposed_trade"]
        self.assertEqual(trade["held_quantity"], 30.0)
        self.assertEqual(trade["review_quantity"], 10.0)
        self.assertEqual(trade["remaining_quantity"], 20.0)
        self.assertAlmostEqual(trade["estimated_proceeds_krw"], 3_000_000.0)

    def test_cash_after_a_sale_goes_up(self):
        trade = _summary(review_quantity=10)["proposed_trade"]
        self.assertAlmostEqual(trade["cash_after_krw"], 44_000_000.0)

    def test_weight_after_a_sale_is_the_remaining_weight(self):
        """30주 중 10주를 팔면 남는 20주의 비중이 매도 후 비중이다."""
        trade = _summary(review_quantity=10)["proposed_trade"]
        remaining_value = 20 * 300_000.0
        total_after = 41_000_000.0 + 30 * 300_000.0
        self.assertAlmostEqual(
            trade["post_trade_weight_total_assets_pct"],
            round(remaining_value / total_after * 100, 2),
            places=2,
        )

    def test_target_remaining_quantity_is_converted(self):
        trade = _summary(target_quantity=20)["proposed_trade"]
        self.assertEqual(trade["review_quantity"], 10.0)
        self.assertEqual(trade["review_quantity_basis"], "DERIVED_FROM_TARGET")

    def test_without_a_review_quantity_nothing_is_invented(self):
        """수량을 안 정한 채 물을 수 있다. PC 가 대신 정하지 않는다."""
        trade = _summary(intent="HOLD_REVIEW")["proposed_trade"]
        self.assertIsNone(trade["review_quantity"])
        self.assertEqual(trade["review_quantity_basis"], "NOT_SPECIFIED")
        self.assertIsNone(trade.get("estimated_proceeds_krw"))
        self.assertEqual(trade["held_quantity"], 30.0)

    def test_selling_more_than_held_is_an_input_error(self):
        trade = _summary(review_quantity=40)["proposed_trade"]
        self.assertTrue(trade["contract_errors"])


class TestBudgetBelongsToBuyQuestions(unittest.TestCase):
    def _packet(self, **kwargs):
        return _build_packet(**kwargs)

    def test_sell_review_needs_no_budget(self):
        packet = self._packet(intent="TRIM_REVIEW", amount=None, review_quantity=10)
        self.assertEqual(packet["validation"]["problems"], [])
        self.assertIsNone(packet["request"]["amount"])

    def test_buy_intent_without_a_budget_is_blocked(self):
        packet = self._packet(intent="NEW_BUY", amount=None)
        self.assertTrue(
            any("금액" in problem for problem in packet["validation"]["problems"]),
            packet["validation"]["problems"],
        )

    def test_budget_on_a_sell_question_is_rejected(self):
        """우회로를 남기면 모델은 우회로를 쓴다."""
        packet = self._packet(intent="EXIT_REVIEW", amount=40_000_000.0)
        self.assertTrue(
            any("매수 예산" in problem for problem in packet["validation"]["problems"]),
            packet["validation"]["problems"],
        )

    def test_review_quantity_on_a_buy_question_is_rejected(self):
        packet = self._packet(
            intent="NEW_BUY", amount=40_000_000.0, review_quantity=10
        )
        self.assertTrue(
            any("검토 수량" in problem for problem in packet["validation"]["problems"]),
            packet["validation"]["problems"],
        )

    def test_the_question_matches_what_was_asked(self):
        packet = self._packet(intent="TRIM_REVIEW", amount=None, review_quantity=10)
        self.assertNotIn("매수하면", packet["request"]["question"])
        self.assertEqual(packet["request"]["intent_family"], "EXISTING_POSITION")

    def test_buy_question_is_unchanged(self):
        packet = self._packet(intent="NEW_BUY", amount=40_000_000.0)
        self.assertIn("매수", packet["request"]["question"])
        self.assertEqual(packet["request"]["intent_family"], "NEW_CAPITAL")


class TestReadinessIsTwoQuestions(unittest.TestCase):
    """종목 판단이 가능한가와 수량 지시가 가능한가는 다른 질문이다."""

    def test_both_readiness_states_are_reported(self):
        packet = _build_packet(intent="NEW_BUY", amount=40_000_000.0)
        readiness = packet["validation"]["readiness"]
        self.assertEqual(readiness["security_view"]["status"], "PASS")
        self.assertEqual(readiness["position_action"]["status"], "PASS")

    def test_a_stale_ledger_blocks_quantity_instructions_only(self):
        packet = _build_packet(
            intent="TRIM_REVIEW", amount=None, review_quantity=10, stale_ledger=True
        )
        readiness = packet["validation"]["readiness"]
        self.assertEqual(readiness["security_view"]["status"], "PASS")
        self.assertEqual(readiness["position_action"]["status"], "BLOCKED")
        self.assertEqual(packet["validation"]["status"], "PASS")

    def test_a_missing_ledger_blocks_quantity_instructions(self):
        packet = _build_packet(intent="HOLD_REVIEW", amount=None, portfolio=None)
        self.assertEqual(
            packet["validation"]["readiness"]["position_action"]["status"], "BLOCKED"
        )


class TestBlockedPositionActionReachesTheContract(unittest.TestCase):
    """준비상태가 packet 안에만 있으면 fail-open 이다. 계약이 읽어야 한다."""

    def _context(self, status):
        context = valid_context()
        context["validation"] = {
            "status": "PASS",
            "problems": [],
            "optional_gaps": [],
            "readiness": {
                "security_view": {"status": "PASS", "problems": []},
                "position_action": {"status": status, "problems": ["원장이 낡았다"]},
            },
        }
        context["context"] = {
            "portfolio": {
                "existing_position": {"held": True, "quantity": 30.0}
            }
        }
        context["context_id"] = context_id_for(context)
        return context

    def _decision(self, context, direction, quantity=None, reason=None):
        data = valid_decision()
        data["context_id"] = context["context_id"]
        data["verdict"] = "WAIT"
        data["proposed_trade"] = {**data["proposed_trade"], "amount": 0}
        data["action"] = {
            "direction": direction,
            "quantity": quantity,
            "sell_reason": "VALUATION" if direction in {"TRIM", "EXIT"} else None,
            "rationale": "이유",
            "undetermined_reason": reason,
        }
        return [
            error
            for error in validate_decision(data, context=context)
            if error.startswith("action")
        ]

    def test_trim_is_rejected_when_the_ledger_cannot_back_it(self):
        context = self._context("BLOCKED")
        self.assertTrue(self._decision(context, "TRIM", 10.0))

    def test_hold_is_rejected_too(self):
        """HOLD 는 '이 수량을 계속 들고 있어라'는 지시다."""
        context = self._context("BLOCKED")
        self.assertTrue(self._decision(context, "HOLD"))

    def test_undetermined_is_the_way_through(self):
        context = self._context("BLOCKED")
        self.assertEqual(
            self._decision(context, "UNDETERMINED", reason="NO_POSITION_DATA"), []
        )

    def test_passing_readiness_leaves_the_instruction_alone(self):
        context = self._context("PASS")
        self.assertEqual(self._decision(context, "TRIM", 10.0), [])


class TestNoBudgetMeansNoPurchase(unittest.TestCase):
    """매수 예산이 실리지 않은 packet 에서 집행액을 만들 수 없다."""

    def _context(self):
        context = valid_context()
        context["request"] = {
            **context["request"],
            "amount": None,
            "intent": "TRIM_REVIEW",
            "intent_family": "EXISTING_POSITION",
        }
        context["context_id"] = context_id_for(context)
        return context

    def test_spending_money_that_was_not_offered_is_rejected(self):
        context = self._context()
        data = valid_decision()
        data["context_id"] = context["context_id"]
        data["proposed_trade"] = {**data["proposed_trade"], "amount": 500.0}
        errors = validate_decision(data, context=context)
        self.assertTrue(any("proposed_trade.amount" in error for error in errors), errors)

    def test_zero_passes(self):
        context = self._context()
        data = valid_decision()
        data["context_id"] = context["context_id"]
        data["proposed_trade"] = {**data["proposed_trade"], "amount": 0.0}
        errors = [e for e in validate_decision(data, context=context)
                  if "proposed_trade" in e]
        self.assertEqual(errors, [])


class TestSupersededFinancialsNeedTheirDocument(unittest.TestCase):
    """로컬 재무가 낡았는데 그 공시 원문마저 없으면 확인할 경로가 없다.

    삼성전자 TTM 영업이익이 로컬 94.1조 vs 이미 공시된 179.0조로 1.9배
    벌어진 채 `optional_gap` 한 줄만 달고 PASS 로 통과했다. 색인에 제목이
    있다는 것은 근거가 있다는 뜻이 아니다.
    """

    EVENT = {
        "form": "PROVISIONAL_RESULT",
        "filing_date": "2026-07-30",
        "description": "연결재무제표기준영업(잠정)실적(공정공시)",
        "accession_number": "20260730800077",
        "document_url": "https://dart.fss.or.kr/x",
    }

    def test_missing_document_blocks_the_security_view(self):
        packet = _build_packet(
            intent="NEW_BUY", amount=40_000_000.0,
            filings={"latest_primary_reports": [{"form": "QUARTER_REPORT",
                                                 "report_date": "2026-03-31"}],
                     "latest_material_events": [self.EVENT]},
            stored_keys=set(),
        )
        self.assertEqual(packet["validation"]["status"], "BLOCKED")
        self.assertTrue(
            any("원문이 없다" in p for p in packet["validation"]["problems"]),
            packet["validation"]["problems"],
        )

    def test_a_stored_document_leaves_it_as_a_warning(self):
        packet = _build_packet(
            intent="NEW_BUY", amount=40_000_000.0,
            filings={"latest_primary_reports": [{"form": "QUARTER_REPORT",
                                                 "report_date": "2026-03-31"}],
                     "latest_material_events": [self.EVENT]},
            stored_keys={"20260730800077"},
        )
        self.assertEqual(packet["validation"]["status"], "PASS")
        self.assertTrue(
            any(gap["state"] == "SUPERSEDED_BY_DISCLOSURE"
                for gap in packet["validation"]["optional_gaps"])
        )


def _build_packet(
    *,
    intent,
    amount,
    review_quantity=None,
    target_quantity=None,
    stale_ledger=False,
    portfolio=(),
    filings=None,
    stored_keys=None,
):
    """수집물은 고정하고 intent 경로만 본다."""
    price = {
        "symbol": "214450",
        "currency": "KRW",
        "as_of": "2026-08-06",
        "close": 300_000.0,
        "returns_pct": {"1m": 1.0, "3m": 2.0, "12m": 3.0},
        "last_bar": {"date": "2026-08-06", "session": "SETTLED",
                     "is_settled_close": True, "_warning": None},
    }
    source = {
        "source_id": "PRICE:214450",
        "kind": "MARKET_DATA",
        "path": None,
        "data_as_of": "2026-08-06",
    }
    if portfolio == ():
        portfolio = {
            "available": True,
            "total_assets_krw": 50_000_000.0,
            "cash_krw": 41_000_000.0,
            "existing_position": {
                "held": True,
                "quantity": 30.0,
                "value_at_review_price_krw": 9_000_000.0,
                "weight_at_review_price_pct": 18.0,
            },
            "proposed_trade": {"side": "SELL", "held_quantity": 30.0,
                               "review_quantity": review_quantity,
                               "review_quantity_basis": "USER_INPUT"},
        }
    plan = [Task("price:214450")]
    if stale_ledger:
        plan.append(
            Task(
                "portfolio:holdings",
                required=False,
                state="STALE",
                reason="사용자가 갱신해야 한다",
            )
        )
    with (
        patch("cio.context._price_summary", return_value=(price, source)),
        patch(
            "cio.context._latest_fundamentals",
            return_value=(
                {"fundamentals_id": "FUND-x", "period_end": "2026-03-31"},
                {"source_id": "FUNDAMENTALS:214450", "kind": "FILING_DERIVED",
                 "path": None, "data_as_of": "2026-03-31"},
            ),
        ),
        patch("cio.context._filing_summary", return_value=(filings, None)),
        patch("cio.context.stored_filing_keys", return_value=stored_keys),
        patch("cio.context._earnings_calendar_section", return_value=(None, None)),
        patch("cio.context._latest_calculation", return_value=(None, None)),
        patch(
            "cio.context._market_summary",
            return_value=({"market": "KR", "regime": "NORMAL"},
                          {"source_id": "MARKET_STATE:KR", "kind": "DERIVED",
                           "path": None, "data_as_of": "2026-08-06"}),
        ),
        patch("cio.context._event_summary", return_value=(None, None)),
        patch("cio.context._etf_summary", return_value=(None, None)),
        patch("cio.context._optional_collection_summaries", return_value=({}, [])),
        patch("cio.context._portfolio_summary", return_value=(portfolio or {"available": False}, None)),
        patch("cio.context._attach_position_math", return_value=None),
        patch("cio.context._memory_digest", return_value=[]),
        patch("cio.context.documents_for_context", return_value=None),
        patch("cio.context.open_requests_for_context", return_value=[]),
        patch("cio.context.position_multiples", return_value=None),
        patch("cio.context.inspect_plan", return_value=plan),
    ):
        packet, _path = build_context(
            "214450",
            price=300_000.0,
            amount=amount,
            amount_currency="KRW",
            intent=intent,
            review_quantity=review_quantity,
            target_quantity=target_quantity,
            save=False,
        )
    return packet


if __name__ == "__main__":
    unittest.main()
