"""네트워크를 사용하지 않는 테스트 헬퍼."""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def write_csv(text):
    """임시 CSV 파일을 만들고 경로를 돌려준다."""
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(text.strip() + "\n")
    f.close()
    return f.name
