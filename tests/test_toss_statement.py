"""거래내역서 대조기가 지켜야 할 것.

이 수집기가 존재하는 이유는 하나다 — 사용자 진술로 이어 적은 원장이 두 달 만에
매매 일곱 건을 잃었고, 팔고 없는 30주를 "보유"로 읽은 판단이 기록됐다. 그래서
여기서 보는 것도 세 가지뿐이다. 내역서를 빠짐없이 읽었다는 것을 내역서 자신의
잔액으로 증명하는가, 원장을 내역서와 같게 만드는가, 그러면서 건드리면 안 되는
것(기간 밖의 행·note·holdings.csv)을 그대로 두는가.

아래 내역서는 전부 지어낸 것이다. 실제 계좌의 행은 테스트에 넣지 않는다.
"""
from __future__ import annotations

import datetime as dt
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from calculators import position as position_module
from collectors import toss_statement as ts

D = Decimal
MAPPING = {"US0000000001": "TETF", "US0000000002": "ALPHA"}


class FakeStatement:
    """잔액과 잔고가 스스로 이어지는 `pdftotext -layout` 모양의 글을 만든다."""

    def __init__(self, krw: int = 10_000_000) -> None:
        self.krw = krw
        self.usd = D("0.00")
        self.position: dict[str, Decimal] = {}
        self.krw_lines: list[str] = []
        self.usd_lines: list[str] = []

    def _move(self, key: str | None, kind: str, qty) -> str:
        # 배당·이자 행은 종목이 적혀 있어도 잔고 칸이 0 이다
        if key is None or not (kind in ("구매", "판매") or kind.endswith(("입고", "출고"))):
            return "0"
        delta = D(str(qty)) if kind == "구매" or kind.endswith("입고") else -D(str(qty))
        self.position[key] = self.position.get(key, D(0)) + delta
        return ts.plain(self.position[key])

    def won(self, date, kind, sign, *, label="", code=None, qty=0, amount=0, price=0,
            fee=0, trade_tax=0, other_tax=0, fx=None):
        settle = amount + fee if kind == "구매" else amount - fee - trade_tax - other_tax
        self.krw += sign * settle
        name = f"{label}(A{code})" if code else label
        rate = f"{fx:,.2f}" if fx else ""
        self.krw_lines.append(
            f"{date}   {kind}   {name}   {rate}   {qty}   {amount:,}   {settle:,}   "
            f"{price:,}   {fee:,}   {trade_tax:,}   {other_tax:,}   0   "
            f"{self._move(code, kind, qty)}   {self.krw:,}")
        return self

    def dollar(self, date, kind, sign, *, label="", isin=None, wrap=False, qty=0,
               amount="0", price="0", fee="0", tax="0", fx="1400"):
        amount, price, fee, tax, fx = (D(str(v)) for v in (amount, price, fee, tax, fx))
        settle = amount + fee if kind == "구매" else amount - fee - (tax if kind == "판매" else 0)
        transfer = kind.endswith(("입고", "출고"))
        self.usd += sign * settle
        name = label + (f"({isin})" if isin and not wrap else "")
        to_won = lambda value: f"{int(value * fx):,}"   # noqa: E731
        balance = D(0) if transfer else self.usd
        self.usd_lines.append(
            f"{date}   {kind}   {name}   {fx:,.2f}   {qty}   {to_won(amount)}   "
            f"{to_won(settle)}   {to_won(price)}   {to_won(fee)}   {to_won(tax)}   0   "
            f"{self._move(isin, kind, qty)}   {to_won(balance)}")
        self.usd_lines.append(
            f"        ($ {amount:,}) ($ {settle:,}) ($ {price:,}) ($ {fee:,}) "
            f"($ {tax:,}) ($ 0.00) ($ {balance:,})")
        if isin and wrap:
            self.usd_lines.append(f"            ({isin})")
        return self

    def text(self) -> str:
        return "\n".join([
            "거래내역서", "성명   아무개", "계좌 번호   000-00-000000",
            "조회 기간           2026년 1월 1일 ~ 2026년 3월 31일",
            "원화 거래내역            수량단위 : 주, 원 | 금액단위 : 원",
            "거래일자   거래구분   종목명(종목코드)   환율   거래수량   거래대금   정산금액",
            *self.krw_lines,
            "달러 거래내역            수량단위 : 주, 달러 | 금액단위 : 원, 달러",
            "거래일자   거래구분   종목명(종목코드)   환율   거래수량   거래대금   정산금액",
            *self.usd_lines,
            "발급일자   2026년 4월 1일",
        ])


