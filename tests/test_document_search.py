"""보존한 원문 검색의 회귀 테스트.

사업보고서는 100만 자다. 받아놓고 못 읽으면 로컬에 있는 사실을 두고 웹을
검색하게 된다 — 2분기 실적을 DART 원문 대신 언론 보도로 인용한 실패가 그것이다.
여기서 지키는 것은 세 가지다. 표가 읽히는가, 괄호 든 검색어가 조용히 0건이
되지 않는가, 발췌가 예산 안에 머무는가.
"""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cio import documents as documents_module
from cio.documents import (
    DocumentError,
    _cached_text,
    _strip_markup,
    compile_pattern,
    documents_for_context,
    search_asset,
)

SEGMENT_TABLE = """
<TABLE><TR><TD>부 문</TD><TD>주요 제품</TD><TD>매출액</TD><TD>비중</TD></TR>
<TR><TD>DX 부문</TD><TD>TV, 스마트폰 등</TD><TD>1,879,673</TD><TD>56.3%</TD></TR>
<TR><TD>DS 부문</TD><TD>DRAM, NAND Flash 등</TD><TD>1,301,282</TD><TD>39.0%</TD></TR></TABLE>
<P>주요 매출처는 Alphabet, Apple 등이 있습니다.</P>
<P>연결재무제표기준영업(잠정)실적(공정공시)를 제출하였습니다.</P>
<SCRIPT>var hidden = "이 문자열은 검색되면 안 된다";</SCRIPT>
"""


class TestStripMarkup(unittest.TestCase):
    def test_table_cells_stay_separated(self):
        text = _strip_markup(SEGMENT_TABLE)
        # 표를 공백 하나로 이어 붙이면 어느 숫자가 어느 부문인지 알 수 없다.
        self.assertIn("DX 부문 | TV, 스마트폰 등 | 1,879,673 | 56.3%", text)

    def test_rows_are_separated_by_newline(self):
        text = _strip_markup(SEGMENT_TABLE)
        self.assertIn("56.3%", text.split("DS 부문")[0])

    def test_script_content_is_removed(self):
        self.assertNotIn("검색되면 안 된다", _strip_markup(SEGMENT_TABLE))

    def test_entities_are_decoded(self):
        self.assertIn("A&B", _strip_markup("<p>A&amp;B</p>"))

    def test_no_runaway_blank_lines(self):
        self.assertNotIn("\n\n\n", _strip_markup("<p>가</p><p></p><p></p><p>나</p>"))


class TestCompilePattern(unittest.TestCase):
    def test_valid_regex_stays_regex(self):
        compiled, literal = compile_pattern("Galaxy Z (Fold|TriFold)")
        self.assertFalse(literal)
        self.assertTrue(compiled.search("Galaxy Z TriFold 공개"))

    def test_broken_regex_falls_back_to_literal(self):
        compiled, literal = compile_pattern("영업(잠정")
        self.assertTrue(literal)
        self.assertTrue(compiled.search("영업(잠정)실적"))

    def test_fixed_forces_literal(self):
        compiled, literal = compile_pattern("a.c", fixed=True)
        self.assertTrue(literal)
        self.assertIsNone(compiled.search("abc"))

    def test_empty_pattern_is_refused(self):
        with self.assertRaises(DocumentError):
            compile_pattern("   ")


