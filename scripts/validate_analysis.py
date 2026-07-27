#!/usr/bin/env python3
"""리서치 분석 JSON을 검증한 뒤 승인 저장소에 복사한다."""

import argparse
import sys
from pathlib import Path

from contracts import (ContractError, load_json, require_valid_analysis,
                       save_validated_analysis)

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CALCULATIONS = ROOT / "knowledge" / "calculations"


def main():
    p = argparse.ArgumentParser(description="주장·출처·스냅샷 계약 검증")
    p.add_argument("input", help="검증할 분석 JSON")
    p.add_argument("--save", help="검증 성공 시 저장할 경로")
    p.add_argument("--calculations", default=str(DEFAULT_CALCULATIONS),
                   help="계산 산출물 저장소. 기대수익이 여기 산출물과 "
                        "일치하는지 대조한다")
    a = p.parse_args()

    try:
        data = load_json(a.input)
        if a.save:
            path = save_validated_analysis(a.save, data, a.calculations)
            print(f"VALIDATED_AND_SAVED {path}")
        else:
            require_valid_analysis(data, a.calculations)
            print("VALID")
    except (ContractError, FileNotFoundError) as exc:
        messages = exc.errors if isinstance(exc, ContractError) else [str(exc)]
        for message in messages:
            print(f"INVALID: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
