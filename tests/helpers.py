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


def point(fy, fp, start, end, value, form="10-Q", filed="2026-01-01"):
    """SEC companyfacts 의 관측 한 개.

    edgar 와 peer_us 가 같은 모양의 입력을 받으므로 픽스처도 하나여야 한다 —
    한쪽에만 필드를 더하면 두 테스트가 다른 세상을 보게 된다.
    """
    return {
        "fy": fy,
        "fp": fp,
        "start": start,
        "end": end,
        "val": value,
        "form": form,
        "filed": filed,
    }
