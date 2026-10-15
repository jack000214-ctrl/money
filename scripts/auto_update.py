"""오늘 날짜 기준 시세 미수집 종목 자동 검사 및 업데이트 CLI."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.stock_service import check_and_update_prices


def main() -> int:
    print("=" * 60)
    print("  Money: 오늘 시세 자동 업데이트 검사")
    print("=" * 60)
    res = check_and_update_prices()
    print(f"기준일자: {res['target_date']}")
    print(f"전체 등록 종목: {res['total_checked']}개")
    print(f"업데이트 대상: {res['stale_count']}개")
    print(f"업데이트 성공: {res['updated_count']}개")
    if res["updated"]:
        print(f"  성공 종목: {', '.join(res['updated'])}")
    if res["failed"]:
        print(f"  실패 종목: {', '.join(res['failed'])}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())
