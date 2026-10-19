"""`python -m cio watch ...` 배선: 하위 명령이 핸들러에 닿고, 종료코드가 계약을 지킨다."""
from __future__ import annotations

import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cio import __main__ as cli, watch_queue, watch_runner


def run(argv):
    args = cli.build_parser().parse_args(argv)
    out = io.StringIO()
    with redirect_stdout(out), redirect_stderr(io.StringIO()):
        code = args.func(args)
    return code, out.getvalue()


class TestWatchCli(unittest.TestCase):
    def test_subcommand_is_required(self):
        with self.assertRaises(SystemExit), redirect_stderr(io.StringIO()):
            cli.build_parser().parse_args(["watch"])

    def test_run_once_prints_cycle_and_exits_zero(self):
        cycle = {"result": "SKIPPED", "reason": "WEEKEND", "at": "t"}
        with patch.object(watch_runner, "run_cycle", return_value=cycle) as ran, \
                patch.object(watch_runner, "maybe_send_briefing") as briefed:
            code, out = run(["watch", "run", "--once"])
        self.assertEqual(code, 0)
        self.assertIn("WEEKEND", out)
        ran.assert_called_once()
        briefed.assert_called_once()

    def test_run_once_exits_two_on_error_result(self):
        with patch.object(watch_runner, "run_cycle", return_value={"result": "ERROR", "reason": "X"}), \
                patch.object(watch_runner, "maybe_send_briefing"):
            code, _ = run(["watch", "run", "--once"])
        self.assertEqual(code, 2)

    def test_status_prints_json_without_password(self):
        status = {"smtp_password_set": True, "unread": 0}
        with patch.object(watch_runner, "status", return_value=status):
            code, out = run(["watch", "status"])
        self.assertEqual(code, 0)
        self.assertIn("smtp_password_set", out)

    def test_queue_prints_formatted_items(self):
        with patch.object(watch_queue, "list_items", return_value=[]):
            code, out = run(["watch", "queue"])
        self.assertEqual(code, 0)
        self.assertIn("비어", out)


if __name__ == "__main__":
    unittest.main()