class DocumentSearchTest(unittest.TestCase):
    """임시 저장소에 원문 하나를 심고 검색한다."""

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.knowledge = root / "knowledge"
        objects = root / "data" / "raw" / "dart" / "objects"
        objects.mkdir(parents=True)
        digest = "a" * 64
        (objects / f"{digest}.json").write_text(
            json.dumps(
                {
                    "fetched_at": "2026-07-30T00:00:00+00:00",
                    "source": "opendart.fss.or.kr",
                    "payload_sha256": digest,
                    "payload": {
                        "rcept_no": "20260310002820",
                        "documents": {"main.xml": SEGMENT_TABLE},
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        filings = self.knowledge / "filings"
        filings.mkdir(parents=True)
        (filings / "005930.json").write_text(
            json.dumps(
                {
                    "schema_version": "filing-index/1.0.0",
                    "asset_id": "005930",
                    "source": "DART_LIST",
                    "filings": [
                        {
                            "accession_number": "20260310002820",
                            "filing_date": "2026-03-10",
                            "form": "ANNUAL_REPORT",
                            "description": "사업보고서 (2025.12)",
                        },
                        {
                            "accession_number": "20260515002181",
                            "filing_date": "2026-05-15",
                            "form": "QUARTER_REPORT",
                            "description": "분기보고서 (2026.03)",
                        },
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        for target, value in (
            ("ROOT", root),
            ("KNOWLEDGE_DIR", self.knowledge),
            ("DATA_DIR", root / "data"),
            ("INDEX_PATH", root / "data" / "raw" / "document-index.json"),
            ("TEXT_CACHE_DIR", root / "data" / "raw" / "text"),
            ("OBJECT_DIRS", (("DART", objects),)),
        ):
            patcher = patch.object(documents_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)


class TestSearch(DocumentSearchTest):
    def test_finds_segment_table_with_structure(self):
        result = search_asset("005930", "DS 부문", context_chars=120)
        self.assertEqual(result["total_matches"], 1)
        self.assertIn("1,301,282", result["hits"][0]["excerpt"])

    def test_parenthesised_filing_name_does_not_silently_return_zero(self):
        # '(잠정)' 은 유효한 정규식 그룹이라 오류가 나지 않는다. 그대로 두면
        # 0건이 나오고 모델은 원문에 없다고 결론짓는다.
        result = search_asset("005930", "영업(잠정)실적")
        self.assertEqual(result["total_matches"], 1)
        self.assertTrue(result["treated_as_literal"])

    def test_real_regex_is_not_downgraded(self):
        result = search_asset("005930", "D[SX] 부문")
        self.assertEqual(result["total_matches"], 2)
        self.assertFalse(result["treated_as_literal"])

    def test_no_match_reports_zero_without_falling_back_wrongly(self):
        result = search_asset("005930", "존재하지않는말")
        self.assertEqual(result["total_matches"], 0)
        self.assertEqual(result["hits"], [])

    def test_limit_caps_returned_hits_and_flags_truncation(self):
        result = search_asset("005930", "부문", max_matches=1)
        self.assertEqual(result["returned"], 1)
        self.assertTrue(result["truncated"])

    def test_excerpt_budget_is_respected(self):
        result = search_asset("005930", "부문", context_chars=10_000)
        total = sum(len(hit["excerpt"]) for hit in result["hits"])
        self.assertLessEqual(total, documents_module.MAX_TOTAL_EXCERPT_CHARS + 20_000)

    def test_unfetched_filings_are_reported(self):
        result = search_asset("005930", "부문")
        self.assertEqual(result["not_fetched_total"], 1)
        self.assertEqual(
            result["not_fetched"][0]["accession_number"], "20260515002181"
        )

    def test_unknown_doc_id_is_refused(self):
        with self.assertRaises(DocumentError):
            search_asset("005930", "부문", doc_id="DARTDOC:99999999999999")

    def test_search_can_be_scoped_to_one_document(self):
        result = search_asset("005930", "부문", doc_id="DARTDOC:20260310002820")
        self.assertEqual(result["searched_documents"], 1)


class TestTextCache(DocumentSearchTest):
    def test_second_read_uses_the_cached_copy(self):
        digest = "a" * 64
        locator = f"data/raw/dart/objects/{digest}.json"
        first = _cached_text(digest, locator)
        version = documents_module.TEXT_EXTRACT_VERSION
        cache = documents_module.TEXT_CACHE_DIR / f"{digest}.{version}.txt"
        self.assertTrue(cache.exists())
        # 원문이 사라져도 사본으로 계속 읽힌다 — 내용 주소라 사본이 곧 원문이다.
        (documents_module.ROOT / locator).unlink()
        self.assertEqual(_cached_text(digest, locator), first)

    def test_cache_identity_includes_the_extract_version(self):
        # 원문은 안 바뀌어도 추출 규칙은 바뀐다. 규칙 판을 신원에 넣지 않으면
        # 규칙을 고쳐도 낡은 사본이 계속 쓰인다.
        digest = "a" * 64
        locator = f"data/raw/dart/objects/{digest}.json"
        _cached_text(digest, locator)
        stale = documents_module.TEXT_CACHE_DIR / f"{digest}.v1.txt"
        stale.write_text("낡은 규칙으로 뽑은 사본", encoding="utf-8")
        with patch.object(documents_module, "TEXT_EXTRACT_VERSION", "v1"):
            self.assertEqual(
                _cached_text(digest, locator), "낡은 규칙으로 뽑은 사본"
            )
        self.assertNotIn("낡은 규칙", _cached_text(digest, locator))

    def test_index_is_rebuilt_when_the_extract_version_moves(self):
        documents_module.build_index()
        with patch.object(documents_module, "TEXT_EXTRACT_VERSION", "v99"):
            index = documents_module.build_index()
        self.assertEqual(index["extract_version"], "v99")


class TestEntities(unittest.TestCase):
    def test_numeric_entities_are_decoded(self):
        # SEC 문서는 &#160; 을 잔뜩 쓴다. 안 풀면 발췌가 그 쓰레기로 채워진다.
        self.assertNotIn("&#160;", _strip_markup("<td>4,087&#160;</td>"))

    def test_named_entities_are_decoded(self):
        self.assertIn("R&D", _strip_markup("<p>R&amp;D</p>"))

    def test_escaped_tag_text_survives_stripping(self):
        # 태그를 먼저 걷어내고 엔티티를 풀어야 본문에 적힌 &lt;script&gt; 가
        # 진짜 태그로 되살아나 지워지는 일이 없다.
        self.assertIn("<script>", _strip_markup("<p>&lt;script&gt;</p>"))


class TestFetchHint(unittest.TestCase):
    """받는 방법은 시장마다 다르다. 한쪽 안내를 양쪽에 주면 모델이 반드시
    실패하는 명령을 시도한다."""

    def test_korean_asset_points_at_opendart(self):
        hint = documents_module.fetch_hint({
            "asset_id": "005930", "filing_source": "DART",
        })
        self.assertIn("collectors.dart document", hint)

    def test_us_asset_points_at_snapshot_not_dart(self):
        hint = documents_module.fetch_hint(
            {"asset_id": "TSLA", "filing_source": "SEC_EDGAR"},
            {"document_url": "https://www.sec.gov/Archives/x.htm"},
        )
        self.assertIn("cio snapshot", hint)
        self.assertNotIn("collectors.dart", hint)
        self.assertIn("https://www.sec.gov/Archives/x.htm", hint)


class TestUsFilingLinkage(unittest.TestCase):
    """SEC 문서는 스냅샷으로 들어오므로 접수번호가 아니라 URL 로 맞춰야 한다.

    이걸 안 하면 이미 받은 10-Q 가 영원히 '아직 안 받음' 으로 남는다.
    """

    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        knowledge = root / "knowledge"
        objects = root / "data" / "raw" / "web" / "objects"
        objects.mkdir(parents=True)
        digest = "b" * 64
        url = "https://www.sec.gov/Archives/edgar/data/1/tsla.htm"
        (objects / f"{digest}.json").write_text(
            json.dumps({
                "fetched_at": "2026-07-30T00:00:00+00:00",
                "source": "web",
                "payload_sha256": digest,
                "payload": {
                    "final_url": url,
                    "body": "<html><title>tsla</title><body><p>Total revenues 22,496</p></body></html>",
                },
            }),
            encoding="utf-8",
        )
        (knowledge / "filings").mkdir(parents=True)
        (knowledge / "filings" / "TSLA.json").write_text(
            json.dumps({
                "schema_version": "filing-index/1.0.0",
                "asset_id": "TSLA",
                "source": "SEC_EDGAR_SUBMISSIONS",
                "filings": [
                    {
                        "accession_number": "0001628280-26-049270",
                        "filing_date": "2026-07-23",
                        "form": "10-Q",
                        "description": "10-Q",
                        "document_url": url,
                    },
                    {
                        "accession_number": "0001628280-26-049213",
                        "filing_date": "2026-07-22",
                        "form": "8-K",
                        "description": "8-K",
                        "document_url": "https://www.sec.gov/Archives/edgar/data/1/other.htm",
                    },
                ],
            }),
            encoding="utf-8",
        )
        web_dir = knowledge / "evidence" / "web"
        web_dir.mkdir(parents=True)
        (web_dir / "TSLA.json").write_text(
            json.dumps({
                "schema_version": "web-evidence/1.0.0",
                "asset_id": "TSLA",
                "snapshots": [{
                    "content_sha256": digest,
                    "label": "tsla-10q",
                    "url": url,
                    "fetched_at": "2026-07-30T09:00:00+09:00",
                }],
            }),
            encoding="utf-8",
        )
        for target, value in (
            ("ROOT", root),
            ("KNOWLEDGE_DIR", knowledge),
            ("DATA_DIR", root / "data"),
            ("INDEX_PATH", root / "data" / "raw" / "document-index.json"),
            ("TEXT_CACHE_DIR", root / "data" / "raw" / "text"),
            ("OBJECT_DIRS", (("WEB", objects),)),
        ):
            patcher = patch.object(documents_module, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_snapshotted_filing_stops_counting_as_missing(self):
        catalog = documents_module.documents_for("TSLA")
        self.assertEqual(len(catalog["available"]), 1)
        self.assertEqual(
            [item["accession_number"] for item in catalog["missing"]],
            ["0001628280-26-049213"],
        )

    def test_linked_document_carries_the_filing_identity(self):
        catalog = documents_module.documents_for("TSLA")
        entry = catalog["available"][0]
        self.assertEqual(entry["form"], "10-Q")
        self.assertEqual(entry["filing_date"], "2026-07-23")

    def test_it_is_not_listed_twice(self):
        catalog = documents_module.documents_for("TSLA")
        doc_ids = [item["doc_id"] for item in catalog["available"]]
        self.assertEqual(len(doc_ids), len(set(doc_ids)))

    def test_us_asset_search_works(self):
        result = search_asset("TSLA", "Total revenues")
        self.assertEqual(result["total_matches"], 1)
        self.assertIn("cio snapshot", result["_note"])


class TestContextBlock(DocumentSearchTest):
    def test_block_names_documents_without_carrying_their_body(self):
        block = documents_for_context("005930")
        self.assertEqual(block["searchable_total"], 1)
        self.assertEqual(block["not_fetched_total"], 1)
        serialized = json.dumps(block, ensure_ascii=False)
        self.assertNotIn("1,301,282", serialized)
        self.assertLess(len(serialized.encode("utf-8")), 2000)

    def test_block_tells_the_model_how_to_search(self):
        block = documents_for_context("005930")
        self.assertIn("cio search", block["_role"])


if __name__ == "__main__":
    unittest.main()
