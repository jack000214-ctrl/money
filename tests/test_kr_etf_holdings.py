"""운용사 원문에서 ETF 구성종목을 받아 적는 자리.

구성종목은 ETF 의 **필수** 입력이다. 예전에는 수집 명령이 없어 모델이 운용사
표를 손으로 옮겼고, 그 과정에서 세 가지가 조용히 틀릴 수 있었다.

  1. **표가 잘려 온다.** 운용사 주소의 기본 행수는 10이다. 상위 10종목만 담긴
     파일은 형식이 완전히 정상이라 전체처럼 읽힌다.
  2. **종목코드 표기가 운용사식이다.** `300124 C2 EQUITY` 를 야후 심볼로 바꿀 때
     접미사를 잘못 붙이면 다른 종목의 가격과 배수가 끌려온다.
  3. **증권이 아닌 행이 섞여 있다.** 원화예금은 비중이 음수로 오기도 한다.

네트워크는 타지 않는다. 받은 원문을 어떻게 읽는지만 본다.
"""

import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from collectors import kr_etf_holdings as holdings
from cio import refresh


def row(code, name, shares, value, weight, total=3):
    return f"""
<tr data-tot-cnt="{total}">
  <td>
      {code}</td>
  <td>{name}</td>
  <td>
      {shares}</td>
  <td>{value}</td>
  <td>
      {weight}</td>
  <td><div class="color-none"><span class="blind">없음</span>-</div></td>
</tr>"""


LISTING = (
    row("2432 HK EQUITY", "Shenzhen Dobot Corp Ltd", "12,293", "43,924,560", "10.21")
    + row("300124 C2 EQUITY", "Shenzhen Inovance Technology Co Ltd", "3,305", "34,281,885", "7.97")
    + row("KRD010010001", "원화예금", "-692,298", "-692,298", "-0.16")
)

PRODUCT_PAGE = """
<div class="pop-header-info">
    기준일 2026.10.08 16:55:34</div>
<div class="title">기준가격(NAV)</div> 도움말 팝업닫기
<div class="desc">
    <span class="amount">8,251.92</span>
    <span class="unit">원</span>
</div>
<div class="c-card-header">총보수</div>
<div class="c-card-content">
    <p>연 0.49&#37; </p>
    운용: 0.439&#37;<br/> 지정참가: 0.001&#37;</div>
<div class="c-card-header">환헤지 사항</div>
<div class="c-card-content">
    <td><span class="txt-bold-type01">환헤지를 하지 아니함</span></td>
</div>
"""

ASSET = {
    "asset_id": "0053L0", "name": "TIGER 테스트", "asset_type": "ETF",
    "currency": "KRW",
    "etf": {"issuer": "MIRAE_TIGER", "isin": "KR70053L0005"},
}


class TestReadingTheIssuerTable(unittest.TestCase):
    def test_every_row_and_the_declared_count_are_read(self):
        rows, declared = holdings.parse_holdings_rows(LISTING)
        self.assertEqual(declared, 3)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0]["issuer_code"], "2432 HK EQUITY")
        self.assertEqual(rows[0]["shares_per_cu"], 12293)
        self.assertEqual(rows[0]["weight_pct"], 10.21)

    def test_a_negative_cash_weight_is_read_as_negative(self):
        """부호를 떼면 현금 -0.16% 가 +0.16% 가 되어 합계가 틀린다."""
        rows, _ = holdings.parse_holdings_rows(LISTING)
        self.assertEqual(rows[2]["weight_pct"], -0.16)

    def test_a_table_without_rows_declares_nothing(self):
        rows, declared = holdings.parse_holdings_rows("<div>검색 결과가 없습니다.</div>")
        self.assertEqual((rows, declared), ([], None))


