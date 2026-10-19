"""CLI 계약 회귀 테스트.

`cio/__main__.py` 는 264 문장짜리 사용자 진입점인데 테스트가 한 줄도 없었다.
여기서 지키는 것은 화면 문구가 아니라 **종료코드**다.

종료코드는 이 프로그램의 유일한 기계 판독 출력이다. `prepare` 가 BLOCKED 인데
0 을 내면 호출한 쪽은 packet 이 쓸 만하다고 읽고, `policy` 가 '한도 없음'과
'계약 위반'을 같은 값으로 내면 둘을 구분해 행동할 수 없다. 화면 문구는 바뀌어도
되지만 이 숫자들은 계약이다.

| 명령 | 0 | 1 | 2 | 3 | 4 |
|---|---|---|---|---|---|
| prepare | PASS | | BLOCKED | | |
| search | 일치 있음 | 일치 없음 | | | |
| policy | 전부 정함 | | 계약 위반 | 미정 한도 있음 | 정책 파일 없음 |
| score | 전부 채점 | | | 건너뛴 판단 있음 | |
| pipeline·retire | 성공 | | 입력 오류 | | |
"""

import io
import unittest
from collections import namedtuple
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)
from calculators.contracts import ContractError
from cio import __main__ as cli
from cio.assets import AssetRegistryError
from cio.context import INTENTS
from cio.documents import DocumentError
from cio.pipeline import PipelineError
from cio.retire import RetireError, retire as retire_judgments


def run(argv):
    """argv 를 실제 파서로 통과시켜 핸들러까지 돌린다. (종료코드, stdout)."""
    args = cli.build_parser().parse_args(argv)
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = args.func(args)
    return code, out.getvalue()


class TestParserWiring(unittest.TestCase):
    def test_every_subcommand_reaches_a_handler(self):
        """set_defaults(func=...) 를 빠뜨리면 그 명령은 호출 즉시 죽는다."""
        subcommands = cli.build_parser()._subparsers._group_actions[0].choices  # pyright: ignore[reportOptionalMemberAccess]
        self.assertTrue(subcommands)
        for name, subparser in subcommands.items():  # pyright: ignore[reportAttributeAccessIssue, reportOptionalMemberAccess]
            with self.subTest(name):
                self.assertTrue(
                    callable(subparser.get_default("func")),
                    f"{name} 에 핸들러가 없다",
                )

    def test_command_is_required(self):
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args([])

    def test_prepare_requires_a_price(self):
        """가격 없이 packet 을 만들면 '얼마에 살까'가 없는 질문이 된다."""
        with self.assertRaises(SystemExit):
            with redirect_stderr(io.StringIO()):
                cli.build_parser().parse_args(["prepare", "TSLA"])

    def test_prepare_intent_choices_track_the_context_module(self):
        """INTENTS 에 갈래를 더하고 CLI 를 안 고치면 그 갈래를 물을 수 없다."""
        parser = cli.build_parser()
        action = next(
            a for a in parser._subparsers._group_actions[0]  # pyright: ignore[reportIndexIssue, reportOptionalMemberAccess, reportOptionalSubscript]
            .choices["prepare"]._actions
            if a.dest == "intent"
        )
        self.assertEqual(set(action.choices), set(INTENTS))

    def test_register_requires_the_routing_fields(self):
        """시장·상품·렌즈 없이 등록되면 그 자산은 라우팅될 수 없다."""
        for missing in ("--market", "--asset-type", "--analysis-lens"):
            argv = [
                "register", "000000", "--name", "테스트",
                "--market", "KR", "--exchange", "KOSPI",
                "--asset-type", "STOCK", "--price-symbol", "000000.KS",
                "--sector-code", "TECH", "--analysis-lens", "LENS",
                "--valuation-family", "DCF", "--benchmark", "^KS11",
            ]
            index = argv.index(missing)
            trimmed = argv[:index] + argv[index + 2:]
            with self.subTest(missing), self.assertRaises(SystemExit):
                with redirect_stderr(io.StringIO()):
                    cli.build_parser().parse_args(trimmed)


