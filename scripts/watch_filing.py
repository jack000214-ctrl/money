#!/usr/bin/env python3
"""정기보고서 감시 — "그때 가서 확인한다"를 프로그램이 기억하게 한다.

삼성전자 재고자산은 1Q26 에 +10.7% QoQ 로 늘었다. 이것이 사이클 꺾임인지
HBM4 램프 대비 축적인지는 **2Q26 반기보고서가 나와야 갈린다.** 그런데 그
사실을 프로파일 본문에만 적어 두면, 다음 세션의 모델은 그걸 읽고도
"아직 안 나왔겠지" 하고 넘어간다.

이 스크립트는 DART 공시 목록을 조회해 **기다리던 보고서가 접수됐는지**만
확인하고, 접수됐으면 어떤 수집기를 다시 돌려야 하는지 알려 준다.

**판단하지 않는다.** 재고가 늘었는지 줄었는지도 말하지 않는다 —
그건 `inventory_fetch.py` 를 다시 돌려 표를 보고 모델이 할 일이다.

사용:
    python3 scripts/watch_filing.py 005930 --report 반기보고서 --year 2026
    python3 scripts/watch_filing.py 005930 --report 반기보고서 --year 2026 --then \
        "python3 scripts/inventory_fetch.py 005930 --years 2024 2025 2026"

`--then` 은 보고서가 있을 때만 출력한다. **자동 실행하지 않는다** —
수집은 API 한도를 쓰고, 무엇을 언제 돌릴지는 사람이 정한다.

/loop 와 함께 쓰면 주기 확인이 된다:
    /loop 6h python3 scripts/watch_filing.py 005930 --report 반기보고서 --year 2026
"""
from __future__ import annotations

import argparse
import datetime as dt
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dart_fetch import load_key, request, resolve  # noqa: E402

# 종료코드로 자동화에서 분기할 수 있게 한다.
FOUND, NOT_YET = 0, 3


def main():
    p = argparse.ArgumentParser(description="DART 정기보고서 접수 감시")
    p.add_argument("stock_code", help="005930")
    p.add_argument("--report", default="반기보고서",
                   help="보고서명 접두어 (반기보고서 / 분기보고서 / 사업보고서)")
    p.add_argument("--year", type=int, default=dt.date.today().year,
                   help="사업연도. 보고서명의 (YYYY.MM) 과 대조한다")
    p.add_argument("--since", help="조회 시작일 YYYY-MM-DD. 기본은 90일 전")
    p.add_argument("--then", help="보고서가 있으면 안내할 후속 명령")
    a = p.parse_args()

    key = load_key()
    info = resolve(a.stock_code, key)
    since = (a.since or (dt.date.today() - dt.timedelta(days=90)).isoformat())

    data = request("list.json", {
        "corp_code": info["corp_code"],
        "bgn_de": since.replace("-", ""),
        "end_de": dt.date.today().strftime("%Y%m%d"),
        "pblntf_ty": "A", "page_count": "100",
    }, key, allow_empty=True)

    hits = [r for r in (data or {}).get("list", [])
            if r["report_nm"].startswith(a.report) and str(a.year) in r["report_nm"]]

    label = f"{info['corp_name']} {a.year} {a.report}"
    if not hits:
        print(f"[대기] {label} — 아직 접수되지 않았다 "
              f"(조회 {since} ~ 오늘, 정기공시 {len((data or {}).get('list', []))}건)")
        return NOT_YET

    for r in hits:
        print(f"[접수] {label}")
        print(f"  보고서명   {r['report_nm'].strip()}")
        print(f"  접수번호   {r['rcept_no']}")
        print(f"  접수일자   {r['rcept_dt']}")
        print(f"  원문       https://dart.fss.or.kr/dsaf001/main.do?rcpNo={r['rcept_no']}")
    if a.then:
        print(f"\n다음에 돌릴 것 (자동 실행하지 않는다):\n  {a.then}")
    return FOUND


if __name__ == "__main__":
    raise SystemExit(main())
