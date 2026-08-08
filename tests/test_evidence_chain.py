"""판단이 인용한 근거가 지금도 그 자리에 그대로 있는가.

`validate_context_integrity` 는 이 저장소에서 **근거 사슬을 지키는 유일한 검사**
다. packet 이 저장된 뒤에 원문이 바뀌거나 사라져도, 판단 파일은 여전히 그
context_id 를 인용한다. 검사가 없으면 인용은 성립하는데 내용은 다른 상태가
되고, 그 상태는 파일을 열어봐도 정상으로 보인다.

그런데 이 함수의 규칙 14 개가 테스트에서 한 번도 터지지 않았다. 실측으로 지금
저장소에는 끊긴 참조가 0 건이지만, **0 건이라는 사실과 0 건인지 확인할 수 있다는
사실은 다르다** — 검사가 조용히 망가져 있어도 결과는 똑같이 0 건이다.

여기서 고정하는 것은 셋이다.
  1. 내용이 바뀌면 지문이 달라진다 (packet 도, 근거 파일도)
  2. 가리키는 곳이 없으면 없다고 말한다
  3. 저장소 밖은 근거가 아니다
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import content_hash
from cio.decision import context_id_for, validate_context_integrity


class ChainCase(unittest.TestCase):
    """임시 저장소를 만들고 그 안에서만 근거를 해석한다."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        (self.root / "prompts").mkdir()
        (self.root / "schemas").mkdir()
        self.prompt = self.root / "prompts" / "cio-system.md"
        self.schema = self.root / "schemas" / "cio-decision.schema.json"
        self.prompt.write_text("프롬프트", encoding="utf-8")
        self.schema.write_text("{}", encoding="utf-8")
        self.addCleanup(self._tmp.cleanup)

    def evidence(self, name="price.csv", body="date,close\n2026-08-08,1422000\n"):
        path = self.root / name
        path.write_text(body, encoding="utf-8")
        return {"source_id": f"PRICE:{name}", "kind": "MARKET_DATA",
                "path": name, "content_sha256": content_hash(path)}

    def packet(self, *, evidence_index=None, model_contract=None, **overrides):
        data = {
            "schema_version": "cio-context/1.0.0",
            "asset": {"asset_id": "000660"},
            "evidence_index": (self.evidence(),) if evidence_index is None
                              else evidence_index,
            "model_contract": model_contract if model_contract is not None else {
                "prompt_snapshot": "prompts/cio-system.md",
                "prompt_sha256": content_hash(self.prompt),
                "decision_schema_snapshot": "schemas/cio-decision.schema.json",
                "decision_schema_sha256": content_hash(self.schema),
            },
        }
        data["evidence_index"] = list(data["evidence_index"])
        data.update(overrides)
        data["context_id"] = context_id_for(data)
        return data

    def check(self, data, **kwargs):
        with patch("cio.decision.ROOT", self.root):
            return validate_context_integrity(data, **kwargs)

    def assertClean(self, data, **kwargs):
        self.assertEqual(self.check(data, **kwargs), [])

    def assertFlags(self, data, needle, **kwargs):
        errors = self.check(data, **kwargs)
        self.assertTrue(
            any(needle in error for error in errors),
            f"{needle!r} 가 없다 — 실제: {errors}",
        )


class TestAValidPacketPasses(ChainCase):
    def test_an_untouched_packet_has_no_errors(self):
        self.assertClean(self.packet())

    def test_the_expected_id_matches(self):
        data = self.packet()
        self.assertClean(data, expected_id=data["context_id"])


class TestThePacketProvesItself(ChainCase):
    """packet 내용이 바뀌면 지문이 달라진다."""

    def test_an_edited_packet_is_caught(self):
        """저장된 뒤 누가 값을 고치면 인용은 성립하는데 내용이 다르다."""
        data = self.packet()
        data["asset"]["asset_id"] = "005930"
        self.assertFlags(data, "내용 지문은")

    def test_a_malformed_id_stops_everything_else(self):
        data = self.packet()
        data["context_id"] = "CTX-짧음"
        self.assertEqual(self.check(data),
                         ["context_id가 CTX-<20자리 16진수> 형식이 아니다"])

    def test_asking_for_a_different_id_is_refused(self):
        data = self.packet()
        self.assertFlags(data, "요청값 또는 파일명과 다르다",
                         expected_id="CTX-" + "0" * 20)

    def test_an_unknown_schema_is_refused(self):
        self.assertFlags(self.packet(schema_version="cio-context/9.9.9"),
                         "지원하지 않는 context schema")


