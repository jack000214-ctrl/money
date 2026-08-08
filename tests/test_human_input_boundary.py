"""사람이 손으로 넣는 것이 잘못됐을 때 멈추는가.

이 저장소에는 계산 주체가 셋 있고 그중 **사람이 넣는 것은 두 가지뿐**이다 —
질문(`cio prepare` 인자)과 손으로 채우는 원장·구성종목 CSV. 나머지는 전부
수집기가 만든다. 그래서 오타가 실제로 들어올 수 있는 지점도 이 둘뿐이고,
여기 검증이 새면 사람이 잘못 친 숫자가 그대로 packet 이 되어 모델에게 간다.

이 경계의 규칙 14 개가 테스트에서 한 번도 터지지 않았다. 직접 찔러 보니 전부
정상 작동했으므로 **지금 구멍은 없다** — 다만 리팩터로 조용히 죽는 것을 막을
것이 아무것도 없었다. 검증이 죽어도 결과는 "오류 없음"으로 똑같이 보인다.

`validation.status` 가 BLOCKED 여야 하는 자리들이다. BLOCKED 면 모델은
INSUFFICIENT 로만 답할 수 있고, 그것이 잘못된 질문에 대한 옳은 답이다.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from cio import context
from cio.context import ContextError
from test_intent_contract import _build_packet


class TestTheQuestionItself(unittest.TestCase):
    """`cio prepare` 로 들어오는 값. 사람이 직접 친다."""

    def problems(self, **kwargs):
        kwargs.setdefault("intent", "NEW_BUY")
        kwargs.setdefault("amount", 10_000_000)
        return _build_packet(**kwargs)["validation"]

    def assertBlocked(self, needle, **kwargs):
        validation = self.problems(**kwargs)
        self.assertEqual(validation["status"], "BLOCKED")
        self.assertTrue(
            any(needle in problem for problem in validation["problems"]),
            f"{needle!r} 가 없다 — 실제: {validation['problems']}",
        )

    def test_a_sane_question_passes(self):
        self.assertEqual(self.problems()["status"], "PASS")

    def test_an_unknown_intent_is_refused(self):
        """오타 난 intent 를 기본값으로 삼키면 묻지 않은 질문에 답하게 된다."""
        self.assertBlocked("intent는", intent="TRIM_REVIEWW")

    def test_a_zero_price_is_refused(self):
        """0 으로 나누는 자리가 아래에 여럿 있다."""
        self.assertBlocked("price는", price_value=0.0)

    def test_a_negative_price_is_refused(self):
        self.assertBlocked("price는", price_value=-1000.0)

    def test_a_zero_amount_is_refused(self):
        self.assertBlocked("amount는", amount=0.0)


class TestValuesThatCannotBeInAPacket(unittest.TestCase):
    """nan·inf 는 JSON 에 없다 — 담아 보낼 packet 자체가 없다.

    예전에는 문제만 기록하고 수집을 끝까지 마친 뒤 직렬화에서 죽었다. 사용자가
    본 것은 `Out of range float values are not JSON compliant` 였고, 위에서
    만든 "price는 0보다 큰 유한한 숫자여야 한다"는 그 packet 과 함께 사라졌다.
    가드는 있는데 메시지가 도달하지 못하는 상태였다.

    `argparse` 의 `type=float` 은 "nan" 도 "1e999"(→inf) 도 그대로 통과시킨다.
    """

    def build(self, **kwargs):
        kwargs.setdefault("intent", "NEW_BUY")
        kwargs.setdefault("amount", 10_000_000)
        return _build_packet(**kwargs)

    def assertStops(self, needle, **kwargs):
        with self.assertRaises(ContextError) as caught:
            self.build(**kwargs)
        self.assertIn(needle, str(caught.exception))

    def test_a_nan_price_stops_with_a_readable_reason(self):
        self.assertStops("price", price_value=float("nan"))

    def test_an_infinite_price_stops_with_a_readable_reason(self):
        self.assertStops("price", price_value=float("inf"))

    def test_a_nan_amount_stops_with_a_readable_reason(self):
        self.assertStops("amount", amount=float("nan"))

    def test_the_reason_names_the_field_the_user_typed(self):
        """JSON 인코더 오류에는 어느 인자가 문제인지가 없다."""
        with self.assertRaises(ContextError) as caught:
            self.build(price_value=float("inf"))
        message = str(caught.exception)
        self.assertIn("price", message)
        self.assertIn("유한하지 않은", message)

    def test_a_representable_bad_value_still_returns_a_blocked_packet(self):
        """0 은 담기기는 한다 — 모델이 이유를 읽고 INSUFFICIENT 로 답한다."""
        packet = self.build(price_value=0.0)
        self.assertEqual(packet["validation"]["status"], "BLOCKED")
        self.assertIn("price는 0보다 큰 유한한 숫자여야 한다",
                      packet["validation"]["problems"])


class TestTheQuestionCurrency(unittest.TestCase):
    """통화가 맞지 않으면 환산 없이 숫자가 섞인다."""

    def problems(self, **kwargs):
        kwargs.setdefault("intent", "NEW_BUY")
        kwargs.setdefault("amount", 10_000_000)
        return _build_packet(**kwargs)["validation"]

    def assertBlocked(self, needle, **kwargs):
        validation = self.problems(**kwargs)
        self.assertEqual(validation["status"], "BLOCKED")
        self.assertTrue(
            any(needle in problem for problem in validation["problems"]),
            f"{needle!r} 가 없다 — 실제: {validation['problems']}",
        )

    def test_an_unsupported_currency_is_refused(self):
        self.assertBlocked("amount_currency는", amount_currency="JPY")

    def test_a_korean_asset_priced_in_dollars_is_refused(self):
        """214450 에 USD 예산을 주면 환산 없이 원 단위로 섞인다."""
        self.assertBlocked("한국 자산", amount_currency="USD")


class TestTheHoldingsLedger(unittest.TestCase):
    """ETF 구성종목 CSV. 수집기가 없어 사람이 채우는 파일이다."""

    HEADER = "etf_id,holding_id,as_of,weight_pct\n"
    ASSET = {"asset_id": "XLK", "market": "US", "asset_type": "ETF",
             "name": "Technology"}

    def summarize(self, body):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            directory = root / "etfs" / "XLK"
            directory.mkdir(parents=True)
            (directory / "holdings-2026-08-01.csv").write_text(
                self.HEADER + body, encoding="utf-8")
            with patch.object(context, "KNOWLEDGE_DIR", root), \
                    patch.object(context, "ROOT", root):
                summary, _source = context._etf_summary(self.ASSET)
        return summary or {}

    def assertRejects(self, body, needle):
        errors = self.summarize(body).get("contract_errors") or []
        self.assertTrue(
            any(needle in error for error in errors),
            f"{needle!r} 가 없다 — 실제: {errors}",
        )

    def test_a_clean_file_produces_no_errors(self):
        summary = self.summarize(
            "XLK,AAPL,2026-08-01,10\nXLK,MSFT,2026-08-01,20\n")
        self.assertFalse(summary.get("contract_errors"))

    def test_a_row_for_another_etf_is_refused(self):
        """다른 ETF 의 줄이 섞이면 비중 합계가 조용히 틀어진다."""
        self.assertRejects("XLY,AAPL,2026-08-01,10\n", "etf_id가 요청 자산과 다르다")

    def test_a_blank_holding_id_is_refused(self):
        self.assertRejects("XLK,,2026-08-01,10\n", "holding_id가 비었다")

    def test_a_blank_as_of_is_refused(self):
        self.assertRejects("XLK,AAPL,,10\n", "as_of가 비었다")

    def test_a_non_numeric_weight_is_refused(self):
        """손으로 채우는 파일이라 '10%' 나 빈칸이 실제로 들어온다."""
        self.assertRejects("XLK,AAPL,2026-08-01,열개\n",
                           "weight_pct가 유효하지 않다")

    def test_a_negative_weight_is_refused(self):
        self.assertRejects("XLK,AAPL,2026-08-01,-5\n",
                           "weight_pct가 유효하지 않다")

    def test_a_duplicate_holding_is_refused(self):
        """같은 종목을 두 번 적으면 비중이 두 번 세어진다."""
        self.assertRejects(
            "XLK,AAPL,2026-08-01,10\nXLK,AAPL,2026-08-01,20\n",
            "holding_id가 중복됐다")

    def test_rows_from_different_dates_are_refused(self):
        """반쯤 갱신하다 만 파일이다 — 섞인 기준일은 한 시점의 구성이 아니다."""
        self.assertRejects(
            "XLK,AAPL,2026-08-01,10\nXLK,MSFT,2026-07-01,20\n",
            "기준일이 행마다 다르다")

    def test_all_zero_weights_are_refused(self):
        """비중 칸을 채우지 않은 파일이 '구성 없음'으로 읽히면 안 된다."""
        self.assertRejects("XLK,AAPL,2026-08-01,0\n", "비중 합계가 0보다 커야 한다")

    def test_the_row_number_points_at_the_line_to_fix(self):
        """사람이 고칠 파일이므로 몇 행인지가 있어야 한다."""
        self.assertRejects(
            "XLK,AAPL,2026-08-01,10\nXLK,,2026-08-01,20\n", "3행")


if __name__ == "__main__":
    unittest.main()