def sample() -> FakeStatement:
    fake = FakeStatement()
    fake.won("2026.01.05", "이자입금", 1, amount=1000, other_tax=150)
    fake.won("2026.01.10", "구매", -1, label="테스트전자", code="111111", qty=10,
             amount=500_000, price=50_000, fee=100)
    fake.won("2026.01.20", "구매", -1, label="테스트전자", code="111111", qty=5,
             amount=240_000, price=48_000, fee=50)
    fake.won("2026.02.03", "판매", 1, label="테스트전자", code="111111", qty=15,
             amount=900_000, price=60_000, fee=180, trade_tax=450, other_tax=1350)
    fake.won("2026.02.10", "환전원화출금", -1, amount=1_400_000, fx=1400)
    fake.won("2026.02.20", "구매", -1, label="가나다", code="222222", qty=3,
             amount=300_000, price=100_000, fee=60)
    fake.won("2026.03.02", "이체입금(은행)", 1, label="홍길동", amount=1_000_000)
    fake.won("2026.03.12", "구매", -1, label="다라", code="333333", qty=4,
             amount=40_000, price=10_000)
    fake.won("2026.03.20", "판매", 1, label="다라", code="333333", qty=4,
             amount=44_000, price=11_000, trade_tax=88)
    fake.dollar("2026.02.10", "환전외화입금", 1, amount="1000.00")
    fake.dollar("2026.02.12", "구매", -1, label="테스트 배당 ETF", isin="US0000000001",
                wrap=True, qty=20, amount="200.00", price="10.00", fee="0.20", fx="1410")
    fake.dollar("2026.02.15", "출석체크이벤트입고", 0, label="알파", isin="US0000000002",
                qty="0.0001", price="355.87", fx="1405")  # pyright: ignore[reportArgumentType]
    fake.dollar("2026.03.01", "외화증권배당금입금", 1, label="테스트 배당 ETF",
                isin="US0000000001", wrap=True, qty=20, amount="2.00", tax="0.35")
    fake.dollar("2026.03.10", "판매", 1, label="테스트 배당 ETF", isin="US0000000001",
                wrap=True, qty=5, amount="60.00", price="12.00", fee="0.06", tax="0.01")
    return fake


OLD_KR = """# 테스트전자 (111111) 매매 내역
# 순 12주 — 사용자 진술로 이어 적은 원장
settle_date,side,quantity,price_krw,fee_krw,tax_krw,note
2025-12-01,BUY,1,40000,0,0,기간 전 매수
2025-12-15,SELL,1,41000,0,82,기간 전 매도
2026-01-10,BUY,10,50000,0,0,체결 1/8 사용자 진술 / 지정가 51,000원보다 1,000원 유리 / 수수료 미확인
2026-01-25,BUY,2,49000,0,0,기억으로 적은 행
2026-04-02,BUY,1,52000,0,0,기간 뒤 매수
"""
OLD_US = """# 테스트 배당 ETF (TETF) 매매 내역
settle_date,side,quantity,price_krw,price_usd,fx_rate,fee_krw,note
2026-02-12,BUY,20,14100,10,1410,282,적립 매수
"""
HOLDINGS = """# 보유 내역 / 기준일 2026-03-31
asset_id,name,market,asset_type,risk_class,value_krw,fx_exposure,sector,quantity,pnl_krw,pnl_pct,note
111111,테스트전자,KR,STOCK,STOCK,600000,,,12,0,0,
TETF,테스트 배당 ETF,US,ETF,BROAD_ETF,300000,,,15,0,0,
ZZZ,거래 없는 종목,US,STOCK,STOCK,100000,,,7,0,0,
CASH_KRW,원화,KR,CASH,CASH,2000000,KRW,,,,,
"""


