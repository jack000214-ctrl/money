"""모든 명령이 실제로 이어져 있는가.

`cio` 본체는 `test_cli_contract.py` 가 본다. 여기서 보는 것은 **수집기·계산기
20여 개의 CLI 배선**이다. 이 계층은 사람이 직접 치는 자리이고, 배선이 끊기면
그 명령만 조용히 못 쓰게 된다 — 다른 명령은 멀쩡하므로 눈치채기 어렵다.

실패 방식이 셋이다.

  1. **서브커맨드를 추가하고 핸들러를 안 붙인다.** `args.func` 가 없어
     `AttributeError` 가 나거나 아무 일도 없이 끝난다.
  2. **`main()` 안에서만 쓰는 이름을 import 하지 않는다.** 모듈 import 는
     통과하고 실행 중에만 `NameError` 가 난다 — 실제로 그런 적이 있다.
  3. **`--help` 가 터진다.** 도움말은 이 저장소에서 사용법의 유일한 문서다.

여기서 명령을 **실행**하지는 않는다. 대부분 네트워크를 타기 때문이다. 파서를
세우고 배선을 확인하는 데까지가 네트워크 없이 볼 수 있는 전부다.
"""

import argparse
import importlib
import inspect
import io
import pkgutil
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import helpers  # noqa: F401  (저장소 루트를 sys.path 에 올린다)


def modules_with_a_main():
    """`main()` 을 가진 수집기·계산기 전부."""
    found = []
    for package in ("collectors", "calculators"):
        pkg = importlib.import_module(package)
        for info in pkgutil.iter_modules(pkg.__path__):
            module = importlib.import_module(f"{package}.{info.name}")
            if callable(getattr(module, "main", None)):
                found.append(module)
    return found


MODULES = modules_with_a_main()


def parsers_of(module):
    """`main()` 이 세우는 파서를 실행 없이 가로챈다.

    `parse_args` 에서 멈춘다 — 그 뒤는 네트워크다. 파서를 다 세운 시점이
    배선을 볼 수 있는 마지막 지점이다.
    """
    captured = []
    real_init = argparse.ArgumentParser.__init__

    def spy_init(self, *args, **kwargs):
        real_init(self, *args, **kwargs)
        captured.append(self)

    class Stop(Exception):
        pass

    def stop(self, *args, **kwargs):
        raise Stop

    with patch.object(argparse.ArgumentParser, "__init__", spy_init), \
            patch.object(argparse.ArgumentParser, "parse_args", stop), \
            patch.object(argparse.ArgumentParser, "parse_known_args", stop):
        try:
            module.main()
        except Stop:
            pass
    return captured


class TestEveryModuleHasAParser(unittest.TestCase):
    def test_the_scan_found_the_collectors(self):
        """모듈을 못 찾으면 이 파일 전체가 아무것도 검사하지 않는다."""
        names = {module.__name__ for module in MODULES}
        self.assertGreaterEqual(len(names), 15)
        for expected in ("collectors.dart", "collectors.price",
                         "calculators.reverse_dcf"):
            self.assertIn(expected, names)

    def test_every_main_builds_a_parser_without_crashing(self):
        """`main()` 초반에 없는 이름을 참조하면 여기서 드러난다."""
        for module in MODULES:
            with self.subTest(module.__name__):
                self.assertTrue(parsers_of(module),
                                "ArgumentParser 를 하나도 세우지 않았다")

    def test_every_parser_describes_itself(self):
        """`--help` 가 이 저장소에서 사용법의 유일한 문서다."""
        for module in MODULES:
            with self.subTest(module.__name__):
                root = parsers_of(module)[0]
                self.assertTrue(root.description or root.usage or root.prog)


class TestEverySubcommandIsWired(unittest.TestCase):
    def subcommands(self, module):
        """(이름, 파서) 목록. 서브커맨드가 없는 모듈은 빈 목록."""
        rows = []
        for parser in parsers_of(module):
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    rows.extend(action.choices.items())
        return rows

    def test_no_subcommand_is_left_without_a_handler(self):
        """`set_defaults(func=...)` 를 빠뜨리면 그 명령만 조용히 죽는다.

        핸들러를 `set_defaults` 로 붙이지 않는 모듈은 `main()` 이 이름으로
        분기하므로, 그 경우는 이름이 본문에 나오는지로 본다.
        """
        for module in MODULES:
            commands = self.subcommands(module)
            if not commands:
                continue
            source = inspect.getsource(module.main)
            for name, parser in commands:
                with self.subTest(f"{module.__name__} {name}"):
                    wired = (parser.get_default("func") is not None
                             or f'"{name}"' in source
                             or f"'{name}'" in source)
                    self.assertTrue(
                        wired, f"{name} 이 어떤 핸들러에도 이어지지 않았다")

    def test_a_declared_handler_is_callable(self):
        for module in MODULES:
            for name, parser in self.subcommands(module):
                handler = parser.get_default("func")
                if handler is None:
                    continue
                with self.subTest(f"{module.__name__} {name}"):
                    self.assertTrue(callable(handler))

    def test_subcommand_names_are_unique_within_a_module(self):
        """같은 이름을 두 번 등록하면 뒤엣것이 앞엣것을 조용히 덮는다."""
        for module in MODULES:
            names = [name for name, _parser in self.subcommands(module)]
            with self.subTest(module.__name__):
                self.assertEqual(len(names), len(set(names)))


class TestHelpTextRenders(unittest.TestCase):
    """도움말을 만드는 도중에 죽지 않는가."""

    def test_every_root_help_renders(self):
        for module in MODULES:
            with self.subTest(module.__name__):
                buffer = io.StringIO()
                with redirect_stdout(buffer):
                    parsers_of(module)[0].print_help()
                self.assertTrue(buffer.getvalue().strip())

    def test_every_subcommand_help_renders(self):
        for module in MODULES:
            for parser in parsers_of(module):
                for action in parser._actions:
                    if not isinstance(action, argparse._SubParsersAction):
                        continue
                    for name, sub in action.choices.items():
                        with self.subTest(f"{module.__name__} {name}"):
                            buffer = io.StringIO()
                            with redirect_stdout(buffer):
                                sub.print_help()
                            self.assertTrue(buffer.getvalue().strip())


if __name__ == "__main__":
    unittest.main()
