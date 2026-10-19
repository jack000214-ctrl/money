"""모델이 통째로 읽는 JSON 은 내용만큼만 읽혀야 한다.

판단 한 번에 모델이 읽는 것은 SKILL·prompt·스키마 둘·packet 이다. 그중 JSON 셋이
사람용 들여쓰기로 저장돼 있었다 — 값 하나가 한 줄이고 깊이만큼 공백이 붙는다.

· 내용이 아닌 줄바꿈·공백이 packet 파일의 25%, 스키마의 45% 였다.
· **예산이 재는 것과 읽히는 것이 달랐다.** 한도 80KB 는 압축 크기로 재는데 저장된
  파일은 97~101KB 였다.
· **줄 수가 읽기 도구의 창을 넘었다.** 파마리서치 packet 은 2,183줄이었고, 파일을
  2,000줄씩 읽는 도구에서는 맨 뒤의 근거 색인과 모델 계약이 잘린 채 읽혔다.

그렇다고 한 줄로 붙이면 긴 줄을 자르는 도구(2,000자)에서 앞부분만 읽힌다. 그래서
줄 길이 상한 안에서 채워 쓴다. 여기서 지키는 성질은 둘이다 — 공백만 다르고 내용은
같다, 그리고 그 배치가 조용히 되돌아가지 않는다.
"""

import json
import shutil
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from helpers import ROOT
from calculators.contracts import (
    PACKET_LAYOUT,
    SCHEMA_FILES,
    SCHEMA_LAYOUT,
    ContractError,
    dumps_wrapped,
    reformat_schemas,
    write_json,
)
from cio import context

# 긴 줄을 자르는 읽기 도구의 한도와, 한 번에 읽는 줄 수.
READ_LINE_CHARS = 2000
READ_WINDOW_LINES = 2000

SAMPLE = {
    "schema_version": "x/1.0.0",
    "request": {"asset_id": "214450", "price": 399500.0, "claims": []},
    "context": {
        "price": {"close": 399500.0, "returns_pct": {"1m": -3.2, "3m": None}},
        "fundamentals": {
            "values": {f"metric_{i}": i * 1.5 for i in range(40)},
            "_role": "순부채는 총차입에서 현금과 단기투자를 뺀 값이다 — \"따옴표\"와 \\ 역슬래시",
        },
        "empty": {},
        "rows": [{"date": f"2026-01-{day:02d}", "close": day} for day in range(1, 29)],
    },
    "evidence_index": [],
}


class TestOnlyWhitespaceChanges(unittest.TestCase):
    def test_every_layout_reads_back_as_the_same_value(self):
        for layout in (PACKET_LAYOUT, SCHEMA_LAYOUT, {"width": 40},
                       {"width": 1, "indent": 2}, {"width": 10_000}):
            text = dumps_wrapped(SAMPLE, **layout)
            self.assertEqual(json.loads(text), SAMPLE, layout)

    def test_key_order_is_kept(self):
        """packet 의 내용 지문은 값으로 내지만, 읽는 순서도 그대로여야 한다."""
        text = dumps_wrapped(SAMPLE, **PACKET_LAYOUT)
        self.assertEqual(list(json.loads(text)), list(SAMPLE))
        self.assertEqual(json.dumps(json.loads(text)), json.dumps(SAMPLE))

    def test_scalars_and_empty_containers(self):
        for value in (None, True, 0, -1.5, "글", [], {}, [[]], {"a": {}}):
            self.assertEqual(json.loads(dumps_wrapped(value, width=5)), value)

    def test_what_cannot_be_json_is_refused_not_written(self):
        with self.assertRaises(ValueError):
            dumps_wrapped({"x": float("nan")}, width=100)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ContractError):
                write_json(Path(tmp) / "x.json", {"x": float("inf")},
                           layout=PACKET_LAYOUT)
            self.assertFalse((Path(tmp) / "x.json").exists())


class TestLinesStayWithinTheWidth(unittest.TestCase):
    def test_lines_are_filled_up_to_the_width(self):
        lines = dumps_wrapped(SAMPLE, width=120).splitlines()
        self.assertLessEqual(max(map(len, lines)), 120 + 20)
        # 값 하나가 한 줄이면 100줄이 넘는다. 채워 쓰면 그 몇 분의 일이다.
        indented = json.dumps(SAMPLE, ensure_ascii=False, indent=2).splitlines()
        self.assertLess(len(lines) * 4, len(indented))

    def test_a_string_longer_than_the_width_is_the_only_thing_that_overflows(self):
        long = "가" * 500
        lines = dumps_wrapped({"a": 1, "note": long, "b": 2}, width=100).splitlines()
        over = [line for line in lines if len(line) > 100]
        self.assertEqual(len(over), 1)
        self.assertIn(long, over[0])

    def test_a_value_that_fits_stays_on_one_line(self):
        self.assertEqual(dumps_wrapped({"a": [1, 2], "b": {"c": 3}}, width=100),
                         '{"a":[1,2],"b":{"c":3}}\n')


class TestSectionsStartAtTheLineHead(unittest.TestCase):
    """packet 은 최상위 키와 context 의 섹션이 언제나 줄 머리에서 시작한다.

    그래야 한 섹션만 찾아 읽을 수 있다(`grep -n '^"fundamentals"'`).
    """

    def setUp(self):
        big = {**SAMPLE, "context": {
            **SAMPLE["context"],
            "filler": {f"k{i}": "값" * 30 for i in range(60)},
        }}
        self.data = big
        self.lines = dumps_wrapped(big, **PACKET_LAYOUT).splitlines()

    def heads(self):
        return {line.split('"')[1] for line in self.lines if line.startswith('"')}

    def test_top_level_keys(self):
        self.assertTrue(set(self.data) <= self.heads())

    def test_context_sections(self):
        self.assertTrue(set(self.data["context"]) <= self.heads())

    def test_deeper_values_are_still_packed(self):
        row_lines = [line for line in self.lines if '"date":"2026-01-' in line]
        self.assertLess(len(row_lines), 28)