class TempLedgers(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.trades = self.root / "trades"
        self.trades.mkdir()
        (self.trades / "111111.csv").write_text(OLD_KR, encoding="utf-8")
        (self.trades / "TETF.csv").write_text(OLD_US, encoding="utf-8")
        (self.root / "holdings.csv").write_text(HOLDINGS, encoding="utf-8")
        for name, value in (("TRADES_DIR", self.trades),
                            ("HOLDINGS", self.root / "holdings.csv"),
                            ("BACKUP_DIR", self.root / "backup")):
            original = getattr(ts, name)
            setattr(ts, name, value)
            self.addCleanup(setattr, ts, name, original)
        self.statement = ts.parse_text(sample().text())

    def plan(self, mapping=None):
        return ts.plan(self.statement, MAPPING if mapping is None else mapping)


class TestParsing(unittest.TestCase):
    def setUp(self):
        self.statement = ts.parse_text(sample().text())

    def test_period_and_every_row_are_read(self):
        self.assertEqual(self.statement["period_start"], "2026-01-01")
        self.assertEqual(self.statement["period_end"], "2026-03-31")
        sections = [row["section"] for row in self.statement["rows"]]
        self.assertEqual((sections.count("KRW"), sections.count("USD")), (9, 5))

    def test_a_clean_statement_proves_itself(self):
        problems, warnings = ts.verify(self.statement)
        self.assertEqual((problems, warnings), ([], []))

    def test_isin_wrapped_onto_the_next_line_is_attached(self):
        buy = next(r for r in self.statement["rows"]
                   if r["section"] == "USD" and r["type"] == "구매")
        self.assertEqual(buy["security"], "US0000000001")
        self.assertEqual(buy["name"], "테스트 배당 ETF")
        self.assertEqual((buy["price_usd"], buy["fx"]), (D("10.00"), D("1410.00")))

    def test_tax_is_trade_tax_plus_other_tax(self):
        sell = next(r for r in self.statement["rows"]
                    if r["security"] == "111111" and r["type"] == "판매")
        self.assertEqual(sell["tax"], D(1800))

    def test_counterparty_name_and_account_are_not_kept(self):
        blob = repr(self.statement)
        for private in ("홍길동", "아무개", "000-00-000000"):
            self.assertNotIn(private, blob)

    def test_a_dropped_row_breaks_the_balance_chain(self):
        """한 행을 못 읽으면 다음 행의 잔액이 맞지 않는다. 그것이 검증이다."""
        lines = sample().text().split("\n")
        without = [line for line in lines if "2026.01.20" not in line]
        problems, _ = ts.verify(ts.parse_text("\n".join(without)))
        self.assertTrue(any("잔액" in p for p in problems))
        self.assertTrue(any("잔고" in p for p in problems))

    def test_a_misread_amount_breaks_the_balance_chain(self):
        text = sample().text().replace("500,100", "500,000")
        problems, _ = ts.verify(ts.parse_text(text))
        self.assertTrue(problems)

    def test_unknown_cash_type_is_reported_not_swallowed(self):
        fake = sample()
        fake.won("2026.03.25", "새로운출금", -1, amount=500)
        problems, warnings = ts.verify(ts.parse_text(fake.text()))
        self.assertEqual(problems, [])
        self.assertTrue(any("새로운출금" in w for w in warnings))

    def test_changed_layout_stops_instead_of_guessing(self):
        text = sample().text().replace("   0   15   ", "   15   ", 1)
        with self.assertRaises(ts.StatementError):
            ts.parse_text(text)

    def test_text_that_is_not_a_statement_is_refused(self):
        with self.assertRaises(ts.StatementError):
            ts.parse_text("아무 글")


class TestReconciliation(TempLedgers):
    def test_missing_wrong_and_invented_rows_are_all_named(self):
        result = self.plan()["assets"]["111111"]
        self.assertEqual([(r["settle_date"], r["side"], r["quantity"]) for r in result["added"]],
                         [("2026-01-20", "BUY", "5"), ("2026-02-03", "SELL", "15")])
        self.assertEqual([r["settle_date"] for r in result["removed"]], ["2026-01-25"])
        row, diffs = result["changed"][0]
        self.assertEqual(diffs, {"fee_krw": ("0", "100")})
        self.assertEqual(result["problems"], [])

    def test_rows_outside_the_period_are_left_alone(self):
        rows = self.plan()["assets"]["111111"]["rows"]
        self.assertEqual([r["note"] for r in rows if r["settle_date"] < "2026-01-01"],
                         ["기간 전 매수", "기간 전 매도"])
        self.assertEqual(rows[-1]["note"], "기간 뒤 매수")

    def test_note_with_unquoted_commas_survives(self):
        """옛 원장은 note 의 쉼표를 따옴표 없이 적었다. 첫 쉼표에서 잘리면 안 된다."""
        rows = self.plan()["assets"]["111111"]["rows"]
        kept = next(r for r in rows if r["settle_date"] == "2026-01-10")
        self.assertIn("지정가 51,000원보다 1,000원 유리", kept["note"])
        self.assertIn("로 보정", kept["note"])

    def test_us_sell_tax_goes_to_the_note(self):
        """미국 원장에는 세금 열이 없다. 매도 제세금을 버리지 않고 note 에 남긴다."""
        added = self.plan()["assets"]["TETF"]["added"]
        self.assertEqual(len(added), 1)
        self.assertIn("제세금 14원 별도", added[0]["note"])
        self.assertEqual((added[0]["price_usd"], added[0]["fx_rate"]), ("12", "1400"))

    def test_holdings_quantity_mismatch_is_reported_but_not_fixed(self):
        before = (self.root / "holdings.csv").read_text(encoding="utf-8")
        change = self.plan()
        ts.apply(change)
        found = {m["asset_id"]: (m["statement"], m["holdings"])
                 for m in change["holdings"]["mismatches"]}
        self.assertEqual(found["111111"], (D(0), D(12)))
        self.assertNotIn("TETF", found)                       # 15 == 15
        self.assertEqual(change["holdings"]["unseen"], ["ZZZ"])   # 거래가 없으면 모른다
        self.assertEqual((self.root / "holdings.csv").read_text(encoding="utf-8"), before)

    def test_planning_writes_nothing(self):
        before = {p.name: p.read_text(encoding="utf-8") for p in self.trades.iterdir()}
        self.assertFalse(ts.in_sync(self.plan()))
        after = {p.name: p.read_text(encoding="utf-8") for p in self.trades.iterdir()}
        self.assertEqual(before, after)

    def test_held_security_without_a_ticker_is_a_problem_not_a_guess(self):
        change = self.plan(mapping={"US0000000001": "TETF"})
        self.assertTrue(any("US0000000002" in p and "--map" in p for p in change["problems"]))
        self.assertNotIn("ALPHA", change["assets"])

    def test_ledger_that_disagrees_before_the_period_is_not_touched(self):
        """기간 전 수량이 내역서 기초잔고와 다르면 이 내역서만으로는 못 고친다."""
        path = self.trades / "111111.csv"
        text = path.read_text(encoding="utf-8").replace(
            "2025-12-15,SELL,1,41000,0,82,기간 전 매도\n", "")
        path.write_text(text, encoding="utf-8")
        change = self.plan()
        self.assertTrue(any("그 이전 내역서" in p for p in change["assets"]["111111"]["problems"]))
        ts.apply(change)
        self.assertEqual(path.read_text(encoding="utf-8"), text)


class TestApply(TempLedgers):
    def setUp(self):
        super().setUp()
        self.written = ts.apply(self.plan(), today=dt.date(2026, 4, 1))

    def fold(self, asset_id, field):
        original = position_module.TRADES_DIR
        position_module.TRADES_DIR = self.trades
        try:
            return position_module.read_ledger(asset_id, field)
        finally:
            position_module.TRADES_DIR = original

    def test_ledgers_fold_to_the_statement_balance(self):
        self.assertEqual(self.fold("TETF", "price_usd")["quantity"], 15.0)
        self.assertEqual(self.fold("222222", "price_krw")["quantity"], 3.0)
        self.assertEqual(self.fold("ALPHA", "price_usd")["quantity"], 0.0001)
        # 111111 은 기간 뒤 행(1주)이 남아 있다. 내역서 잔고 0 + 그 1주다
        self.assertEqual(self.fold("111111", "price_krw")["quantity"], 1.0)

    def test_fee_and_tax_now_reach_realized_pnl(self):
        ledger = self.fold("111111", "price_krw")
        self.assertEqual(ledger["fees_paid_krw"], 330.0)
        self.assertEqual(ledger["taxes_paid_krw"], 1882.0)

    def test_closed_security_goes_to_the_closed_file(self):
        _header, fields, rows = ts.read_ledger_file(self.trades / "_closed.csv")
        self.assertEqual(fields, ts.CLOSED_FIELDS)
        self.assertEqual([(r["name"], r["side"], r["balance"]) for r in rows],
                         [("다라", "BUY", "4"), ("다라", "SELL", "0")])
        self.assertFalse((self.trades / "333333.csv").exists())

    def test_header_is_kept_and_stamped(self):
        lines = (self.trades / "111111.csv").read_text(encoding="utf-8").split("\n")
        self.assertEqual(lines[0], "# 테스트전자 (111111) 매매 내역")
        self.assertIn("[toss_statement 2026-04-01]", lines[2])
        self.assertIn("추가 2 · 삭제 1 · 값 변경 1", lines[2])

    def test_previous_files_are_backed_up(self):
        saved = list((self.root / "backup").glob("*/111111.csv"))
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0].read_text(encoding="utf-8"), OLD_KR)

    def test_second_run_finds_nothing_to_do(self):
        again = self.plan()
        self.assertEqual(ts.apply(again), [])
        results = list(again["assets"].values()) + [again["closed"]]
        self.assertFalse(any(r["added"] or r["removed"] or r["changed"] for r in results))


if __name__ == "__main__":
    unittest.main()