class TestPrepareExitCode(unittest.TestCase):
    def packet(self, status):
        return {
            "context_id": "CTX-" + "a" * 20,
            "validation": {"status": status, "problems": ["입력이 낡았다"]},
        }

    def test_pass_exits_zero(self):
        with patch.object(cli, "build_context",
                          return_value=(self.packet("PASS"), "some/path.json")):
            code, out = run(["prepare", "TSLA", "--price", "100"])
        self.assertEqual(code, 0)
        self.assertIn("compact_size_bytes", out)

    def test_blocked_exits_two_even_though_the_packet_was_written(self):
        """파일이 저장됐다는 것과 쓸 만하다는 것은 다른 사실이다."""
        with patch.object(cli, "build_context",
                          return_value=(self.packet("BLOCKED"), "some/path.json")):
            code, out = run(["prepare", "TSLA", "--price", "100"])
        self.assertEqual(code, 2)
        self.assertIn("입력이 낡았다", out)


class TestSearchExitCode(unittest.TestCase):
    def test_matches_exit_zero_and_no_match_exits_one(self):
        """0건을 성공으로 내면 스크립트가 '찾았다'로 읽는다."""
        for total, expected in ((3, 0), (0, 1)):
            with self.subTest(total=total):
                result = {"total_matches": total}
                with patch.object(cli, "search_asset", return_value=result), \
                        patch.object(cli, "format_search", return_value=""):
                    code, _ = run(["search", "TSLA", "패턴"])
                self.assertEqual(code, expected)


class TestPolicyExitCodes(unittest.TestCase):
    """네 상태가 서로 다른 숫자여야 한다 — 합치면 행동을 구분할 수 없다."""

    def policy(self):
        return {"policy_version": "2026-08-01", "base_currency": "KRW"}

    def test_absent_policy_exits_four(self):
        with patch.object(cli, "load_policy", return_value=(None, [])):
            code, out = run(["policy"])
        self.assertEqual(code, 4)
        self.assertIn("투자정책이 없다", out)

    def test_invalid_policy_exits_two(self):
        with patch.object(cli, "load_policy", return_value=(None, ["계약 미달"])):
            code, _ = run(["policy"])
        self.assertEqual(code, 2)

    def test_unset_limits_exit_three(self):
        unset = ["horizon.typical_holding_months"]
        with patch.object(cli, "load_policy", return_value=(self.policy(), [])), \
                patch.object(cli, "unset_fields", return_value=unset), \
                patch.object(cli, "deliberate_gaps", return_value={}):
            code, out = run(["policy"])
        self.assertEqual(code, 3)
        self.assertIn("horizon.typical_holding_months", out)

    def test_fully_decided_policy_exits_zero(self):
        with patch.object(cli, "load_policy", return_value=(self.policy(), [])), \
                patch.object(cli, "unset_fields", return_value=[]), \
                patch.object(cli, "deliberate_gaps", return_value={}):
            code, _ = run(["policy"])
        self.assertEqual(code, 0)

    def test_deliberate_gap_is_not_counted_as_unset(self):
        """'두지 않기로 했다'가 '아직 모른다'로 집계되면 이미 답한 질문을 되묻는다."""
        with patch.object(cli, "load_policy", return_value=(self.policy(), [])), \
                patch.object(cli, "unset_fields", return_value=[]), \
                patch.object(cli, "deliberate_gaps",
                             return_value={"risk.max_loss": "두지 않기로 함"}):
            code, out = run(["policy"])
        self.assertEqual(code, 0)
        self.assertIn("일부러 두지 않음", out)


class TestScoreExitCode(unittest.TestCase):
    def test_skipped_assessments_exit_three(self):
        """건너뛴 판단을 조용히 성공으로 내면 track record 에 구멍이 생긴다."""
        for skipped, expected in (([], 0), ([{"id": "x"}], 3)):
            with self.subTest(skipped=bool(skipped)):
                with patch.object(cli, "score_asset",
                                  return_value={"skipped": skipped}):
                    code, _ = run(["score", "TSLA"])
                self.assertEqual(code, expected)