class TestEvidenceMustResolve(ChainCase):
    """가리키는 곳에 그 파일이 그대로 있는가."""

    def test_a_deleted_evidence_file_is_reported(self):
        data = self.packet()
        (self.root / "price.csv").unlink()
        self.assertFlags(data, "파일이 없다")

    def test_an_evidence_file_edited_after_the_fact_is_caught(self):
        """내용 주소 저장의 요점이다 — 근거가 조용히 바뀌면 안 된다."""
        data = self.packet()
        (self.root / "price.csv").write_text("date,close\n2026-08-08,1\n",
                                             encoding="utf-8")
        self.assertFlags(data, "파일 지문이 달라졌다")

    def test_a_path_outside_the_repository_is_not_evidence(self):
        item = self.evidence()
        item["path"] = "../../etc/passwd"
        self.assertFlags(self.packet(evidence_index=[item]), "저장소 밖")

    def test_a_malformed_digest_is_refused(self):
        item = self.evidence()
        item["content_sha256"] = "not-a-sha"
        self.assertFlags(self.packet(evidence_index=[item]),
                         "content_sha256 형식이 잘못됐다")

    def test_an_empty_path_with_a_digest_is_refused(self):
        item = self.evidence()
        item["path"] = ""
        self.assertFlags(self.packet(evidence_index=[item]), "path가 비었다")

    def test_an_entry_with_neither_path_nor_digest_is_allowed(self):
        """경로 없는 출처는 '직접 조사'처럼 파일이 없는 근거다."""
        self.assertClean(self.packet(evidence_index=[
            {"source_id": "USER_RESEARCH:1", "kind": "USER_DATA"}
        ]))


class TestSourceIdsIdentifyOneThing(ChainCase):
    def test_a_duplicate_source_id_is_reported(self):
        """같은 이름이 두 파일을 가리키면 인용이 어느 쪽인지 알 수 없다."""
        first = self.evidence("a.csv", "1")
        second = self.evidence("b.csv", "2")
        second["source_id"] = first["source_id"]
        self.assertFlags(self.packet(evidence_index=[first, second]),
                         "source_id가 중복됐다")

    def test_an_empty_source_id_is_reported(self):
        item = self.evidence()
        item["source_id"] = ""
        self.assertFlags(self.packet(evidence_index=[item]),
                         "source_id가 비었다")

    def test_a_non_object_entry_is_reported(self):
        self.assertFlags(self.packet(evidence_index=["price.csv"]),
                         "객체가 아니다")


class TestTheModelContractIsPinned(ChainCase):
    """어떤 프롬프트·스키마로 낸 판단인지가 남아 있는가."""

    def test_an_edited_prompt_is_caught(self):
        """프롬프트가 바뀌면 그 판단은 다른 규칙 아래 나온 것이다."""
        data = self.packet()
        self.prompt.write_text("바뀐 프롬프트", encoding="utf-8")
        self.assertFlags(data, "prompt_snapshot 파일이 없거나 지문이 다르다")

    def test_an_edited_schema_is_caught(self):
        data = self.packet()
        self.schema.write_text('{"changed": true}', encoding="utf-8")
        self.assertFlags(data,
                         "decision_schema_snapshot 파일이 없거나 지문이 다르다")

    def test_a_contract_path_outside_the_repository_is_refused(self):
        self.assertFlags(self.packet(model_contract={
            "prompt_snapshot": "../../../etc/passwd",
            "prompt_sha256": "0" * 64,
        }), "prompt_snapshot가 저장소 밖")

    def test_a_missing_contract_entry_is_skipped_not_invented(self):
        """옛 packet 에는 없던 칸이다. 없는 것과 틀린 것은 다르다."""
        self.assertClean(self.packet(model_contract={}))


if __name__ == "__main__":
    unittest.main()
