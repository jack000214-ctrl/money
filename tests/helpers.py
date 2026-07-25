"""테스트 공용 헬퍼.

원칙
  - 네트워크를 타지 않는다. 외부 API 응답은 고정 데이터로 흉내낸다.
  - 모든 테스트는 **실제로 발생했던 버그**를 하나씩 붙잡는다.
    새 버그를 찾으면 고치기 전에 여기 테스트부터 추가한다.
"""

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# 스크립트가 두 곳에 흩어져 있다: 공용 scripts/ 와 SKILL 번들 안의 scripts/
for d in (ROOT / "scripts", ROOT / "skills" / "portfolio-xray" / "scripts"):
    sys.path.insert(0, str(d))


def write_csv(text):
    """임시 CSV 파일을 만들고 경로를 돌려준다."""
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(text.strip() + "\n")
    f.close()
    return f.name