class TestErrorPathsExitTwo(unittest.TestCase):
    def test_pipeline_input_error_exits_two(self):
        with patch.object(cli, "record_program",
                          side_effect=PipelineError("출처가 없다")):
            code, _ = run(["pipeline", "RXRX", "--name", "REC-1"])
        self.assertEqual(code, 2)

    def test_pipeline_lookup_of_an_unregistered_asset_is_an_error(self):
        """오타 난 자산에 '기록된 파이프라인이 없다'를 exit 0 으로 답했다."""
        with self.assertRaises(AssetRegistryError):
            run(["pipeline", "NOPE"])

    def test_retire_error_exits_two(self):
        with patch.object(cli, "retire", side_effect=RetireError("대상이 없다")):
            code, _ = run(["retire", "TSLA", "--reason", "구버전"])
        self.assertEqual(code, 2)

    def test_retire_refuses_a_pasted_placeholder_reason(self):
        """안내문의 `--reason "<사유>"` 를 그대로 넣은 폐기가 기록에 남았다."""
        for reason in ("<사유>", "  <reason> "):
            with self.subTest(reason=reason), self.assertRaises(RetireError):
                retire_judgments("TSLA", reason=reason)

    def test_retire_without_reason_only_surveys(self):
        """사유 없이 부르면 조회다. 실수로 폐기가 일어나면 안 된다."""
        with patch.object(cli, "survey", return_value={}) as surveyed, \
                patch.object(cli, "format_survey", return_value="상태"), \
                patch.object(cli, "retire") as retired:
            code, out = run(["retire", "TSLA"])
        self.assertEqual(code, 0)
        self.assertIn("상태", out)
        surveyed.assert_called_once()
        retired.assert_not_called()

    def test_retire_without_confirm_does_not_move_anything(self):
        """기본은 조회다 — --confirm 없이 파일이 움직이면 안 된다."""
        plan = {
            "confirmed": False,
            "would_retire": [{"created_at": "2026-08-01", "assessment_id": "CIO-1"}],
            "_note": "실제로 옮기려면 --confirm",
        }
        with patch.object(cli, "retire", return_value=plan) as retired, \
                patch.object(cli, "format_survey", return_value="상태"):
            code, out = run(["retire", "TSLA", "--reason", "구버전"])
        self.assertEqual(code, 0)
        self.assertIn("폐기 대상 1건", out)
        self.assertFalse(retired.call_args.kwargs["confirm"])


class TestMainErrorFunnel(unittest.TestCase):
    """main() 이 삼키는 예외 갈래. 놓치면 사용자는 파이썬 트레이스백을 본다."""

    def funnel(self, exception):
        parsed = cli.build_parser().parse_args(["assets"])
        parsed.func = lambda _args: (_ for _ in ()).throw(exception)
        with patch.object(cli, "build_parser") as parser:
            parser.return_value.parse_args.return_value = parsed
            err = io.StringIO()
            with redirect_stderr(err), redirect_stdout(io.StringIO()):
                code = cli.main()
        return code, err.getvalue()

    def test_contract_error_prints_every_message(self):
        code, err = self.funnel(ContractError(["첫째 위반", "둘째 위반"]))
        self.assertEqual(code, 2)
        self.assertIn("첫째 위반", err)
        self.assertIn("둘째 위반", err)

    def test_document_value_and_os_errors_are_caught(self):
        for exception in (
            DocumentError("문서가 없다"),
            ValueError("값이 이상하다"),
            OSError("파일을 못 읽는다"),
        ):
            with self.subTest(type(exception).__name__):
                code, err = self.funnel(exception)
                self.assertEqual(code, 2)
                self.assertIn("ERROR:", err)

    def test_unexpected_exception_is_not_swallowed(self):
        """모르는 예외를 2로 접으면 버그가 '입력 오류'로 위장된다."""
        with self.assertRaises(KeyError):
            self.funnel(KeyError("예상 못 한 것"))

    def test_old_python_is_refused_before_anything_runs(self):
        # 실제 sys.version_info 처럼 튜플 비교와 .major/.minor 를 둘 다 만족해야
        # 한다. 평범한 튜플로 흉내 내면 안내문 자체가 AttributeError 로 죽는다.
        old = namedtuple("version_info", "major minor micro")(3, 12, 0)
        with patch.object(cli.sys, "version_info", old), \
                patch.object(cli, "build_parser") as parser:
            err = io.StringIO()
            with redirect_stderr(err):
                code = cli.main()
        self.assertEqual(code, 2)
        self.assertIn("3.13", err.getvalue())
        parser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