class TestIndentedLayoutForHumanEditedFiles(unittest.TestCase):
    def test_structure_is_visible(self):
        text = dumps_wrapped(
            {"type": "object", "properties": {f"p{i}": {"type": "string"}
                                              for i in range(12)}},
            width=60, indent=1)
        lines = text.splitlines()
        self.assertEqual((lines[0], lines[-1]), ("{", "}"))
        self.assertTrue(all(line.startswith(" ") for line in lines[1:-1]))
        self.assertIn(' "properties":{', lines)


class TestSchemasStayInTheirLayout(unittest.TestCase):
    def test_schema_files_are_in_the_canonical_layout(self):
        """편집기가 스키마를 다시 들여쓰면 판단마다 읽는 분량이 조용히 두 배가 된다."""
        for name in SCHEMA_FILES:
            text = (ROOT / name).read_text(encoding="utf-8")
            self.assertEqual(
                text, dumps_wrapped(json.loads(text), **SCHEMA_LAYOUT),
                f"{name} 의 배치가 달라졌다. 내용은 그대로 두고 배치만 맞춘다: "
                'python -c "from calculators.contracts import reformat_schemas; '
                'reformat_schemas()"',
            )

    def test_schemas_fit_one_read(self):
        for name in SCHEMA_FILES:
            lines = (ROOT / name).read_text(encoding="utf-8").splitlines()
            self.assertLess(len(lines), 600, name)
            self.assertLess(max(map(len, lines)), READ_LINE_CHARS, name)

    def test_reformatting_changes_layout_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "schemas").mkdir()
            originals = {}
            for name in SCHEMA_FILES:
                data = json.loads((ROOT / name).read_text(encoding="utf-8"))
                originals[name] = data
                (root / name).write_text(
                    json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            changed = reformat_schemas(root)
            self.assertEqual(len(changed), len(SCHEMA_FILES))
            for name in SCHEMA_FILES:
                self.assertEqual(
                    json.loads((root / name).read_text(encoding="utf-8")),
                    originals[name])
            self.assertEqual(reformat_schemas(root), [])


class TestTheSavedPacketIsWhatTheBudgetMeasured(unittest.TestCase):
    """헬퍼가 맞아도 저장 배선이 들여쓰기로 돌아가면 소용없다."""

    def saved_packet(self):
        price = {"currency": "USD", "close": 100, "as_of": "2026-10-12",
                 "last_bar": {"is_settled_close": True}}
        # 섹션 하나를 크게 채워 실제 packet 크기에 가깝게 만든다.
        optional = {"filler": {f"row_{i}": {"value": i, "note": "값" * 40}
                               for i in range(350)}}
        fixed = {
            "_price_summary": (price, None), "_session_gaps": None,
            "_latest_fundamentals": ({"period_end": "2026-06-30", "values": {}}, None),
            "_filing_summary": ({"latest_primary_reports": [],
                                 "latest_material_events": []}, None),
            "_earnings_calendar_section": (None, None),
            "_latest_calculation": (None, None),
            "_market_summary": ({"input_sources": []}, None),
            "_event_summary": (None, None), "_etf_summary": (None, None),
            "inspect_plan": [], "_optional_collection_summaries": (optional, []),
            "_us_automated_summaries": ({}, []),
            "_portfolio_summary": ({"available": False}, None),
            "_attach_position_math": None, "load_policy": (None, []),
            "position_multiples": None, "_memory_digest": {}, "_daily_ranges": [],
            "open_requests_for_context": [], "documents_for_context": None,
        }
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp).resolve()
            for relative in ("prompts/cio-system.md",
                             "schemas/cio-decision.schema.json"):
                (root / relative).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / relative, root / relative)
            for name, value in fixed.items():
                stack.enter_context(patch.object(context, name, return_value=value))
            stack.enter_context(patch("cio.capital.section", return_value=(None, [])))
            stack.enter_context(patch.object(context, "ROOT", root))
            stack.enter_context(
                patch.object(context, "KNOWLEDGE_DIR", root / "knowledge"))
            packet, path = context.build_context(
                "TSLA", price=100, amount=1000, amount_currency="USD", save=True)
            return packet, path.read_text(encoding="utf-8")  # pyright: ignore[reportOptionalMemberAccess]

    def test_the_file_holds_exactly_the_packet(self):
        packet, text = self.saved_packet()
        self.assertEqual(json.loads(text), packet)

    def test_the_file_is_the_size_the_budget_measured(self):
        packet, text = self.saved_packet()
        compact = context._compact_size(packet)
        self.assertGreater(compact, 40_000)
        self.assertLess(len(text.encode("utf-8")), compact * 1.01)

    def test_the_file_fits_one_read(self):
        _packet, text = self.saved_packet()
        lines = text.splitlines()
        self.assertLess(len(lines), READ_WINDOW_LINES // 4)
        self.assertLess(max(map(len, lines)), READ_LINE_CHARS)

    def test_other_artifacts_stay_human_readable(self):
        """판단·평가처럼 사람이 열어 보는 산출물은 그대로 들여쓴다."""
        with tempfile.TemporaryDirectory() as tmp:
            path = write_json(Path(tmp) / "a.json", {"a": {"b": 1}})
            self.assertEqual(path.read_text(encoding="utf-8"),
                             '{\n  "a": {\n    "b": 1\n  }\n}\n')


if __name__ == "__main__":
    unittest.main()