class TestIssuerCodeNormalization(unittest.TestCase):
    """아는 표기만 바꾼다. 모르는 표기에 접미사를 추측해 붙이지 않는다."""

    def normalized(self, code, name="Some Co"):
        return holdings.normalize_holding(code, name)

    def test_hong_kong(self):
        self.assertEqual(self.normalized("2432 HK EQUITY"),
                         {"kind": "SECURITY", "holding_id": "2432.HK", "market": "HK"})

    def test_shenzhen_connect_is_not_shanghai(self):
        """C1 과 C2 를 섞으면 같은 6자리 코드의 다른 거래소 종목이 된다."""
        self.assertEqual(self.normalized("300124 C2 EQUITY")["holding_id"], "300124.SZ")
        self.assertEqual(self.normalized("603501 C1 EQUITY")["holding_id"], "603501.SS")

    def test_a_korean_short_code_stays_as_it_is(self):
        self.assertEqual(self.normalized("005930"),
                         {"kind": "SECURITY", "holding_id": "005930", "market": "KR"})

    def test_us_listing(self):
        self.assertEqual(self.normalized("TSLA UW EQUITY"),
                         {"kind": "SECURITY", "holding_id": "TSLA", "market": "US"})

    def test_cash_is_recognized_by_code_or_name(self):
        self.assertEqual(self.normalized("KRD010010001", "원화예금")["kind"], "CASH")
        self.assertEqual(self.normalized("XYZ", "설정현금액")["kind"], "CASH")

    def test_an_unknown_exchange_keeps_the_code_and_leaves_the_market_blank(self):
        result = self.normalized("ABC ZZ EQUITY")
        self.assertEqual(result["market"], "")
        self.assertEqual(result["holding_id"], "ABC_ZZ_EQUITY")


class TestBuildingTheHoldings(unittest.TestCase):
    def build(self, listing=LISTING):
        rows, _ = holdings.parse_holdings_rows(listing)
        return holdings.build_holdings("0053L0", rows, as_of="2026-10-08")

    def test_cash_is_left_out_and_reported(self):
        kept, excluded, errors = self.build()
        self.assertEqual(errors, [])
        self.assertEqual([item["holding_id"] for item in kept], ["2432.HK", "300124.SZ"])
        self.assertEqual(excluded[0]["holding_name"], "원화예금")
        self.assertEqual(excluded[0]["reason"], "CASH")

    def test_a_security_with_a_negative_weight_is_an_error(self):
        """현금이 아닌데 음수면 표를 잘못 읽은 것이다. 조용히 빼지 않는다."""
        listing = row("2432 HK EQUITY", "Dobot", "1", "1", "-1.5", total=1)
        _, _, errors = self.build(listing)
        self.assertTrue(any("음수" in error for error in errors))

    def test_duplicate_holdings_are_an_error(self):
        listing = (row("2432 HK EQUITY", "Dobot", "1", "1", "5", total=2)
                   + row("2432 HK EQUITY", "Dobot", "1", "1", "5", total=2))
        _, _, errors = self.build(listing)
        self.assertTrue(any("중복" in error for error in errors))

    def test_rows_are_ordered_by_weight(self):
        listing = (row("300124 C2 EQUITY", "A", "1", "1", "3", total=2)
                   + row("2432 HK EQUITY", "B", "1", "1", "9", total=2))
        kept, _, _ = self.build(listing)
        self.assertEqual([item["weight_pct"] for item in kept], [9, 3])


class TestReadingTheProductPage(unittest.TestCase):
    def test_base_date_nav_fee_and_hedge_statement(self):
        facts = holdings.parse_product_page(PRODUCT_PAGE)
        self.assertEqual(facts["as_of"], "2026-10-08")
        self.assertEqual(facts["nav"], 8251.92)
        self.assertEqual(facts["total_fee_pct"], 0.49)
        self.assertEqual(facts["hedge_statement"], "환헤지를 하지 아니함")

    def test_what_cannot_be_read_is_null_not_a_guess(self):
        facts = holdings.parse_product_page("<html>점검 중입니다</html>")
        self.assertEqual(set(facts.values()), {None})


class Collecting(unittest.TestCase):
    """`collect` 를 네트워크 없이 돌린다. 원문 두 개만 바꿔 끼운다."""

    def run_collect(self, *, listing=LISTING, product=PRODUCT_PAGE, asset=ASSET):
        pages = iter([({"body": product}, "WEBSNAP:product00000"),
                      ({"body": listing}, "WEBSNAP:holdings0000")])
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        with patch.object(holdings, "get_asset", return_value=asset), \
                patch.object(holdings, "_snapshot", lambda *a, **k: next(pages)), \
                patch.object(holdings, "ETF_DIR", Path(self.tmp.name)), \
                patch.object(holdings, "ROOT", Path(self.tmp.name)):
            return holdings.collect("0053L0")

    def failure(self, **kwargs):
        captured = io.StringIO()
        with redirect_stderr(captured), self.assertRaises(SystemExit):
            self.run_collect(**kwargs)
        return captured.getvalue()


