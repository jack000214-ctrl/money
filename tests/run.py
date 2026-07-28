#!/usr/bin/env python3
"""전체 테스트 실행.

    python3 tests/run.py

네트워크를 타지 않으므로 항상 같은 결과가 나온다.
새 버그를 발견하면 **고치기 전에 여기 테스트를 먼저 추가한다.**
"""

import sys
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))


def main():
    suite = unittest.defaultTestLoader.discover(str(HERE), pattern="test_*.py")
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