class TestCollectRefusesWhatItCannotTrust(Collecting):
    def test_a_truncated_table_is_not_saved(self):
        """운용사가 3행이라 했는데 2행만 왔으면 일부만 적지 않는다."""
        truncated = (row("2432 HK EQUITY", "Dobot", "1", "1", "10", total=3)
                     + row("300124 C2 EQUITY", "Inovance", "1", "1", "8", total=3))
        message = self.failure(listing=truncated)
        self.assertIn("잘렸다", message)
        self.assertEqual(list(Path(self.tmp.name).rglob("holdings-*.csv")), [])

    def test_a_missing_base_date_is_not_replaced_by_today(self):
        message = self.failure(product="<html>기준일 없음</html>")
        self.assertIn("조회일로 대신하지 않는다", message)

    def test_an_unsupported_issuer_says_which_are_supported(self):
        asset = {**ASSET, "etf": {"issuer": "SOMEONE_ELSE", "isin": "KR70053L0005"}}
        message = self.failure(asset=asset)
        self.assertIn("MIRAE_TIGER", message)

    def test_a_malformed_isin_is_refused_before_any_request(self):
        asset = {**ASSET, "etf": {"issuer": "MIRAE_TIGER", "isin": "0053L0"}}
        self.assertIn("etf.isin", self.failure(asset=asset))


class TestWhatCollectWrites(Collecting):
    def setUp(self):
        self.facts = self.run_collect()
        self.path = next(Path(self.tmp.name).rglob("holdings-2026-10-08.csv"))
        self.text = self.path.read_text(encoding="utf-8")

    def test_the_file_is_named_by_the_issuer_base_date(self):
        self.assertEqual(self.path.name, "holdings-2026-10-08.csv")
        self.assertEqual(self.facts["as_of"], "2026-10-08")

    def test_the_excluded_cash_row_is_written_down(self):
        self.assertIn("제외한 행 KRD010010001 원화예금", self.text)
        self.assertEqual(self.facts["excluded_rows"][0]["weight_pct"], -0.16)

    def test_the_sources_are_recorded(self):
        self.assertIn("WEBSNAP:holdings0000", self.text)
        self.assertEqual(self.facts["sources"]["product_page"], "WEBSNAP:product00000")

    def test_refresh_reads_the_base_date_and_the_retrieval_time(self):
        """머리말 형식이 `refresh` 가 읽는 형식과 어긋나면 신선도를 못 잰다."""
        self.assertEqual(refresh._data_as_of(self.path), "2026-10-08")
        self.assertIsNotNone(refresh._holdings_retrieved_at(self.path))

    def test_the_file_passes_the_holdings_contract(self):
        task = refresh.RefreshTask(
            name="holdings:0053L0", capability="ETF_HOLDINGS",
            artifact="unused", max_age_hours=168, command=None)
        self.assertIsNone(refresh._artifact_contract_error(task, self.path))


class TestHoldingsFreshness(unittest.TestCase):
    """자동 수집본은 조회시각으로, 손으로 적은 파일은 기준일로 신선도를 잰다."""

    HEADER = "etf_id,holding_id,holding_name,weight_pct,market,sector,as_of\n"

    def write(self, text):
        """구성 파일은 이름이 `holdings-<행의 기준일>.csv` 다."""
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        as_of = text.strip().rsplit(",", 1)[-1]
        path = Path(directory.name) / f"holdings-{as_of}.csv"
        path.write_text(text, encoding="utf-8")
        return path

    def task(self):
        return refresh.RefreshTask(
            name="holdings:0053L0", capability="ETF_HOLDINGS",
            artifact="unused", max_age_hours=168, command=None)

    def test_a_hand_written_file_has_no_retrieval_time(self):
        path = self.write("# 조회일: 2026-10-09 / 데이터 기준일: 2026-10-08\n"
                          + self.HEADER + "0053L0,2432.HK,Dobot,10,HK,,2026-10-08\n")
        self.assertIsNone(refresh._holdings_retrieved_at(path))

    def test_an_issuer_that_stopped_updating_is_caught(self):
        """조회만 하면 계속 신선해 보이는 것을 막는다."""
        path = self.write(
            "# retrieved_at 2026-10-09T00:00:00+00:00 / source MIRAE_TIGER\n"
            + self.HEADER + "0053L0,2432.HK,Dobot,10,HK,,2026-09-01\n")
        error = refresh._artifact_contract_error(self.task(), path)
        self.assertIn("뒤처졌다", error)  # pyright: ignore[reportArgumentType]

    def test_a_long_holiday_is_not_mistaken_for_a_stale_issuer(self):
        path = self.write(
            "# retrieved_at 2026-10-10T00:00:00+00:00 / source MIRAE_TIGER\n"
            + self.HEADER + "0053L0,2432.HK,Dobot,10,HK,,2026-10-02\n")
        self.assertIsNone(refresh._artifact_contract_error(self.task(), path))


if __name__ == "__main__":
    unittest.main()
